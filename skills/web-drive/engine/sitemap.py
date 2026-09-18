"""Route-graph crawl (``map``) — the first half of "read + navigate".

This module only *navigates*: it visits routes and records what actually
happened. It does not read a page's declared surface (Phase B, ``read.py``) and
it makes no claim about what a route is *for* — the agent does that.

Design notes worth keeping:

* **Same-origin only.** Off-origin links are recorded in ``skipped`` rather than
  dropped, so a generated driver's coverage gap is visible instead of implied.
* **Auth is observed, not assumed.** A route that bounces to a login-ish URL, or
  answers 401/403, is marked ``required``. When the crawl runs *with* a session
  that signal is unavailable by construction (everything authenticates), so those
  routes are honestly marked ``unknown`` rather than guessed ``public``.
* **The cap is disclosed.** ``SiteMap.capped`` rides in the JSON, mirroring
  web-qa's rule that a truncated run must never read as a complete one.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse

from .browser import BrowserController
from .catalog import AuthState, RouteNode, SiteMap
from .models import Action, ActionType

# Path SEGMENTS that indicate a bounce to authentication. Matched per-segment,
# never as a substring: "/auth" as a substring also matches "/blog/auth-in-rails",
# which would mark an ordinary article as an auth-gated route.
_LOGIN_SEGMENTS = frozenset(
    {"login", "signin", "sign-in", "auth", "authenticate", "session", "sso"}
)


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)


def templatize(path: str) -> str:
    """Collapse a concrete path to its route TEMPLATE.

    ``/quiz/0b9ed04c-accd-...`` and ``/quiz/e3cb7b60-9b92-...`` are not two
    routes; they are one route with two arguments. Treating them as distinct is
    what makes a crawl of a content-heavy site unfinishable — twenty instances of
    one template consume the entire request budget while adding nothing a driver
    could use. The generated CLI wants `quiz show --id X`, not twenty URLs.
    """
    out: List[str] = []
    for seg in path.split("/"):
        if not seg:
            out.append(seg)
        elif _UUID_RE.match(seg):
            out.append("{uuid}")
        elif seg.isdigit():
            out.append("{int}")
        elif _HEX_RE.match(seg):
            out.append("{hex}")
        else:
            out.append(seg)
    return "/".join(out) or "/"


# How many distinct values to keep per query parameter. The point is the
# VOCABULARY (what values this filter accepts), and a facet with hundreds of tags
# would otherwise grow the map without teaching a driver anything more.
_MAX_PARAM_VALUES = 25


def query_params(url: str) -> Dict[str, List[str]]:
    """Parameter names and values from a URL's query string, order-independent."""
    return {k: list(v) for k, v in parse_qs(urlparse(url).query).items()}


def record_params(
    into: Dict[str, Dict[str, Any]], template: str, url: str
) -> None:
    """Accumulate the observed parameter vocabulary for one route template.

    A faceted browse page is ONE route with a parameter space, not N routes. What
    a generated driver needs from `/explore?category=romance&tags=Fantasy` is
    that `explore` accepts `category` and `tags`, and which values it has been
    seen to take -- so the values are collected as a set per name, and each is
    marked `truncated` once it stops being an exhaustive list.
    """
    bucket = into.setdefault(template, {})
    for name, values in query_params(url).items():
        slot = bucket.setdefault(name, {"values": [], "truncated": False})
        for value in values:
            if value in slot["values"]:
                continue
            if len(slot["values"]) >= _MAX_PARAM_VALUES:
                slot["truncated"] = True
                break
            slot["values"].append(value)


def origin_of(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def normalize(url: str) -> str:
    """Drop the fragment and any trailing slash so ``/a``, ``/a/`` and ``/a#x``
    are one route rather than three entries in the graph."""
    clean, _ = urldefrag(url)
    parts = urlparse(clean)
    path = parts.path.rstrip("/") or "/"
    q = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{path}{q}"


def same_origin(url: str, origin: str) -> bool:
    return origin_of(url) == origin


def looks_like_login(url: str) -> bool:
    segments = {s for s in urlparse(url).path.lower().split("/") if s}
    return bool(segments & _LOGIN_SEGMENTS)


def classify_auth(
    requested: str, final_url: str, status: int, with_session: bool
) -> AuthState:
    """Judge access from one visit's observable facts.

    With a session every route authenticates, so the discriminating signal is
    gone; saying ``public`` there would be a guess presented as an observation.
    """
    if status in (401, 403):
        return AuthState.REQUIRED
    if normalize(final_url) != normalize(requested) and looks_like_login(final_url):
        return AuthState.REQUIRED
    if with_session:
        return AuthState.UNKNOWN
    return AuthState.PUBLIC if 200 <= status < 400 else AuthState.UNKNOWN


def count_throttled(controller: BrowserController, since: int) -> int:
    """How many requests since ``since`` came back 429.

    Counts **every** captured request, not just the document. This is the whole
    trick: on the run that exposed this, the page documents kept returning 200
    while the SPA's data fetches were throttled — so a document-status check
    would have reported a perfectly healthy crawl of starved pages.
    """
    return sum(
        1 for c in controller._network[since:] if c.status == 429
    )  # noqa: SLF001


async def pace(
    controller: BrowserController, started: float, max_rpm: int, baseline: int
) -> None:
    """Hold the crawl to ``max_rpm`` REQUESTS per minute.

    Pacing per route is the wrong unit and was the tuning gap the first live
    re-run exposed: an asset-heavy SPA pulls ~15 requests per page, so ten
    "politely spaced" pages is still ~150 requests and trips a limiter that
    counts requests. This paces on what is actually being metered — the captured
    request count — so a heavy page earns a longer pause than a light one, with
    no need to know the page weight in advance.
    """
    if max_rpm <= 0:
        return
    made = controller.network_len() - baseline
    if made <= 0:
        return
    earned = made / (max_rpm / 60.0)  # seconds this many requests should have taken
    elapsed = time.monotonic() - started
    if earned > elapsed:
        await asyncio.sleep(earned - elapsed)


# Labels that suggest a control mutates state rather than navigates. Probing is
# about DISCOVERING ROUTES, so these are skipped by default even on a target you
# own: firing them would make the map's contents depend on what the crawl broke.
_MUTATING_WORDS = (
    "delete",
    "remove",
    "archive",
    "buy",
    "pay",
    "purchase",
    "upgrade",
    "cancel",
    "logout",
    "sign out",
    "submit",
    "save",
    "publish",
    "send",
    "generate",
    "run",
    "reset",
    "disconnect",
    "revoke",
    "clear",
)


def is_probe_safe(text: str) -> bool:
    """Whether a button looks like navigation rather than a state change."""
    t = (text or "").strip().lower()
    return bool(t) and not any(w in t for w in _MUTATING_WORDS)


async def _content_fingerprint(controller: BrowserController) -> str:
    """Cheap same-URL change detector for ``probe_buttons``.

    URL equality is not "nothing happened" on a single-page wizard/flow -- a
    step transition renders new content at the SAME address, which every
    URL-keyed check in this engine otherwise misses entirely. A full
    ``capture_state()`` is too costly to pay per button click (it is already
    the dominant cost of a button-heavy crawl, see ``probe_buttons``'s own
    docstring); title plus a short slice of visible text is enough to tell
    "this click changed the page" from "this click did nothing," which is the
    only distinction this needs to make. False negatives (a change this slice
    misses) just mean one more candidate the agent doesn't get a lead on --
    the same failure mode the rest of this module already accepts elsewhere.
    """
    try:
        return await controller.page.evaluate(
            "() => document.title + '|' + "
            "((document.body && document.body.innerText) || '').slice(0, 500)"
        )
    except Exception:  # noqa: BLE001 — a page mid-navigation has no stable content to read
        return ""


def _record_cross_template_outcome(
    label: str,
    outcome: str,
    seen_once: Dict[str, str],
    confirmed: Dict[str, str],
    varies: Set[str],
) -> None:
    """Track whether a label's outcome is stable across DIFFERENT templates.

    First occurrence just remembers what happened (`outcome`: a normalized
    destination URL, or `""` for "clicked, nothing navigated" -- a toggle or a
    modal). A second occurrence elsewhere that agrees promotes the label to
    `confirmed`, safe to reuse without clicking again. A second occurrence that
    disagrees marks it `varies` PERMANENTLY -- a "Next"/"View"-style label that
    means something different per page must never be trusted globally, even if
    a later occurrence would have coincidentally matched an earlier one.
    """
    if label in varies or label in confirmed:
        return
    prior = seen_once.get(label)
    if prior is None:
        seen_once[label] = outcome
        return
    if prior == outcome:
        confirmed[label] = outcome
    else:
        varies.add(label)
        seen_once.pop(label, None)


async def probe_buttons(
    controller: BrowserController,
    url: str,
    controls: List[Any],
    origin: str,
    probed_labels: Set[str],
    max_probes: Optional[int] = None,
    scope: str = "",
    pacer: Optional[Callable[[], Awaitable[None]]] = None,
    cross_template_seen: Optional[Dict[str, str]] = None,
    cross_template_confirmed: Optional[Dict[str, str]] = None,
    cross_template_varies: Optional[Set[str]] = None,
) -> Tuple[List[Tuple[str, str]], int, List[str], List[str], int, int]:
    """Click navigation-looking buttons to find routes no <a href> exposes.

    SPAs route through onClick handlers constantly -- an interstitial whose only
    control is an "Enter Dashboard" button is invisible to a link crawler, which
    is exactly how this app first mapped to a single route. Each candidate is
    clicked from a freshly re-navigated page so one button's side effects cannot
    contaminate the next one's result.

    That re-navigate is a full page load, and it is the dominant cost of a
    button-heavy crawl (measured live on content-jumpstart.com, 2026-09-16:
    ~8-9 requests per probe, most of them the SPA's own JS/CSS/data-fetch calls
    a fresh navigation re-triggers). A persistent app shell -- a header, a theme
    switcher, an AI-assistant toggle -- renders the SAME control on every route,
    so probing it once per template (the existing dedup below) still re-pays the
    full cost once per DIFFERENT template: 5 shell buttons across 19 distinct
    templates cost ~76 of those reloads for zero new routes (most shell controls
    are toggles/modals that never navigate at all).

    `cross_template_*`, when supplied, cache a label's outcome ACROSS templates:
    once the SAME label has produced the SAME outcome (a destination, or "no
    navigation") on two DIFFERENT route scopes, it is trusted and reused without
    a third click -- freeing its slot in `max_probes` for a page-specific control
    that would otherwise be starved (BUGS.md 2026-08-26). A label whose outcome
    DIFFERS between two scopes is marked permanently untrustworthy and always
    re-probed per template, same as today -- this cache only ever skips a click
    it has empirically verified is redundant, never guesses.

    Two more outcomes are tracked besides "found a route" (see catalog.py's
    `RouteNode.state_changing_controls` / `.gated_controls`): a click that left
    the URL unchanged but altered the page's own content (a same-URL wizard/flow
    step -- content-jumpstart.com's Project Wizard is exactly this shape), and a
    click that failed specifically because the element was present but not
    enabled (the disabled-button shape of a precondition-gated control). Neither
    was visible in the output AT ALL before -- both were silently indistinguishable
    from a true no-op toggle, which is how a fully exhaustive, zero-throttled
    crawl of content-jumpstart.com never surfaced its own multi-step wizard.
    Detection only: this never fills a combobox or retries a gated control to
    get past it -- that would mean the crawler starts guessing valid business
    data and chaining through mutating flows on its own, which is exactly the
    line "probes are safe by default" (CLAUDE.md) exists to hold.

    Returns ``(found, cache_hits, state_changes, gated_controls, controls_probed,
    controls_skipped_budget)``. The last two are BUGS.md 2026-08-26's disclosure
    fix: the budget is spent in DOM order (no ranking pass here), so a page whose
    primary CTA is control 77 of 78 can be starved by app-shell chrome before the
    crawl ever reaches it -- and the old return tuple had no way to say that
    happened. `controls_skipped_budget` counts probe-safe, not-yet-probed
    candidates the crawl declined to click purely because the budget ran out,
    computed via a lookahead at the exhaustion point rather than by removing the
    early `break` -- so `found`/`cache_hits`/ordering are unchanged from before,
    the count is additive-only.
    """
    found: List[Tuple[str, str]] = []
    state_changes: List[str] = []
    gated_controls: List[str] = []
    cache_hits = 0
    controls_probed = 0
    controls_skipped_budget = 0
    budget = max_probes
    for idx, ctl in enumerate(controls):
        if budget is not None and budget <= 0:
            for remaining in controls[idx:]:
                rlabel = (remaining.get("text") or "").strip().lower()
                if remaining.get("role") != "button" or not is_probe_safe(rlabel):
                    continue
                if f"{scope}|{rlabel}" in probed_labels:
                    continue
                controls_skipped_budget += 1
            break
        label = (ctl.get("text") or "").strip().lower()
        if ctl.get("role") != "button" or not is_probe_safe(label):
            continue
        # Dedup by (route template, label) rather than by label alone. A dashboard
        # nav renders on every route, so probing "Projects" once per page turns an
        # O(routes) crawl into O(routes x buttons) -- the thing that made the first
        # live attempt exceed ten minutes without finishing. But deduping GLOBALLY
        # overshot: a "Next" or "View" button means something different on every
        # template, and a global set meant the second template's copy was never
        # clicked. Scoping to the template keeps the nav-bar saving while letting
        # a shared label be followed once per page shape.
        key = f"{scope}|{label}"
        if key in probed_labels:
            continue

        if cross_template_confirmed is not None and label in cross_template_confirmed:
            # Empirically confirmed stable on >=2 other templates already --
            # reuse it instead of paying another reload to re-learn the same
            # fact. Does not spend `max_probes` budget: a control we already
            # know about should not crowd out one we do not.
            probed_labels.add(key)
            cache_hits += 1
            dest = cross_template_confirmed[label]
            if dest:
                found.append((dest, f"button:{ctl.get('text','')[:40]}[cached]"))
            continue

        probed_labels.add(key)
        controls_probed += 1
        if budget is not None:
            budget -= 1
        try:
            if pacer is not None:
                await pacer()
            await controller.navigate(url)
            before = controller.page.url
            before_content = await _content_fingerprint(controller)
            await controller.perform(
                Action(type=ActionType.CLICK, selector=ctl["selector"])
            )
            await controller.settle(900)
            after = controller.page.url
            same_url = normalize(after) == normalize(before)
            outcome = (
                normalize(after) if (not same_url and same_origin(after, origin)) else ""
            )
            if outcome:
                found.append((outcome, f"button:{ctl.get('text','')[:40]}"))
            elif same_url:
                # No navigation is NOT "nothing happened" on a single-page flow --
                # a wizard step advances the SAME url. Without this check that
                # click is indistinguishable from a true no-op toggle and vanishes
                # from the map entirely (see this function's docstring).
                after_content = await _content_fingerprint(controller)
                if after_content != before_content:
                    state_changes.append(f"button:{ctl.get('text','')[:40]}")
            if (
                cross_template_seen is not None
                and cross_template_confirmed is not None
                and cross_template_varies is not None
            ):
                _record_cross_template_outcome(
                    label, outcome, cross_template_seen, cross_template_confirmed,
                    cross_template_varies,
                )
        except Exception as exc:  # noqa: BLE001 — a button that will not click is not a route
            if "not enabled" in str(exc):
                # Playwright's own actionability wait found the element present
                # but disabled -- the shape of a control gated behind state this
                # isolated, freshly-reloaded probe never provided (a wizard
                # "Continue" before its combobox is filled). Recorded distinctly
                # from every other click failure, which stays silent here exactly
                # as before: naming what a control is gated ON is the agent's job.
                gated_controls.append(f"button:{ctl.get('text','')[:40]}")
            continue
    return found, cache_hits, state_changes, gated_controls, controls_probed, controls_skipped_budget


# Values safe to type into a discovery form. Nothing here should read as real
# user data if it lands in someone's database.
_FILL_VALUES = {
    "email": "qa-probe@example.com",
    "search": "test",
    "number": "1",
    "tel": "5555550100",
    "url": "https://example.com",
}


async def probe_forms(
    controller: BrowserController,
    url: str,
    forms: List[Any],
    origin: str,
    probed: Set[str],
    pacer: Optional[Callable[[], Awaitable[None]]] = None,
    scope: str = "",
) -> List[Tuple[str, str]]:
    """Fill and submit non-destructive forms to reach what lies behind them.

    Search boxes, filters and "continue" gates hide whole sections from a link
    crawler. Filling them is how you find those sections.

    Three hard limits, because a discovery crawl must not become a write:
    a form the snapshot marks ``destructive`` is skipped; any form containing a
    password field is skipped (that is a login, and guessing at one is both
    useless and hostile); and values are obviously synthetic so anything that
    does persist is identifiable as a probe.

    Dedup by ``(route template, submit-selector, field-count)`` rather than by
    signature alone — the same asymmetry `probe_buttons` had before its
    per-template tier was added (BUGS.md/plan 2026-09-16, WD-P1): a form that
    repeats identically across N instances of one template (a per-category
    "Filter by tag" form on `/category/{id}`) would otherwise be submitted on
    the FIRST instance only and silently skipped on every later one, even
    though the filter's effect is instance-specific. Scoping to the template
    keeps a genuinely page-invariant form (probed once) from being confused
    with a per-instance one (probed once per shape), the same tradeoff
    `probe_buttons` already makes for buttons.
    """
    found: List[Tuple[str, str]] = []
    for form in forms:
        if form.get("destructive"):
            continue
        fields = form.get("fields") or []
        if any((f.get("type") or "") == "password" for f in fields):
            continue
        sig = f"{scope}|{form.get('submit')}|{len(fields)}"
        if not form.get("submit") or sig in probed:
            continue
        probed.add(sig)
        try:
            if pacer is not None:
                await pacer()
            await controller.navigate(url)
            before = controller.page.url
            for f in fields:
                ftype = (f.get("type") or "text").lower()
                if ftype in ("hidden", "submit", "button", "file", "checkbox", "radio"):
                    continue
                await controller.perform(
                    Action(
                        type=ActionType.FILL,
                        selector=f["selector"],
                        value=_FILL_VALUES.get(ftype, "test"),
                    )
                )
            await controller.perform(
                Action(type=ActionType.CLICK, selector=form["submit"])
            )
            await controller.settle(1200)
            after = controller.page.url
            if normalize(after) != normalize(before) and same_origin(after, origin):
                found.append((normalize(after), f"form:{form.get('submit','')[:34]}"))
        except Exception:  # noqa: BLE001 — a form that will not submit is not a route
            continue
    return found


class _Admission:
    """Decides whether a discovered URL earns a page load.

    One place, used by the link path and the button/form path alike, because the
    two diverging is exactly how per-template sampling ended up inactive on the
    sites `--probe-buttons` exists for.

    Two orthogonal questions, deliberately counted separately:

    * how many concrete instances of a PATH template to walk
      (`/storylines/{hex}` had 129) -- ``max_per_template``;
    * how many QUERY variants of the same path to walk (`/explore` had 37
      filter combinations) -- ``max_query_variants``.

    The second exists because a faceted browse page is combinatorial: every
    filter chip mints a URL, so an uncapped crawl of one never converges. It is
    ONE route with a parameter space, and the vocabulary is what a driver wants.
    """

    def __init__(
        self,
        max_per_template: Optional[int],
        max_query_variants: Optional[int],
        template_seen: Dict[str, int],
        collapsed: Dict[str, int],
        variants: Dict[str, Set[str]],
        variants_collapsed: Dict[str, int],
        params: Dict[str, Dict[str, Any]],
    ) -> None:
        self.max_per_template = max_per_template
        self.max_query_variants = max_query_variants
        self.template_seen = template_seen
        self.collapsed = collapsed
        self.variants = variants
        self.variants_collapsed = variants_collapsed
        self.params = params

    def admits(self, target: str) -> bool:
        """True to queue ``target``; False having COUNTED why it was declined."""
        parts = urlparse(target)
        tmpl = templatize(parts.path or "/")

        if parts.query:
            # Recorded before any decision: the parameter vocabulary is the
            # product here, and it must not depend on whether this particular
            # variant happened to fall inside the sample.
            record_params(self.params, tmpl, target)
            seen_variants = self.variants.setdefault(tmpl, set())
            seen_variants.add(parts.query)
            if (
                self.max_query_variants is not None
                and len(seen_variants) > self.max_query_variants
            ):
                self.variants_collapsed[tmpl] = (
                    self.variants_collapsed.get(tmpl, 0) + 1
                )
                return False

        self.template_seen[tmpl] = self.template_seen.get(tmpl, 0) + 1
        if (
            self.max_per_template is not None
            and self.template_seen[tmpl] > self.max_per_template
        ):
            self.collapsed[tmpl] = self.collapsed.get(tmpl, 0) + 1
            return False
        return True


async def crawl(
    controller: BrowserController,
    entry_url: str,
    max_pages: Optional[int] = None,
    max_depth: Optional[int] = None,
    with_session: bool = False,
    delay_ms: int = 250,
    max_retries: int = 3,
    backoff_ms: int = 2000,
    max_rpm: int = 120,
    max_per_template: Optional[int] = None,
    max_query_variants: Optional[int] = 3,
    probe_buttons_enabled: bool = False,
    fill_forms_enabled: bool = False,
    max_probes: Optional[int] = None,
    deadline: Optional[float] = None,
    resume: Optional[dict] = None,
) -> SiteMap:
    """Breadth-first walk of the same-origin route graph from ``entry_url``.

    **Traversal is unbounded by default.** ``max_pages``, ``max_depth``,
    ``max_per_template`` and ``max_probes`` all accept ``None`` meaning "no
    limit", and that is the default. What actually terminates a crawl is the
    structure of the site itself: same-origin only, and every URL visited at most
    once. The coverage caps remain available for a deliberately partial run, but
    a cap silently applied is how a map comes back looking complete while
    describing a fraction of an app.

    The real guard is ``deadline`` (a ``time.monotonic()`` timestamp). Wall clock
    bounds a crawl without deciding in advance which parts of the site matter,
    which is exactly what a page or depth cap does. Hitting it is not data loss:
    the frontier travels in the output, so the run resumes where it stopped.

    Rate-limit behaviour (added after a live run exhausted a real app's quota):

    * ``delay_ms`` is paused between routes — **on by default**. A crawler that
      runs as fast as the browser allows will trip any real app's limiter.
    * If a route's requests include a 429 it is **retried** after an exponential
      backoff, because the first throttled response is usually recoverable.
    * If it is still throttled after ``max_retries``, the crawl **stops** and
      says so. Continuing would append rows for pages the crawler itself
      starved, and a throttled page is indistinguishable from an empty one —
      so the honest move is fewer routes, not more untrustworthy ones.
    """
    started = time.monotonic()
    baseline = controller.network_len()

    if resume:
        # Continue a crawl that stopped (limiter or cap) instead of re-walking
        # what we already paid for -- the whole point when the target is the
        # scarce resource.
        site = SiteMap.resume_from(resume)
        origin = site.origin
        entry_url = site.entry_url
        with_session = site.with_session
        queue: List[Tuple[str, int, str]] = [
            (f[0], int(f[1]), str(f[2])) for f in site.frontier
        ]
        seen: Set[str] = {normalize(r.url) for r in site.routes} | {q[0] for q in queue}
        skipped_seen: Set[str] = {s_["url"] for s_ in site.skipped}
        site.frontier = []
        template_seen = dict(resume.get("_template_seen", {}))
        collapsed = dict(resume.get("_collapsed", {}))
        variants = {k: set(v) for k, v in resume.get("_variants", {}).items()}
        variants_collapsed = dict(resume.get("_variants_collapsed", {}))
        params = {k: dict(v) for k, v in resume.get("_params", {}).items()}
        # A resumed run re-reports its own trust flags; a stale `rate_limited`
        # from the previous leg would mislabel a clean continuation.
        site.rate_limited = False
        site.stopped_reason = None
    else:
        origin = origin_of(entry_url)
        site = SiteMap(entry_url=entry_url, origin=origin, with_session=with_session)
        start = normalize(entry_url)
        queue = [(start, 0, "entry")]
        seen = {start}
        skipped_seen = set()
        template_seen = {}
        collapsed = {}
        variants = {}
        variants_collapsed = {}
        params = {}
    probed_labels: Set[str] = set()
    probed_forms: Set[str] = set()
    # Cross-template button-outcome cache (see `probe_buttons`'s docstring). Not
    # carried across --resume legs, same as the timing counters: re-establishing
    # confidence after a gap is cheap (two clicks) and safer than trusting a
    # cache built in a prior process.
    cross_template_seen: Dict[str, str] = {}
    cross_template_confirmed: Dict[str, str] = {}
    cross_template_varies: Set[str] = set()
    # Distinct same-origin targets reached via <a href> this leg. Kept separate
    # from `seen` (which also holds button/form finds and the entry) so the
    # navigation heuristic below measures the LINK mechanism specifically.
    link_targets: Set[str] = set()
    gate = _Admission(
        max_per_template, max_query_variants,
        template_seen, collapsed, variants, variants_collapsed, params,
    )
    first = True
    fetched = 0  # routes actually visited THIS leg (resumed ones were not)

    while queue:
        if max_pages is not None and len(site.routes) >= max_pages:
            # More was reachable than we visited — say so rather than implying
            # the graph is complete.
            site.capped = True
            break
        if deadline is not None and time.monotonic() >= deadline:
            # The one guard that does not pre-judge which parts of a site matter.
            # Disclosed like `capped`, and resumable: the frontier rides along.
            site.timed_out = True
            site.stopped_reason = (
                f"time budget reached with {len(queue)} route(s) still queued. "
                f"This map is PARTIAL -- re-run with --resume to continue from "
                f"the saved frontier, or raise --time-budget-s."
            )
            break
        url, depth, viaction = queue.pop(0)

        node = RouteNode(
            path=urlparse(url).path or "/",
            url=url,
            final_url=url,
            status=0,
            depth=depth,
            reached_by=[viaction],
        )
        # Be a polite client. `pace` is the primary control (requests/minute,
        # the unit limiters actually meter); delay_ms is a small floor so two
        # cheap pages in a row still leave a gap.
        if not first:
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
            await pace(controller, started, max_rpm, baseline)
        first = False

        links: List[Any] = []
        for attempt in range(max_retries + 1):
            links = []
            net_mark = controller.network_len()
            try:
                await controller.navigate(url)
                snapshot = await controller.capture_snapshot()
                node.final_url = snapshot.url
                node.title = snapshot.title
                node.redirected = normalize(snapshot.url) != normalize(url)
                node.status = _status_for(controller, snapshot.url)
                node.auth = classify_auth(url, snapshot.url, node.status, with_session)
                links = list(snapshot.links)
                node.controls = [
                    {
                        "role": e.role,
                        "text": e.text,
                        "selector": e.selector,
                        "rank": e.rank,
                        "kind": getattr(e, "kind", None),
                    }
                    for e in snapshot.interactive
                ]
                node.forms = [
                    {
                        "submit": f.submit,
                        "destructive": f.destructive,
                        "fields": [
                            {
                                "selector": x.selector,
                                "type": x.type,
                                "name": x.name,
                                "label": x.label,
                            }
                            for x in f.fields
                        ],
                    }
                    for f in snapshot.forms
                ]
                node.error = None
            except (
                Exception
            ) as exc:  # noqa: BLE001 — one bad route must not sink the crawl
                node.error = str(exc)

            if not count_throttled(controller, net_mark):
                if attempt > 0:
                    site.rate_limit.recovered_after_retry = True
                node.throttled = False
                break

            # Throttled. Record when it first happened -- this is the crawl
            # MEASURING the limiter rather than assuming it, so a later run can
            # pick a real budget instead of a guess.
            if site.rate_limit.first_throttle_after_requests is None:
                site.rate_limit.first_throttle_after_requests = (
                    controller.network_len() - baseline
                )
                site.rate_limit.first_throttle_after_s = time.monotonic() - started
            node.throttled = True
            if attempt < max_retries:
                await asyncio.sleep((backoff_ms * (2**attempt)) / 1000)

        if node.throttled:
            site.rate_limited = True
            site.throttled_routes += 1
            site.stopped_reason = (
                f"rate limited at {node.url} — still 429 after {max_retries} "
                f"backoff retries. Stopped rather than recording routes the crawl "
                f"itself starved; re-run with a larger --delay-ms."
            )
            site.routes.append(node)
            fetched += 1
            break

        # Rebase the same-origin baseline onto where the ENTRY actually LANDED.
        # A bare-domain -> www redirect (quizsquirrel.com -> www.quizsquirrel.com)
        # or http -> https is extremely common; keeping the *requested* origin
        # would make every link on the landed page read as off-origin, and the
        # crawl would silently return a one-route graph for a whole site.
        if depth == 0 and not node.error:
            landed = origin_of(node.final_url)
            if landed != origin:
                origin = landed
                site.origin = landed
                seen.add(normalize(node.final_url))

        # Appended exactly once, on both paths: appending inside the try AND in
        # the handler would list a route twice when a link raised mid-loop.
        site.routes.append(node)
        fetched += 1
        if node.error or (max_depth is not None and depth >= max_depth):
            continue

        async def pacer() -> None:
            """Probing performs full page loads INSIDE a route, and those were
            never metered -- `pace()` ran once per route, so the request budget
            was bypassed in proportion to the probe count. The uncapped run
            reported 184 rpm against a 120 budget; the tool was announcing a rate
            it did not honour."""
            await pace(controller, started, max_rpm, baseline)

        discovered: List[Tuple[str, str]] = []
        if probe_buttons_enabled and node.controls:
            (
                button_finds,
                cache_hits,
                state_changes,
                gated_controls,
                controls_probed,
                controls_skipped_budget,
            ) = await probe_buttons(
                controller,
                node.final_url,
                node.controls,
                origin,
                probed_labels,
                max_probes=max_probes,
                scope=templatize(node.path),
                pacer=pacer,
                cross_template_seen=cross_template_seen,
                cross_template_confirmed=cross_template_confirmed,
                cross_template_varies=cross_template_varies,
            )
            site.probe_cache_hits += cache_hits
            node.state_changing_controls = state_changes
            node.gated_controls = gated_controls
            node.controls_probed = controls_probed
            node.controls_skipped_budget = controls_skipped_budget
            for tgt, via in button_finds:
                if tgt not in seen:
                    discovered.append((tgt, via))

        if fill_forms_enabled and node.forms:
            for tgt, via in await probe_forms(
                controller, node.final_url, node.forms, origin, probed_forms,
                pacer=pacer, scope=templatize(node.path),
            ):
                if tgt not in seen:
                    discovered.append((tgt, via))

        # Button and form finds go through the SAME admission gate as links.
        # They did not, and per-template sampling was therefore inactive exactly
        # on the sites --probe-buttons exists for: the isekaizero run walked five
        # instances of one character template under a cap of three, and reported
        # `collapsed: 0`, so the output could not reveal that sampling was skipped.
        for tgt, via in discovered:
            if tgt in seen or not gate.admits(tgt):
                continue
            seen.add(tgt)
            queue.append((tgt, depth + 1, via))

        for link in links:
            target, reason = _resolve(link, node.final_url, origin)
            if target is None:
                continue  # dead link (href="#"/javascript:) — nothing to visit
            if reason:
                if target not in skipped_seen:
                    skipped_seen.add(target)
                    site.skipped.append({"url": target, "reason": reason})
                continue
            if target in seen:
                continue
            # Sample a bounded number of instances per route template. The rest
            # are counted, not crawled: the map records that the template has N
            # instances, which is the fact a driver needs, without paying N page
            # loads to learn one page shape.
            # Counted BEFORE the per-template cap, and de-duped by target rather
            # than by increment, because this number answers "does this site
            # navigate by link?" -- a question our own sampling policy must not
            # be allowed to answer for it. isekaizero found 140 storyline links
            # and deliberately walked 3; counting only the walked ones reported
            # `link_discoveries: 3` and blamed the site for a cap we imposed.
            if target not in link_targets:
                link_targets.add(target)
                site.link_discoveries += 1
            if not gate.admits(target):
                continue
            seen.add(target)
            queue.append((target, depth + 1, f"link:{link.text or link.href}"))

    # Whatever is still queued travels with the result, so this map can be
    # handed straight back via --resume.
    site.frontier = [list(q) for q in queue]
    site.rate_limit.requests_total = controller.network_len() - baseline
    site.rate_limit.elapsed_s = time.monotonic() - started
    if site.rate_limit.elapsed_s > 0:
        site.rate_limit.effective_rpm = (
            site.rate_limit.requests_total / site.rate_limit.elapsed_s * 60.0
        )
    if fetched:
        # Divide by routes fetched on THIS leg, not the whole map. A resumed run
        # carries routes it never re-requested; counting them understates the
        # per-route cost -- the one number a caller uses to choose --max-rpm.
        site.rate_limit.requests_per_route = site.rate_limit.requests_total / fetched
    site.rate_limit.throttled_requests = count_throttled(controller, baseline)
    visited: dict[str, int] = {}
    for r in site.routes:
        t = templatize(r.path)
        visited[t] = visited.get(t, 0) + 1
    site.templates = [
        {
            "template": t,
            "visited": visited.get(t, 0),
            "instances_seen": template_seen.get(t, visited.get(t, 0)),
            "collapsed": collapsed.get(t, 0),
            # A faceted page is one route with a parameter space. `variants_seen`
            # is how many distinct query strings were observed, `variants_walked`
            # how many earned a page load, and `params` the vocabulary a driver
            # needs to construct its own -- `explore --category romance` rather
            # than a list of 37 URLs nobody can generalize from.
            "variants_seen": len(variants.get(t, ())),
            "variants_collapsed": variants_collapsed.get(t, 0),
            "params": params.get(t, {}),
        }
        for t in sorted(set(visited) | set(template_seen) | set(variants))
    ]
    site.buttons_seen = sum(
        1 for r in site.routes for c in r.controls if c.get("role") == "button"
    )
    # Say it plainly when the map is probably a fraction of the site. A crawl
    # that exhausts its frontier after three routes on a page carrying dozens of
    # buttons has not mapped the app; it has run out of the ONE mechanism it
    # understands, and reporting `frontier: 0` without this reads as success.
    # Two distinct shapes of "the link mechanism is exhausted, not the site".
    #  * zero yield with buttons present -- a gate page whose only control is a
    #    button (content-jumpstart's interstitial mapped to ONE route this way);
    #  * heavy button surface with negligible link yield -- isekaizero returned 3
    #    routes off 230 buttons, and reported frontier 0 as though complete.
    # Thresholds are deliberately conservative: a site that genuinely navigates
    # by link always clears them, and a warning that cries wolf gets ignored.
    heavy = site.buttons_seen >= 20 and site.link_discoveries * 10 < site.buttons_seen
    barren = site.buttons_seen >= 3 and site.link_discoveries == 0
    if (heavy or barren) and not probe_buttons_enabled:
        site.navigation_hint = (
            f"LIKELY INCOMPLETE: {site.buttons_seen} button(s) across "
            f"{len(site.routes)} route(s), but only {site.link_discoveries} "
            f"route(s) came from <a href>. This site probably navigates by "
            f"button/onClick, which link crawling cannot see -- so `frontier: 0` "
            f"means the link mechanism is exhausted, NOT that the site is mapped. "
            f"Re-run with --probe-buttons (add --fill-forms if sections sit "
            f"behind search/filter gates)."
        )
    site.asset_blocking = getattr(controller, "asset_blocking", "off")
    site.collapsed_routes = sum(collapsed.values())
    site._template_seen = template_seen
    site._collapsed = collapsed
    # Carried so a --resume leg keeps counting from where the last one stopped;
    # restarting these would let a resumed crawl re-walk a facet space it had
    # already sampled, which is the exact cost resuming exists to avoid.
    site._variants = {k: sorted(v) for k, v in variants.items()}
    site._variants_collapsed = variants_collapsed
    site._params = params
    return site


def _resolve(link, base_url: str, origin: str) -> Tuple[Optional[str], Optional[str]]:
    """Map a snapshot link to ``(url, skip_reason)``.

    Three outcomes: ``(url, None)`` crawl it, ``(url, reason)`` record it as
    skipped, ``(None, None)`` ignore it entirely — a dead ``href="#"`` is not a
    coverage gap, so reporting it as skipped would be noise. Callers must test
    ``target is None`` *before* testing ``reason``.
    """
    if link.dead:
        return None, None  # href="#"/javascript: — nothing to visit, nothing lost
    if link.scheme in ("mailto", "tel"):
        return link.href, "non-http scheme"
    absolute = normalize(urljoin(base_url, link.href))
    if not absolute.startswith(("http://", "https://")):
        return absolute, "non-http scheme"
    if not same_origin(absolute, origin):
        return absolute, "off-origin"
    return absolute, None


def _status_for(controller: BrowserController, final_url: str) -> int:
    """Document status for the settled URL, read from the captured network log.

    Scans newest-first: a route revisited during the crawl (a nav bar link back
    to the dashboard) appears more than once, and the latest entry is this
    visit's.
    """
    target = normalize(final_url)
    for call in reversed(controller._network):  # noqa: SLF001 — same package
        if normalize(call.url) == target:
            return call.status
    return 0
