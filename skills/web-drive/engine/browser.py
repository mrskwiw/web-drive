"""Headless-browser driver + evidence capture (Phase A).

``BrowserController`` wraps Playwright's async API. Its job is narrow: launch a
browser, perform one requested :class:`~engine.models.Action`, and capture a full
:class:`~engine.models.PageState`. It makes no judgments — deterministic gating
(Phase B) and semantic judgment (the agent) live elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .accessibility import _A11Y_JS, parse_a11y
from .models import (
    Action,
    ActionType,
    A11yReport,
    BrowserEngine,
    ConsoleDelta,
    ConsoleMessage,
    FormField,
    FormInfo,
    IncompleteFeature,
    InteractiveElement,
    LinkInfo,
    NetworkCall,
    PageSnapshot,
    PageState,
)

# JS that emits a compact role/text tree of interactive + landmark elements.
# Structure over raw HTML keeps the outline token-efficient for the agent.
_DOM_OUTLINE_JS = r"""
() => {
  const MAX_LINES = 250;
  const lines = [];
  const INTERACTIVE = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA']);
  const LANDMARK = new Set(['NAV', 'MAIN', 'HEADER', 'FOOTER', 'FORM', 'SECTION', 'ASIDE']);

  const label = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 80);
    if (el.tagName === 'INPUT') {
      return (el.getAttribute('placeholder') || el.getAttribute('name') || el.type || '').trim().slice(0, 80);
    }
    const t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    return t.slice(0, 80);
  };

  const selectorFor = (el) => {
    if (el.id) return '#' + el.id;
    const testid = el.getAttribute('data-testid');
    if (testid) return '[data-testid="' + testid + '"]';
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
    let cls = '';
    if (el.className && typeof el.className === 'string') {
      const parts = el.className.trim().split(/\s+/).slice(0, 2);
      if (parts[0]) cls = '.' + parts.join('.');
    }
    return el.tagName.toLowerCase() + cls;
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };

  const walk = (el, depth) => {
    if (lines.length >= MAX_LINES) return;
    for (const child of el.children) {
      const tag = child.tagName;
      const isInt = INTERACTIVE.has(tag) || child.hasAttribute('role') || child.hasAttribute('onclick');
      const isLand = LANDMARK.has(tag);
      if ((isInt || isLand) && visible(child)) {
        const role = child.getAttribute('role') || tag.toLowerCase();
        const indent = '  '.repeat(Math.min(depth, 8));
        lines.push(indent + role + ' "' + label(child) + '" {' + selectorFor(child) + '}');
      }
      walk(child, depth + ((isInt || isLand) ? 1 : 0));
      if (lines.length >= MAX_LINES) return;
    }
  };

  if (document.body) walk(document.body, 0);
  return lines.join('\n');
}
"""

_FOCUS_JS = (
    "() => { const a = document.activeElement; "
    "if (!a || a === document.body) return null; "
    "return a.id ? '#' + a.id : a.tagName.toLowerCase(); }"
)

# Readable rendered text (what the *user* sees), for outcome verification — the
# agent judges the produced content against intent (e.g. did research actually
# yield keywords?). Distinct from the structural DOM outline; capped for tokens.
_CONTENT_JS = r"""
() => {
  const t = (document.body && document.body.innerText) ? document.body.innerText : '';
  const clean = t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const LIMIT = 20000;
  if (clean.length <= LIMIT) return clean;
  // Truncation must ANNOUNCE itself. Silently returning a prefix makes a partial
  // read indistinguishable from a whole page, so an agent judging "is this output
  // complete?" reaches a confident verdict on evidence whose end it cannot see.
  // Reporting the true length also tells it how much it is missing.
  return clean.slice(0, LIMIT) +
    '\n\n[content truncated: showing ' + LIMIT + ' of ' + clean.length + ' chars]';
}
"""

# Structured page inventory for the agent: ranked interactive elements, forms
# (with a destructive heuristic), and links (external / new-tab / scheme flags).
_SNAPSHOT_JS = r"""
() => {
  const originHost = location.host;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => {
    const a = el.getAttribute('aria-label');
    if (a) return a.trim().slice(0, 100);
    if (el.tagName === 'INPUT') {
      return (el.getAttribute('placeholder') || el.getAttribute('name') || el.type || '').trim().slice(0, 100);
    }
    return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100);
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
  const landmark = (el) => {
    let n = el.parentElement;
    while (n) {
      const t = n.tagName;
      if (t === 'MAIN' || t === 'HEADER' || t === 'FOOTER' || t === 'NAV' || t === 'FORM') return t;
      n = n.parentElement;
    }
    return 'BODY';
  };

  // Resolve a selector that addresses THIS element uniquely. A bare tag/class
  // selector often matches many nodes (row-level Edit/Delete buttons, repeated
  // links); Playwright would then click the wrong one — or strict-fail. Append a
  // Playwright `>> nth=` disambiguator keyed to the element's DOM position. The
  // match set per base selector is cached so this stays cheap on large DOMs.
  const matchCache = new Map();
  const uniqueSel = (el, base) => {
    let arr = matchCache.get(base);
    if (arr === undefined) {
      try { arr = Array.prototype.slice.call(document.querySelectorAll(base)); }
      catch (e) { arr = []; }  // invalid selector string (e.g. Tailwind `md:flex`)
      matchCache.set(base, arr);
    }
    if (arr.length <= 1) return base;
    const idx = arr.indexOf(el);
    return idx >= 0 ? base + ' >> nth=' + idx : base;
  };

  const interactive = [];
  const links = [];
  // Cap a run of identical controls (same role/text/base selector) so a 500-row
  // table can't swamp the inventory, while still PRESERVING duplicates the old
  // dedup collapsed — the agent needs to see & test each row's action surface.
  const MAX_DUP = 12;
  const sigCount = new Map();
  for (const el of document.querySelectorAll('a, button, input, select, textarea, [role=button], [onclick]')) {
    if (!visible(el)) continue;
    const tag = el.tagName;
    const base = sel(el);
    const s = uniqueSel(el, base);
    const text = label(el);
    const role = el.getAttribute('role') || (tag === 'A' ? 'link' : tag === 'BUTTON' ? 'button' : tag.toLowerCase());
    const loc = landmark(el);
    let rank = 4, kind = 'other';
    if (loc === 'MAIN') { const big = (tag === 'BUTTON' || tag === 'A'); rank = big ? 0 : 2; kind = big ? 'cta' : 'field'; }
    else if (loc === 'HEADER' || loc === 'NAV') { rank = 1; kind = 'nav'; }
    else if (loc === 'FORM') { rank = 2; kind = 'field'; }
    else if (loc === 'FOOTER') { rank = 3; kind = 'footer'; }
    const sig = role + '|' + text + '|' + base;
    const n = sigCount.get(sig) || 0;
    if (n < MAX_DUP) { sigCount.set(sig, n + 1); interactive.push({ selector: s, role, text, rank, kind }); }
    if (tag === 'A') {
      const rawHref = el.getAttribute('href');
      const href = rawHref || '';
      let scheme = 'relative', external = false;
      try {
        const u = new URL(href, location.href);
        scheme = u.protocol.replace(':', '');
        external = (u.host !== originHost) && (scheme === 'http' || scheme === 'https');
      } catch (e) { scheme = href ? 'unknown' : 'relative'; }
      // A link that goes nowhere: no href, '#', or a javascript: sink.
      const dead = (rawHref === null) || href === '' || href === '#'
        || /^javascript:/i.test(href);
      links.push({ selector: s, text, href, target: el.getAttribute('target'), external, scheme, dead });
    }
  }

  // Incomplete / not-built markers a human tester would notice. Extracted from
  // the page's visible text in ONE pass — a single body.innerText (which already
  // covers ALL visible elements incl. div/p, so nothing is missed by markup) then
  // a cheap line scan. No per-element walk: fast on huge DOMs AND complete.
  const INCOMPLETE_RE = /(coming soon|under construction|not implemented|work in progress|placeholder|lorem ipsum|to be added|\btbd\b|\bwip\b)/i;
  const incomplete = [];
  const bodyText = document.body ? document.body.innerText : '';
  if (INCOMPLETE_RE.test(bodyText)) {
    const lines = bodyText.split('\n').map((s) => s.trim()).filter(Boolean);
    const seenInc = new Set();
    for (let i = 0; i < lines.length && incomplete.length < 30; i++) {
      const m = lines[i].match(INCOMPLETE_RE);
      if (!m) continue;
      // Label = the marker line; if that's just the bare marker, prepend the
      // previous line (usually the feature name, e.g. "Voice Analysis").
      let label = lines[i];
      if (label.length < 25 && i > 0 && !INCOMPLETE_RE.test(lines[i - 1])) {
        label = lines[i - 1] + ' — ' + label;
      }
      label = label.slice(0, 90);
      if (!seenInc.has(label)) { seenInc.add(label); incomplete.push({ marker: m[0].toLowerCase(), label }); }
    }
  }

  const forms = [];
  for (const f of document.querySelectorAll('form')) {
    if (!visible(f)) continue;
    const fields = [];
    for (const inp of f.querySelectorAll('input, select, textarea')) {
      fields.push({
        selector: sel(inp),
        name: inp.getAttribute('name'),
        type: (inp.getAttribute('type') || inp.tagName.toLowerCase()),
        label: label(inp),
      });
    }
    // Resolve the true submit control in priority order. A bare
    // `querySelector('..., button')` returns the FIRST button in DOM order,
    // which on many forms is an in-field control (e.g. a show-password toggle),
    // not the submit. Prefer explicit submit, then a button that is not
    // explicitly non-submit, taking the LAST such (submit is usually last).
    const nonSubmit = [...f.querySelectorAll('button:not([type=button]):not([type=reset])')];
    const submitEl =
      f.querySelector('button[type=submit]') ||
      f.querySelector('input[type=submit]') ||
      f.querySelector('input[type=image]') ||
      (nonSubmit.length ? nonSubmit[nonSubmit.length - 1] : null) ||
      f.querySelector('button');
    // Every submit-capable control ASSOCIATED with this form, not just its
    // descendants -- HTML lets `form="id"` submit-associate a control living
    // anywhere else in the document, and such a control still fires this
    // form's submit despite living outside it (post-commit review,
    // 2026-09-18, round 2). `el.form` is the browser's own association
    // resolution (handles both nesting and `form=""`, and ignores a
    // `form=""` that doesn't resolve to any real form), so filtering the
    // whole document by it is correct where string-matching an id attribute
    // would not be.
    const isSubmitCapable = (el) => {
      const tag = el.tagName;
      if (tag === 'BUTTON') {
        const t = (el.getAttribute('type') || 'submit').toLowerCase();
        return t !== 'button' && t !== 'reset';
      }
      if (tag === 'INPUT') {
        const t = (el.getAttribute('type') || '').toLowerCase();
        return t === 'submit' || t === 'image';
      }
      return false;
    };
    const submitCandidates = Array.prototype.filter.call(
      document.querySelectorAll('button, input'),
      (el) => el.form === f && isSubmitCapable(el)
    );
    // The whole-form DESTRUCTIVE scan below reads `f.innerText`, which is
    // DESCENDANTS ONLY -- an externally form-associated control's own text
    // ("Delete account" on a `form="id"` button living outside the <form>)
    // is invisible to it. Fold each such external candidate's own label in,
    // so a destructive external submit is still caught by the fallback scan
    // even where the login exemption below does not fire.
    const externalText = submitCandidates
      .filter((el) => !f.contains(el))
      .map((el) => (el.innerText || el.value || ''))
      .join(' ');
    const text = ((f.innerText || '') + ' ' + externalText).toLowerCase();
    // A password field alone is NOT destructive — a login is idempotent auth,
    // not a record-creating action. Flag on destructive wording, or on a
    // password field that is NOT on a login form (likely registration / set-password).
    const DESTRUCTIVE = /\b(pay|checkout|purchase|place order|delete|remove|cancel account|unsubscribe|sign\s*up|register|create account)\b/;
    const LOGIN = /\b(log\s*in|sign\s*in)\b/;
    const hasPassword = fields.some((x) => x.type === 'password');
    // A form whose OWN (and ONLY) submit is a login action is never destructive,
    // no matter what else the whole form's text contains -- a login commonly
    // nests a "Don't have an account? Sign up" cross-link inside the SAME
    // <form>, and "sign up" alone would otherwise satisfy DESTRUCTIVE against
    // the whole-form text above (found live on quizsquirrel.com's /login,
    // 2026-09-16/18). Gated on exactly ONE submit-capable control associated
    // with the form: `submitEl` above resolves to only ONE winner by priority
    // order, so a form with a SECOND, genuinely destructive submit (descendant
    // or externally `form="id"`-associated) must not have that second action's
    // risk hidden behind whichever button the priority chain happened to pick.
    const submitText = (submitEl ? (submitEl.innerText || submitEl.value || '') : '').toLowerCase();
    const isLoginSubmit = submitCandidates.length === 1 && LOGIN.test(submitText);
    const destructive = !isLoginSubmit && (DESTRUCTIVE.test(text) || (hasPassword && !LOGIN.test(text)));
    forms.push({ selector: sel(f), fields, submit: submitEl ? sel(submitEl) : null, destructive });
  }

  // React Native Web renders every control as `button.css-<hash> >> nth=N`
  // with no semantic landmark, so the location-based rank above can't tell
  // any of them apart -- every element falls through to the UNRANKED
  // default (4), and a stable sort over an all-4 list is equivalent to
  // plain DOM order. On isekaizero's storyline page that put the one
  // control that mattered ("Start Now") at position 77 of 78, past a
  // `--max-probes 12` budget spent entirely on nav chrome (BUGS.md
  // 2026-08-26). Detection must check the UNRANKED VALUE, not merely that
  // ranks are equal (post-commit review, 2026-09-19): a page with several
  // legitimate main-CTA buttons and nothing else ALSO has every element
  // sharing one rank (0, a real positive identification, not "the ranker
  // learned nothing") -- an equal-ranks-only check would have run the
  // label guess over that page too and demoted a CTA whose label matches
  // neither keyword list (e.g. "Delete", "Edit") down from its correct
  // rank 0. Only rank 4 means the primary ranker found nothing at all;
  // ranks 0-3 are each a real landmark match and must never be
  // second-guessed by a keyword heuristic, uniform or not. Overwrites
  // `rank` itself (not just array order) so the reported field stays
  // consistent with its own documented contract ("0 = primary CTA")
  // regardless of which mechanism produced it.
  if (interactive.length > 1 && interactive.every((x) => x.rank === 4)) {
    const HIGH = /\b(start|play|begin|create|continue|next|go|launch|submit|join|enter|open)\b/i;
    const LOW = /\b(home|profile|settings|notifications?|menu|back|cancel|close|log\s*out|help|about)\b/i;
    for (const x of interactive) {
      x.rank = HIGH.test(x.text) ? 0 : LOW.test(x.text) ? 2 : 1;
    }
  }

  interactive.sort((a, b) => a.rank - b.rank);
  return { interactive, forms, links, incomplete };
}
"""


# Raised internally when the auth state moved while the document was being read.
# Retried, never surfaced: a snapshot mixing two auth states is not evidence.
_UNSTABLE_CAPTURE = "capture raced a cookie change"

# Every document-scoped field in a single atomic read (see _capture_state_once).
_STATE_JS = (
    "() => ({"
    " url: location.href,"
    " title: document.title,"
    " readyState: document.readyState,"
    " focus: (" + _FOCUS_JS + ")(),"
    " outline: (" + _DOM_OUTLINE_JS + ")(),"
    " content: (" + _CONTENT_JS + ")()"
    "})"
)


# URL patterns dropped when a run only cares about structure. Measured against
# isekaizero.com over 4 loads per condition: requests fall from a median of 184
# to 46 (~75%), which is the entire point on a rate-limited crawl. Control counts
# were 162 median unblocked vs 152 blocked, with heavily overlapping ranges
# (157-172 vs 120-168) because the page rotates its content per load -- so a
# small loss cannot be ruled out, and any single-run comparison here is noise.
# Re-measure with repeats before trusting a claim about this trade.
#
# The exclusions are each load-bearing, and each was established by experiment
# rather than assumption:
#
# * FONTS ARE NOT BLOCKED. This looks like the safest thing in the list and is
#   the most dangerous. Blocking fonts took isekaizero from 159 controls to
#   ZERO with an empty body -- its nav is an icon font (private-use glyphs like
#   ), and the app gates its render on the font resolving. A font blocklist
#   does not degrade such a page, it erases it, and the crawl reports a
#   confident empty map.
# * STYLESHEETS ARE NOT BLOCKED. Every snapshot filters through `visible()`
#   (getBoundingClientRect + computed display/visibility), so dropping CSS
#   collapses real controls to zero size and reveals normally-hidden menus.
# * SCRIPTS ARE NOT BLOCKED. An SPA has no DOM without them.
# * SVG IS NOT BLOCKED. It is nominally an image, but it is what icon buttons,
#   nav glyphs and logos are actually made of -- the same shape as the font
#   failure above. Measurement settled it rather than argument: dropping it from
#   the list cost ONE request out of 46. Zero benefit against a real risk to the
#   controls the map exists to record.
_BLOCKED_URL_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.avif",
    "*.ico", "*.bmp", "*.mp4", "*.webm", "*.mp3", "*.wav", "*.ogg",
]

# COMPLETION_AND_OPTIMIZATION_PLAN.md v2.2, X-M1 (2026-09-22): opt-in, conservative
# Chromium flags that cut per-instance baseline RSS in headless/automation contexts.
# Matters most for `SKILL.md`'s fan-out orchestration, where ~4-6 of these launch
# concurrently -- every flag here is paid once per subagent. Deliberately does NOT
# include `--single-process`: it destabilizes Playwright's own CDP connection and
# would trade a memory saving for flaky runs, which is a worse failure mode than the
# memory pressure this flag exists to reduce.
_LOW_MEMORY_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
]


class BrowserController:
    """Drive a single page and capture its observable state."""

    def __init__(
        self,
        engine: BrowserEngine = BrowserEngine.CHROMIUM,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        timeout_ms: int = 15000,
        slowmo_ms: int = 0,
        nav_idle_ms: int = 3000,
        storage_state: Optional[Any] = None,
        user_agent: Optional[str] = None,
        block_assets: bool = False,
        low_memory: bool = False,
    ) -> None:
        self._block_assets = block_assets
        self._low_memory = low_memory
        # How asset blocking actually resolved, set by launch(). Carried into the
        # sitemap so "I asked for blocking" and "blocking happened" can never
        # again be assumed to be the same statement.
        self.asset_blocking = "off"
        self._engine = engine
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._timeout = timeout_ms
        self._slowmo = slowmo_ms
        # Bounded, best-effort settle after a navigation reaches a load milestone.
        # NOT a hard wait for networkidle (which never arrives on apps with
        # polling/websockets/SSE/analytics and would hang the run).
        self._nav_idle_ms = nav_idle_ms
        # Persistent auth: a Playwright storage_state (cookies + localStorage) to seed
        # the context with, and the user-agent to run under. Auth tokens are commonly
        # bound to a device fingerprint (user-agent + IP), so the SAME user_agent must
        # be used when a saved session is replayed or the server will reject the token.
        # Establish once (login → save_session), then reuse across many explore/act/flow
        # runs without re-authenticating — sidesteps auth rate limits and bot challenges.
        self._storage_state = storage_state
        self._user_agent = user_agent

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # Cumulative capture (deltas derived by the EvidenceBundler).
        self._console: List[ConsoleMessage] = []
        self._network: List[NetworkCall] = []
        self._page_errors: List[str] = []

        # New tabs / popups opened by an action (e.g. target="_blank" links).
        self._popup_pages: list = []
        self._popups: List[NetworkCall] = []

    # Playwright handles are None until launch(). These accessors assert the
    # launched invariant in one place, so the call sites below can use a
    # non-Optional handle instead of each re-proving it. Using the controller
    # before launch() is a programming error and now says so, instead of
    # surfacing as an AttributeError on None several frames deeper.
    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Controller is not launched - call launch() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Controller is not launched - call launch() first.")
        return self._context

    # -- lifecycle ---------------------------------------------------------

    async def launch(self) -> None:
        self._pw = await async_playwright().start()
        browser_type = getattr(self._pw, self._engine.value)
        launch_kwargs: dict = {"headless": self._headless, "slow_mo": self._slowmo}
        if self._low_memory:
            launch_kwargs["args"] = _LOW_MEMORY_ARGS
        self._browser = await browser_type.launch(**launch_kwargs)
        ctx_kwargs: dict = {"viewport": self._viewport}
        if self._user_agent:
            ctx_kwargs["user_agent"] = self._user_agent
        if self._storage_state is not None:
            # Playwright accepts either a state dict or a path to a state file.
            ctx_kwargs["storage_state"] = self._storage_state
        self._context = await self._browser.new_context(**ctx_kwargs)
        self._page = await self.context.new_page()
        self.page.set_default_timeout(self._timeout)
        if self._block_assets:
            self.asset_blocking = await self._install_asset_blocking()
        self._wire_listeners()

    async def _install_asset_blocking(self) -> str:
        """Drop weight-only assets at the network stack, and report how.

        Deliberately NOT Playwright's `context.route()`. Routing enables CDP
        request interception for the WHOLE context regardless of how narrow the
        url pattern is, and that alone is enough to break a real app: on
        isekaizero, a route handler that merely called `continue_()` on every
        request took the page from 156 controls to 2. The pattern argument
        chooses what reaches your handler, not what gets intercepted.

        `Network.setBlockedURLs` is a network-stack blocklist instead, so
        unmatched requests travel the ordinary path untouched. The cost is that
        it is Chromium-only -- which is reported rather than silently ignored,
        since a control that quietly does nothing is the exact defect this
        method was written to fix.
        """
        if self._engine is not BrowserEngine.CHROMIUM:
            return f"unavailable: {self._engine.value} has no CDP blocklist"
        cdp = await self.context.new_cdp_session(self.page)
        await cdp.send("Network.enable")
        await cdp.send("Network.setBlockedURLs", {"urls": _BLOCKED_URL_PATTERNS})
        return "chromium-cdp"

    async def save_session(self, path: str, user_agent: Optional[str] = None) -> str:
        """Persist the live context's auth session (cookies + localStorage) plus the
        user-agent to a session-bundle JSON, for replay by a later run via ``--session``.

        Call this while the context is still open (e.g. at the end of a login flow),
        NOT after ``close()``. The recorded user-agent matters: a fingerprint-bound
        token is only valid when replayed under the same user-agent.

        When no UA was pinned we record the one the browser ACTUALLY used, read from
        the live page — never ``null``. A null here used to be silently poisonous:
        the replay would fall through to its own default UA, which for a headless
        run is the ``HeadlessChrome`` string, so a session established in a headed
        login was replayed under a different fingerprint (and one that announces
        itself as a bot). That reads as an expired token, and the misdiagnosis costs
        a re-login every time.
        """
        state = await self.context.storage_state()
        ua = user_agent or self._user_agent
        if not ua:
            try:
                ua = await self.page.evaluate("() => navigator.userAgent")
            except Exception:  # noqa: BLE001 — page already gone; better null than crash
                ua = None
        bundle = {
            "user_agent": ua,
            "storage_state": state,
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        return str(target)

    async def close(self) -> None:
        """Tear down, tolerating a target that is already gone.

        Teardown must never be able to fail a run. A page that was still
        navigating (or a browser that died) makes these calls raise
        "Target page, context or browser has been closed" — which propagates out
        of the CLI's `finally`, turns a COMPLETED run into exit 1, and discards
        every bundle it had already captured. The evidence is the product; losing
        it to a cleanup error is the worst possible trade.
        """
        for shutdown in (
            lambda: self._context.close() if self._context is not None else None,
            lambda: self._browser.close() if self._browser is not None else None,
            lambda: self._pw.stop() if self._pw is not None else None,
        ):
            try:
                coro = shutdown()
                if coro is not None:
                    await coro
            except Exception:  # noqa: BLE001 — already-closed targets are not errors
                pass

    def _wire_listeners(self) -> None:
        self.page.on(
            "console",
            lambda msg: self._console.append(
                ConsoleMessage(level=msg.type, text=msg.text)
            ),
        )
        self.page.on("pageerror", lambda exc: self._page_errors.append(str(exc)))
        self.page.on("response", self._on_response)
        # Attached after the main page exists, so it only fires for popups.
        self.context.on("page", self._on_popup)

    def _on_response(self, response) -> None:
        try:
            self._network.append(
                NetworkCall(
                    method=response.request.method,
                    url=response.url,
                    status=response.status,
                )
            )
        except (
            Exception
        ):  # noqa: BLE001  # nosec B110 - a bad record must not abort the run
            pass

    def _on_popup(self, page) -> None:
        """Track a new tab/popup opened by an action; status is resolved in settle_popups."""
        self._popup_pages.append(page)

    # -- actions -----------------------------------------------------------

    async def navigate(self, url: str) -> None:
        # Load to a guaranteed milestone; never block on networkidle, which never
        # arrives on apps with persistent connections and would time out the run
        # before the page is even captured.
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
        # Opportunistic, bounded settle so first-paint XHRs land on well-behaved
        # pages; a page that never idles simply proceeds after nav_idle_ms.
        try:
            await self.page.wait_for_load_state(
                "networkidle", timeout=self._nav_idle_ms
            )
        except (
            Exception
        ):  # noqa: BLE001  # nosec B110 — persistent connections: proceed
            pass

    async def perform(self, action: Action) -> None:
        """Dispatch a single action, then wait briefly for the page to settle."""
        t = action.type
        if t is ActionType.NAVIGATE:
            await self.navigate(_require(action.url, "url"))
        elif t is ActionType.CLICK:
            await self.page.click(_require(action.selector, "selector"))
        elif t is ActionType.FILL:
            await self._fill_verified(
                _require(action.selector, "selector"), action.value or ""
            )
        elif t is ActionType.TYPE:
            await self.page.type(
                _require(action.selector, "selector"), action.text or ""
            )
        elif t is ActionType.PRESS:
            await self.page.press(
                action.selector or "body", _require(action.key, "key")
            )
        elif t is ActionType.SCROLL:
            distance = int(action.value) if action.value else 500
            await self.page.mouse.wheel(0, distance)
        elif t is ActionType.WAIT_FOR:
            await self.page.wait_for_selector(_require(action.selector, "selector"))
        elif t is ActionType.PAUSE:
            await self.wait_for_human(
                message=action.text,
                until_selector=action.selector,
                until_url=action.url,
                timeout_s=int(action.value) if action.value else 300,
            )
        elif t is ActionType.SELECT:
            sel = _require(action.selector, "selector")
            option = action.value if action.value is not None else (action.text or "")
            # Author by the human-visible label first (how a QA step is written),
            # falling back to the underlying option value if no label matches.
            try:
                await self.page.select_option(sel, label=option)
            except Exception:  # noqa: BLE001  # nosec B110  -- retry by value
                await self.page.select_option(sel, value=option)
        else:  # pragma: no cover — enum is exhaustive
            raise ValueError(f"Unsupported action type: {t}")
        await self.page.wait_for_timeout(300)

    async def _fill_verified(self, selector: str, value: str) -> None:
        """``fill``, then prove the value actually landed — retrying as keystrokes.

        Playwright's ``fill`` sets the value through the native property setter and
        emits ONE synthetic ``input`` event. React Native Web (and some controlled
        React inputs) bind their own handler and re-render from component state, so
        the DOM value is reverted and the field ends up EMPTY — with no exception,
        no console error, and a fully passing gate. Observed on isekaizero's persona
        form, 2026-08-26; a green step that typed nothing is the worst kind of
        failure this engine can produce, because it is invisible in the evidence.

        ``type`` dispatches real per-character key events, which those handlers do
        honour. We try the fast path first and fall back only when the read-back
        disagrees, so ordinary inputs keep ``fill``'s speed and its ability to
        replace existing text.
        """
        await self.page.fill(selector, value)
        try:
            landed = await self.page.input_value(selector, timeout=2000)
        except Exception:  # noqa: BLE001 — not an <input>/<textarea>; nothing to verify
            return
        if landed == value:
            return
        # The value did not stick. Clear whatever partial state exists and type it.
        await self.page.click(selector)
        await self.page.keyboard.press("ControlOrMeta+a")
        await self.page.keyboard.press("Delete")
        await self.page.type(selector, value)

    async def wait_for_human(
        self,
        message: Optional[str] = None,
        until_selector: Optional[str] = None,
        until_url: Optional[str] = None,
        timeout_s: int = 300,
        poll_ms: int = 500,
    ) -> bool:
        """Block until a human finishes something in the visible browser window.

        The escape hatch for walls a QA tool must not pick: a bot challenge
        (Turnstile/reCAPTCHA), MFA, an SSO redirect, 3-D Secure. SKILL.md's rule is
        that defeating these is an arms race we stay out of — but a human clearing
        one by hand, once, inside the flow's own context, is not defeating anything.
        Every later step then runs with the resulting state already in place.

        Raises if the browser is headless: you cannot solve a challenge you cannot
        see, and silently waiting out the timeout would turn an unsatisfiable step
        into a slow no-op that later steps build on. Returns whether the resume
        condition was actually observed — with no condition given, waiting out the
        clock is the honest answer and returns True.
        """
        if self._headless:
            raise RuntimeError(
                "A 'pause' step needs a browser the human can see — re-run this flow "
                "with --no-headless. (Refusing to wait blindly in headless mode: the "
                "challenge could never be solved and later steps would run on an "
                "unmet precondition.)"
            )
        # stderr, not stdout: stdout carries the flow's JSON result.
        print(
            f"\n>>> PAUSED — {message or 'finish the step in the browser window.'}",
            file=sys.stderr,
            flush=True,
        )
        if until_selector:
            cond = f"until {until_selector!r} appears"
        elif until_url:
            cond = f"until the URL contains {until_url!r}"
        else:
            cond = "for the full window (no resume condition given)"
        print(f">>> Waiting {cond}, up to {timeout_s}s.\n", file=sys.stderr, flush=True)

        waited = 0
        while waited < timeout_s * 1000:
            if until_url and until_url in self.page.url:
                return True
            if until_selector:
                try:
                    if await self.is_present(until_selector):
                        return True
                except Exception:  # noqa: BLE001 — mid-navigation; retry next poll
                    pass
            await asyncio.sleep(poll_ms / 1000)
            waited += poll_ms
        return not (until_selector or until_url)

    async def settle(self, ms: int) -> None:
        """Extra idle wait after an action — for slow SPA transitions/XHR to land
        before the after-state is captured (per-step ``settle_ms`` in a flow)."""
        await self.page.wait_for_timeout(ms)

    def network_len(self) -> int:
        """Count of network calls captured so far (a mark for per-step deltas)."""
        return len(self._network)

    async def wait_for_api(
        self,
        path_contains: str,
        method: Optional[str] = None,
        since: int = 0,
        timeout_ms: int = 15000,
        poll_ms: int = 250,
    ) -> bool:
        """Wait until a network response matching ``path_contains`` (+ optional
        ``method``) appears after index ``since``, or ``timeout_ms`` elapses.

        For long-running actions (LLM/research calls) whose response lands after
        a fixed settle window — poll the captured network so the after-state's
        http delta actually includes the awaited response. Returns whether it
        arrived.
        """
        want = method.upper() if method else None
        waited = 0
        while True:
            for c in self._network[since:]:
                if path_contains in c.url and (
                    want is None or c.method.upper() == want
                ):
                    return True
            if waited >= timeout_ms:
                return False
            await self.page.wait_for_timeout(poll_ms)
            waited += poll_ms

    async def settle_popups(self, timeout_ms: int = 5000) -> None:
        """Load any new tabs opened by the last action and record their document status.

        A popup's original navigation response fires before a listener can attach
        (Playwright race), so we resolve the *settled* URL's status with a follow-up
        context request. Reliable for catching broken new-tab / ``target="_blank"``
        links, which the on-page checks otherwise miss entirely.
        """
        for page in self._popup_pages:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except (
                Exception
            ):  # noqa: BLE001  # nosec B110 - a popup that never settles is not fatal
                pass
            url = page.url
            if not url.startswith(("http://", "https://")):
                continue  # about:blank, mailto:, javascript: — nothing to fetch
            try:
                resp = await self.context.request.get(url, timeout=timeout_ms)
                self._popups.append(
                    NetworkCall(method="GET", url=resp.url, status=resp.status)
                )
            except (
                Exception
            ):  # noqa: BLE001 — a failed fetch is recorded as unreachable
                self._popups.append(NetworkCall(method="GET", url=url, status=0))
        self._popup_pages = []

    def opened_pages(self) -> List[NetworkCall]:
        """Document status for tabs/popups opened by an action (resolved in settle_popups)."""
        return list(self._popups)

    async def is_present(self, selector: str) -> bool:
        """Whether a selector currently resolves to an element in the DOM."""
        return await self.page.query_selector(selector) is not None

    async def screenshot(self, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(target))
        return str(target)

    # -- capture -----------------------------------------------------------

    def _console_delta(self) -> ConsoleDelta:
        return ConsoleDelta(
            errors=[m.text for m in self._console if m.level == "error"],
            warnings=[m.text for m in self._console if m.level in ("warning", "warn")],
        )

    async def capture_a11y(self) -> A11yReport:
        """Run the deterministic WCAG audit in the page context (spec §3d).

        Objective accessibility violations only (missing alt/label/lang, bad
        heading order, positive tabindex, duplicate id) — the semantic
        keyboard/screen-reader judgment stays with the agent.
        """
        raw = await self.page.evaluate(_A11Y_JS)
        return parse_a11y(raw, self.page.url)

    async def capture_snapshot(self) -> PageSnapshot:
        """Structured, ranked page inventory for the agent's intent inference."""
        raw = await self.page.evaluate(_SNAPSHOT_JS)
        interactive = [
            InteractiveElement(
                selector=e["selector"],
                role=e["role"],
                text=e["text"],
                kind=e["kind"],
                rank=e["rank"],
            )
            for e in raw["interactive"]
        ]
        forms = [
            FormInfo(
                selector=f["selector"],
                fields=[
                    FormField(
                        selector=x["selector"],
                        type=x["type"],
                        name=x["name"],
                        label=x["label"],
                    )
                    for x in f["fields"]
                ],
                submit=f["submit"],
                destructive=f["destructive"],
            )
            for f in raw["forms"]
        ]
        links = [
            LinkInfo(
                selector=lk["selector"],
                text=lk["text"],
                href=lk["href"],
                scheme=lk["scheme"],
                external=lk["external"],
                new_tab=(lk["target"] == "_blank"),
                dead=lk.get("dead", False),
            )
            for lk in raw["links"]
        ]
        incomplete = [
            IncompleteFeature(marker=m["marker"], label=m["label"])
            for m in raw.get("incomplete", [])
        ]
        return PageSnapshot(
            url=self.page.url,
            title=await self.page.title(),
            interactive=interactive,
            forms=forms,
            links=links,
            incomplete=incomplete,
            console=self._console_delta(),
            accessibility=await self.capture_a11y(),
        )

    async def _settle_for_capture(self, timeout_ms: int = 10000) -> None:
        """Let an in-flight navigation reach a milestone before reading the page.

        An action that navigates — a link, a form post, an OAuth hand-off to an
        external provider — can still be mid-flight when capture begins, and every
        ``page.evaluate()`` then dies with "Execution context was destroyed".
        Without this, `act` cannot capture ANY navigating interaction: it raises
        instead of returning the evidence bundle that describes where it went.
        """
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 — a page that never settles is not fatal
            pass

    async def capture_state(self) -> PageState:
        # Retry once: the navigation can commit *between* the settle and a later
        # evaluate, destroying the context mid-capture. A second pass runs against
        # the new document, which is the state the caller actually wants.
        last: Exception | None = None
        for _ in range(3):
            await self._settle_for_capture()
            try:
                return await self._capture_state_once()
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                retryable = (
                    "Execution context was destroyed" in text
                    or _UNSTABLE_CAPTURE in text
                )
                if not retryable:
                    raise
                last = exc
        raise RuntimeError(
            f"page kept changing during capture, no stable state to read: {last}"
        )

    async def _capture_state_once(self) -> PageState:
        # ONE evaluate for every document-scoped field. Reading them in separate
        # awaits let a navigation land between two of them, returning a PageState
        # stitched from two different documents — a silently wrong evidence bundle,
        # which is worse than the crash this replaced because the gate then judges
        # the wrong page and reports success. A single evaluate is atomic: it
        # either completes against one document or throws, and the caller retries.
        # Cookies are part of the snapshot (cookies_delta is derived from them),
        # so they must fall inside the same consistency boundary as the document.
        # Reading them once beside the evaluate is not enough: a login or redirect
        # that sets cookies mid-capture would pair OLD cookies with NEW page state.
        # Bracket the atomic page read and demand the set is unchanged across it;
        # if it moved, the whole capture is retried against a settled page.
        before = {
            c["name"]: str(c.get("value", "")) for c in await self.context.cookies()
        }
        snap = await self.page.evaluate(_STATE_JS)
        cookie_map = {
            c["name"]: str(c.get("value", "")) for c in await self.context.cookies()
        }
        if before != cookie_map:
            raise RuntimeError(_UNSTABLE_CAPTURE)
        return PageState(
            url=snap["url"],
            title=snap["title"],
            ready_state=snap["readyState"],
            console=list(self._console),
            network=list(self._network),
            page_errors=list(self._page_errors),
            focus=snap["focus"],
            cookies=cookie_map,
            dom_outline=snap["outline"],
            content=snap["content"],
        )


def _require(value: Optional[str], field_name: str) -> str:
    if not value:
        raise ValueError(f"Action is missing required field: {field_name!r}")
    return value
