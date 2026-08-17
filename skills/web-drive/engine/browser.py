"""Headless-browser driver + evidence capture (Phase A).

``BrowserController`` wraps Playwright's async API. Its job is narrow: launch a
browser, perform one requested :class:`~engine.models.Action`, and capture a full
:class:`~engine.models.PageState`. It makes no judgments — deterministic gating
(Phase B) and semantic judgment (the agent) live elsewhere.
"""

from __future__ import annotations

import json
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
  return t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 4000);
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
    const text = (f.innerText || '').toLowerCase();
    // A password field alone is NOT destructive — a login is idempotent auth,
    // not a record-creating action. Flag on destructive wording, or on a
    // password field that is NOT on a login form (likely registration / set-password).
    const DESTRUCTIVE = /\b(pay|checkout|purchase|place order|delete|remove|cancel account|unsubscribe|sign\s*up|register|create account)\b/;
    const LOGIN = /\b(log\s*in|sign\s*in)\b/;
    const hasPassword = fields.some((x) => x.type === 'password');
    const destructive = DESTRUCTIVE.test(text) || (hasPassword && !LOGIN.test(text));
    forms.push({ selector: sel(f), fields, submit: submitEl ? sel(submitEl) : null, destructive });
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
    ) -> None:
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
        self._browser = await browser_type.launch(
            headless=self._headless, slow_mo=self._slowmo
        )
        ctx_kwargs: dict = {"viewport": self._viewport}
        if self._user_agent:
            ctx_kwargs["user_agent"] = self._user_agent
        if self._storage_state is not None:
            # Playwright accepts either a state dict or a path to a state file.
            ctx_kwargs["storage_state"] = self._storage_state
        self._context = await self._browser.new_context(**ctx_kwargs)
        self._page = await self.context.new_page()
        self.page.set_default_timeout(self._timeout)
        self._wire_listeners()

    async def save_session(self, path: str, user_agent: Optional[str] = None) -> str:
        """Persist the live context's auth session (cookies + localStorage) plus the
        user-agent to a session-bundle JSON, for replay by a later run via ``--session``.

        Call this while the context is still open (e.g. at the end of a login flow),
        NOT after ``close()``. The recorded user-agent matters: a fingerprint-bound
        token is only valid when replayed under the same user-agent.
        """
        state = await self.context.storage_state()
        bundle = {
            "user_agent": user_agent or self._user_agent,
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
            await self.page.fill(
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
