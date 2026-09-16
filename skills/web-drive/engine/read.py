"""``read`` — extract one page's DECLARED surface (spec §3's "claims" half).

Deliberately separate from ``browser.py``'s ``_SNAPSHOT_JS`` / ``capture_snapshot``,
which is copied byte-identical from web-qa (§4.3) and must stay that way. `read`
needs facts `capture_snapshot` was never asked for — validation attributes,
`<select>` options, aria landmarks/headings, error and empty-state copy — and
none of it is web-qa's concern, so extending the shared JS would be exactly the
incidental divergence §4.3 forbids. Duplicating the small DOM helpers (``sel``,
``uniqueSel``, a label resolver) here instead is cheaper than forking the shared
file for one command's needs — the same trade `catalog.py` makes for its models.

This is the READ half only. `probe` (Phase C) is the NAVIGATE half that checks
these claims against what actually happens; until then, everything a `PageSurface`
reports is unverified by construction.
"""

from __future__ import annotations

from typing import Any, Dict

from .browser import BrowserController
from .catalog import (
    Heading,
    Landmark,
    PageSurface,
    SurfaceControl,
    SurfaceField,
    SurfaceForm,
)

_SURFACE_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };

  const sel = (el) => {
    if (el.id) return '#' + el.id;
    const ti = el.getAttribute('data-testid');
    if (ti) return '[data-testid="' + ti + '"]';
    const nm = el.getAttribute('name');
    if (nm) return el.tagName.toLowerCase() + '[name="' + nm + '"]';
    let cls = '';
    if (el.className && typeof el.className === 'string') {
      const p = el.className.trim().split(/\s+/).slice(0, 2);
      if (p[0]) cls = '.' + p.join('.');
    }
    return el.tagName.toLowerCase() + cls;
  };

  const matchCache = new Map();
  const uniqueSel = (el, base) => {
    let arr = matchCache.get(base);
    if (arr === undefined) {
      try { arr = Array.prototype.slice.call(document.querySelectorAll(base)); }
      catch (e) { arr = []; }
      matchCache.set(base, arr);
    }
    if (arr.length <= 1) return base;
    const idx = arr.indexOf(el);
    return idx >= 0 ? base + ' >> nth=' + idx : base;
  };

  // Accessible-name resolution, in the priority ARIA actually defines:
  // aria-labelledby > aria-label > <label for>/wrapping <label> > placeholder/name > text.
  const labelFor = (el) => {
    const alBy = el.getAttribute('aria-labelledby');
    if (alBy) {
      const parts = alBy.split(/\s+/).map((id) => {
        const t = document.getElementById(id);
        return t ? (t.innerText || t.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ').slice(0, 100);
    }
    const al = el.getAttribute('aria-label');
    if (al) return al.trim().slice(0, 100);
    if (el.id) {
      let lab = null;
      try { lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); }
      catch (e) { lab = null; }
      if (lab) return (lab.innerText || lab.textContent || '').trim().slice(0, 100);
    }
    const parentLabel = el.closest ? el.closest('label') : null;
    if (parentLabel) return (parentLabel.innerText || parentLabel.textContent || '').trim().slice(0, 100);
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      return (el.getAttribute('placeholder') || el.getAttribute('name') || '').trim().slice(0, 100);
    }
    return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100);
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      return 'textbox';
    }
    return tag.toLowerCase();
  };

  // -- headings -------------------------------------------------------------
  const headings = [];
  for (const h of document.querySelectorAll('h1, h2, h3, h4, h5, h6')) {
    if (!visible(h)) continue;
    const text = (h.innerText || h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 150);
    if (text) headings.push({ level: Number(h.tagName[1]), text });
  }

  // -- landmarks --------------------------------------------------------------
  const LANDMARK_SEL = 'main, nav, header, footer, aside, ' +
    '[role=main], [role=navigation], [role=banner], [role=contentinfo], ' +
    '[role=complementary], [role=region]';
  const TAG_ROLE = { MAIN: 'main', NAV: 'navigation', HEADER: 'banner', FOOTER: 'contentinfo', ASIDE: 'complementary' };
  const landmarks = [];
  for (const el of document.querySelectorAll(LANDMARK_SEL)) {
    if (!visible(el)) continue;
    const role = el.getAttribute('role') || TAG_ROLE[el.tagName] || el.tagName.toLowerCase();
    landmarks.push({ role, name: el.getAttribute('aria-label') || null, selector: sel(el) });
  }

  // -- controls: non-form interactive elements, role-first -------------------
  const controls = [];
  const MAX_DUP = 12;
  const sigCount = new Map();
  for (const el of document.querySelectorAll('a, button, [role=button], [role=link], [onclick]')) {
    if (!visible(el)) continue;
    if (el.closest('form')) continue;  // forms report their own controls below
    const base = sel(el);
    const s = uniqueSel(el, base);
    const role = roleOf(el);
    const name = labelFor(el);
    const sig = role + '|' + name + '|' + base;
    const n = sigCount.get(sig) || 0;
    if (n >= MAX_DUP) continue;
    sigCount.set(sig, n + 1);
    let kind = 'other';
    if (el.closest('nav, header')) kind = 'nav';
    else if (el.closest('footer')) kind = 'footer';
    else if (role === 'button' || role === 'link') kind = 'cta';
    controls.push({ selector: s, role, name, kind });
  }

  // -- forms: validation attrs + <select> options -----------------------------
  const forms = [];
  for (const f of document.querySelectorAll('form')) {
    if (!visible(f)) continue;
    const formSel = uniqueSel(f, sel(f));  // two unlabeled forms both resolve to "form"
    const fields = [];
    for (const inp of f.querySelectorAll('input, select, textarea')) {
      if (!visible(inp)) continue;
      const tag = inp.tagName;
      const type = tag === 'INPUT' ? (inp.getAttribute('type') || 'text').toLowerCase() : tag.toLowerCase();
      const options = tag === 'SELECT'
        ? Array.prototype.slice.call(inp.options).map((o) => (o.textContent || '').trim())
        : [];
      fields.push({
        selector: sel(inp),
        role: roleOf(inp),
        name: inp.getAttribute('name'),
        label: labelFor(inp),
        type,
        required: inp.hasAttribute('required') || inp.getAttribute('aria-required') === 'true',
        placeholder: inp.getAttribute('placeholder'),
        maxlength: inp.hasAttribute('maxlength') ? Number(inp.getAttribute('maxlength')) : null,
        minlength: inp.hasAttribute('minlength') ? Number(inp.getAttribute('minlength')) : null,
        pattern: inp.getAttribute('pattern'),
        options,
        default_value: tag === 'SELECT' ? (inp.value || null) : (inp.getAttribute('value') || null),
        aria_label: inp.getAttribute('aria-label'),
        aria_describedby: inp.getAttribute('aria-describedby'),
      });
    }
    // Same submit-resolution priority as `map`'s form capture (browser.py's
    // `_SNAPSHOT_JS`), kept consistent on purpose: a bare first-button query
    // picks up in-field controls (e.g. a show-password toggle), not submit.
    const nonSubmit = [...f.querySelectorAll('button:not([type=button]):not([type=reset])')];
    const submitEl =
      f.querySelector('button[type=submit]') ||
      f.querySelector('input[type=submit]') ||
      f.querySelector('input[type=image]') ||
      (nonSubmit.length ? nonSubmit[nonSubmit.length - 1] : null) ||
      f.querySelector('button');
    // Scoped to the submit control's OWN text plus field labels -- NOT the
    // whole form's innerText. A login form commonly nests a "Don't have an
    // account? Sign up" cross-link inside the SAME <form> element; testing
    // the full form text against `sign\s*up` misclassified an ordinary login
    // as destructive (found live on quizsquirrel.com's /login, 2026-09-16).
    // The form's own claim is what its submit button says it does.
    const ownText = (
      (submitEl ? (submitEl.innerText || submitEl.value || '') : '') +
      ' ' + fields.map((x) => x.label).join(' ')
    ).toLowerCase();
    const DESTRUCTIVE = /\b(pay|checkout|purchase|place order|delete|remove|cancel account|unsubscribe|sign\s*up|register|create account)\b/;
    const LOGIN = /\b(log\s*in|sign\s*in)\b/;
    const hasPassword = fields.some((x) => x.type === 'password');
    const destructive = DESTRUCTIVE.test(ownText) || (hasPassword && !LOGIN.test(ownText));
    forms.push({
      selector: formSel,
      role_name: f.getAttribute('aria-label') || null,
      fields,
      submit_selector: submitEl ? sel(submitEl) : null,
      submit_text: submitEl ? (submitEl.innerText || submitEl.value || '').trim().slice(0, 100) : null,
      destructive,
    });
  }

  // -- error messages: visible role=alert / aria-live regions -----------------
  const errors = [];
  for (const el of document.querySelectorAll('[role=alert], [aria-live="assertive"], [aria-live="polite"]')) {
    if (!visible(el)) continue;
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    if (text) errors.push(text.slice(0, 200));
  }

  // -- empty-state copy: what the app says when a list has nothing in it ------
  const EMPTY_RE = /(no results|nothing (here|to show)|no .* (found|yet)|\bempty\b|get started by|you (don't|do not) have any)/i;
  const empty_states = [];
  const bodyText = document.body ? document.body.innerText : '';
  if (EMPTY_RE.test(bodyText)) {
    const lines = bodyText.split('\n').map((s) => s.trim()).filter(Boolean);
    const seen = new Set();
    for (const line of lines) {
      if (empty_states.length >= 10) break;
      const m = line.match(EMPTY_RE);
      if (!m) continue;
      const snippet = line.slice(0, 150);
      if (!seen.has(snippet)) { seen.add(snippet); empty_states.push(snippet); }
    }
  }

  // -- general copy: visible paragraph/list text inside the main content -----
  const copy = [];
  for (const p of document.querySelectorAll('main p, main li, [role=main] p, [role=main] li')) {
    if (copy.length >= 20) break;
    if (!visible(p)) continue;
    const text = (p.innerText || p.textContent || '').trim().replace(/\s+/g, ' ');
    if (text.length >= 10) copy.push(text.slice(0, 200));
  }

  return { headings, landmarks, controls, forms, errors, empty_states, copy };
}
"""


async def read_surface(controller: BrowserController) -> PageSurface:
    """Evaluate ``_SURFACE_JS`` against the CURRENT page and build a `PageSurface`.

    The caller navigates first — unlike `map`, `read` operates on one page the
    agent already chose, so navigation is the CLI command's job, not this one's.
    """
    raw: Dict[str, Any] = await controller.page.evaluate(_SURFACE_JS)
    return PageSurface(
        url=controller.page.url,
        title=await controller.page.title(),
        headings=[Heading(**h) for h in raw.get("headings", [])],
        landmarks=[Landmark(**lm) for lm in raw.get("landmarks", [])],
        controls=[SurfaceControl(**c) for c in raw.get("controls", [])],
        forms=[
            SurfaceForm(
                selector=f["selector"],
                role_name=f.get("role_name"),
                fields=[SurfaceField(**x) for x in f.get("fields", [])],
                submit_selector=f.get("submit_selector"),
                submit_text=f.get("submit_text"),
                destructive=f.get("destructive", False),
            )
            for f in raw.get("forms", [])
        ],
        errors=list(raw.get("errors", [])),
        empty_states=list(raw.get("empty_states", [])),
        copy=list(raw.get("copy", [])),
    )
