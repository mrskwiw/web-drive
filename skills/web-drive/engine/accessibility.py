"""Deterministic accessibility audit (WCAG A/AA high-signal subset).

This is the *objective* half of the blind / assistive-tech pass. A missing
``alt`` attribute, an unlabeled form control, a page with no ``lang`` — these are
facts, not judgments, so they live in engine code (mirroring the
deterministic-vs-advisory rule the spec mandates). The *semantic* half —
whether the reading order is coherent, whether a keyboard-only user can
actually complete a journey, whether an accessible name is meaningful and not
just present — stays the agent's advisory judgment (SKILL.md §3d).

The audit runs in the page context (``_A11Y_JS``) and returns one aggregated
:class:`~engine.models.A11yViolation` per failed rule, each carrying up to a few
example nodes. It is intentionally a curated, low-false-positive subset — not a
full axe-core replacement — chosen so every finding is actionable and true.
"""

from __future__ import annotations

from typing import Any, Dict

from .models import A11yImpact, A11yNode, A11yReport, A11yViolation

# Human-readable rule help + impact. Kept on the Python side (single source of
# truth) so the JS only needs to emit rule ids + offending nodes.
_RULES: Dict[str, Dict[str, str]] = {
    "image-alt": {
        "impact": "serious",
        "help": "Informative <img> has no alt attribute — a screen reader "
        "announces nothing (or reads the file name). Add descriptive alt text, "
        'or alt="" (with role="presentation") if the image is decorative.',
    },
    "control-name": {
        "impact": "serious",
        "help": "Form control has no accessible name — a screen-reader user "
        "hears its type but not its purpose. Associate a <label for>, wrap it in "
        "a <label>, or add aria-label / aria-labelledby. A placeholder is NOT a "
        "name.",
    },
    "button-name": {
        "impact": "serious",
        "help": "Button has no accessible name — it announces only as 'button'. "
        "Add visible text, aria-label, or aria-labelledby (icon-only buttons "
        "especially).",
    },
    "link-name": {
        "impact": "serious",
        "help": "Link has no accessible name — it announces only as 'link' with "
        "no destination. Add link text or aria-label.",
    },
    "html-has-lang": {
        "impact": "serious",
        "help": "<html> has no lang attribute — screen readers can't pick the "
        'correct pronunciation/voice. Set e.g. lang="en".',
    },
    "document-title": {
        "impact": "serious",
        "help": "Document has no <title> — the page is unidentifiable in the tab, "
        "history, and to a screen reader on load.",
    },
    "heading-order": {
        "impact": "moderate",
        "help": "Heading levels are skipped or there is no <h1> — screen-reader "
        "users navigate by heading structure, and gaps break that outline.",
    },
    "positive-tabindex": {
        "impact": "moderate",
        "help": "tabindex > 0 forces an unnatural focus order that diverges from "
        "the visual/DOM order and traps keyboard users. Use tabindex=0 / DOM "
        "order instead.",
    },
    "duplicate-id": {
        "impact": "minor",
        "help": "Duplicate id — breaks label[for], aria-labelledby, and "
        "aria-describedby references that must point at a unique element.",
    },
}

# Page-context audit. Pure DOM inspection, no mutation. Returns:
#   { violations: [ {rule, count, nodes:[{selector, snippet}]} ], counts:{...} }
# Only visible elements are checked for name/alt rules (hidden ones don't affect
# a user); document-level rules (lang, title, duplicate-id) always run.
_A11Y_JS = r"""
() => {
  const MAX_NODES = 6;   // example nodes reported per rule (keeps output small)
  const out = {};        // rule -> [{selector, snippet}]
  const push = (rule, el) => {
    let arr = out[rule]; if (!arr) { arr = out[rule] = []; }
    if (arr.length >= MAX_NODES) { arr._over = (arr._over || 0) + 1; return; }
    arr.push({ selector: sel(el), snippet: snip(el) });
  };

  const sel = (el) => {
    if (!el || el.nodeType !== 1) return '?';
    if (el.id) return '#' + CSS.escape(el.id);
    const ti = el.getAttribute && el.getAttribute('data-testid');
    if (ti) return '[data-testid="' + ti + '"]';
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) return el.tagName.toLowerCase() + '[name="' + nm + '"]';
    let cls = '';
    if (el.className && typeof el.className === 'string') {
      const p = el.className.trim().split(/\s+/).slice(0, 2);
      if (p[0]) cls = '.' + p.map((c) => CSS.escape(c)).join('.');
    }
    return el.tagName.toLowerCase() + cls;
  };
  const snip = (el) => (el.outerHTML || '').replace(/\s+/g, ' ').trim().slice(0, 120);

  const visible = (el) => {
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  // In an aria-hidden subtree the element is removed from the a11y tree, so a
  // missing name there is moot — skip it (mirrors what a screen reader does).
  const ariaHidden = (el) => el.closest('[aria-hidden="true"]') !== null;
  const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
  // Accessible text = descendant text EXCLUDING aria-hidden subtrees, since a
  // screen reader skips those. An icon button whose only content is an
  // aria-hidden glyph therefore has NO accessible name (textContent would lie).
  const accText = (el) => {
    let s = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) s += n.textContent;
      else if (n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true') s += accText(n);
    }
    return s.replace(/\s+/g, ' ').trim();
  };

  // Accessible-name approximation (does NOT count placeholder — per WCAG a
  // placeholder is not a name). Sufficient for the high-signal rules below.
  const hasName = (el, useText) => {
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return true;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      for (const id of lb.split(/\s+/)) {
        const r = document.getElementById(id);
        if (r && txt(r)) return true;
      }
    }
    if (el.id) {
      const forLbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (forLbl && txt(forLbl)) return true;
    }
    const wrap = el.closest('label');
    if (wrap && txt(wrap)) return true;
    const title = el.getAttribute('title');
    if (title && title.trim()) return true;
    if (useText && accText(el)) return true;
    return false;
  };

  // 1. images without alt (informative). alt="" is allowed (decorative).
  for (const img of document.querySelectorAll('img')) {
    if (!visible(img) || ariaHidden(img)) continue;
    const role = (img.getAttribute('role') || '').toLowerCase();
    if (role === 'presentation' || role === 'none') continue;
    if (img.getAttribute('alt') === null && !img.getAttribute('aria-label')
        && !img.getAttribute('aria-labelledby')) push('image-alt', img);
  }

  // 2. form controls without an accessible name.
  const SKIP_INPUT = new Set(['hidden', 'submit', 'reset', 'button', 'image']);
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (!visible(el) || ariaHidden(el)) continue;
    if (el.tagName === 'INPUT' && SKIP_INPUT.has((el.getAttribute('type') || 'text').toLowerCase())) continue;
    if (!hasName(el, false)) push('control-name', el);
  }

  // 3. buttons without an accessible name (text counts, incl. emoji/icon glyphs).
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    if (!visible(el) || ariaHidden(el)) continue;
    if (el.tagName === 'INPUT') continue;  // button-like inputs handled below
    if (!hasName(el, true)) {
      // An <img> child with alt gives the button its name.
      const im = el.querySelector('img[alt]:not([alt=""]), svg title');
      if (!im) push('button-name', el);
    }
  }

  // 3b. button-like inputs. type=submit/reset carry a browser-default label
  // ("Submit"/"Reset") so an empty one is still named — exempt them. type=button
  // has NO default, and type=image is named by alt (falling back to value); a
  // missing name on either is a real, common blocker.
  for (const el of document.querySelectorAll('input[type="button"], input[type="image"]')) {
    if (!visible(el) || ariaHidden(el)) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    // Read the LIVE IDL properties (el.value / el.alt), not getAttribute — that
    // is the source the accessible-name computation actually uses, and it always
    // reflects a label set at runtime (e.g. a framework writing the property).
    const val = (el.value || '').trim();
    const alt = (el.alt || '').trim();
    const named = !!val || (type === 'image' && !!alt) || hasName(el, false);
    if (!named) push('button-name', el);
  }

  // 4. links without an accessible name. Covers real anchors AND ARIA link
  // widgets (role="link" on a span/div, common in component libraries) — a
  // compound selector returns each element once, so a<-role=link isn't double-scanned.
  for (const a of document.querySelectorAll('a[href], [role="link"]')) {
    if (!visible(a) || ariaHidden(a)) continue;
    if (!hasName(a, true)) {
      const im = a.querySelector('img[alt]:not([alt=""]), svg title');
      if (!im) push('link-name', a);
    }
  }

  // 5. <html> lang.
  const lang = document.documentElement.getAttribute('lang');
  if (!lang || !lang.trim()) push('html-has-lang', document.documentElement);

  // 6. document title.
  if (!document.title || !document.title.trim()) {
    if (document.head) push('document-title', document.head);
  }

  // 7. heading order: no h1, or a level jump > 1 (e.g. h1 -> h3).
  const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter((h) => visible(h) && !ariaHidden(h));
  let prev = 0, firstSeen = false;
  for (const h of heads) {
    const lvl = parseInt(h.tagName.substring(1), 10);
    if (!firstSeen) { firstSeen = true; if (lvl !== 1) push('heading-order', h); }
    else if (lvl > prev + 1) push('heading-order', h);
    prev = lvl;
  }

  // 8. positive tabindex.
  for (const el of document.querySelectorAll('[tabindex]')) {
    const t = parseInt(el.getAttribute('tabindex'), 10);
    if (!isNaN(t) && t > 0) { if (visible(el)) push('positive-tabindex', el); }
  }

  // 9. duplicate ids.
  const seen = new Map();
  for (const el of document.querySelectorAll('[id]')) {
    const id = el.id; if (!id) continue;
    seen.set(id, (seen.get(id) || 0) + 1);
  }
  for (const [id, n] of seen) {
    if (n > 1) { const el = document.getElementById(id); if (el) push('duplicate-id', el); }
  }

  // Assemble: attach the over-cap remainder into each rule's count.
  const violations = [];
  for (const rule of Object.keys(out)) {
    const nodes = out[rule];
    violations.push({ rule, count: nodes.length + (nodes._over || 0), nodes });
  }
  return { violations };
}
"""


def parse_a11y(raw: Dict[str, Any], url: str) -> A11yReport:
    """Map the page-context audit result into a typed :class:`A11yReport`,
    attaching each rule's impact + help text and rolling up impact counts."""
    counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0, "total": 0}
    violations = []
    for v in raw.get("violations", []):
        rule = v.get("rule", "")
        meta = _RULES.get(rule)
        if meta is None:  # unknown rule id — skip rather than emit an untyped finding
            continue
        impact = A11yImpact(meta["impact"])
        nodes = [
            A11yNode(selector=n.get("selector", "?"), snippet=n.get("snippet", ""))
            for n in v.get("nodes", [])
        ]
        count = int(v.get("count", len(nodes)))
        violations.append(
            A11yViolation(
                rule=rule,
                impact=impact,
                help=meta["help"],
                count=count,
                nodes=nodes,
            )
        )
        counts[impact.value] += 1
        counts["total"] += 1
    # Most-severe rules first so the agent triages the blockers before the nits.
    order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
    violations.sort(key=lambda x: order.get(x.impact.value, 9))
    return A11yReport(url=url, violations=violations, counts=counts)
