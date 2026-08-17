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
import time
from typing import Any, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

from .browser import BrowserController
from .catalog import AuthState, RouteNode, SiteMap

# Path SEGMENTS that indicate a bounce to authentication. Matched per-segment,
# never as a substring: "/auth" as a substring also matches "/blog/auth-in-rails",
# which would mark an ordinary article as an auth-gated route.
_LOGIN_SEGMENTS = frozenset(
    {"login", "signin", "sign-in", "auth", "authenticate", "session", "sso"}
)


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


async def crawl(
    controller: BrowserController,
    entry_url: str,
    max_pages: int = 40,
    max_depth: int = 3,
    with_session: bool = False,
    delay_ms: int = 250,
    max_retries: int = 3,
    backoff_ms: int = 2000,
    max_rpm: int = 120,
    resume: Optional[dict] = None,
) -> SiteMap:
    """Breadth-first walk of the same-origin route graph from ``entry_url``.

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
    first = True
    fetched = 0  # routes actually visited THIS leg (resumed ones were not)

    while queue:
        if len(site.routes) >= max_pages:
            # More was reachable than we visited — say so rather than implying
            # the graph is complete.
            site.capped = True
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
        if node.error or depth >= max_depth:
            continue

        for link in links:
            target, reason = _resolve(link, node.final_url, origin)
            if target is None:
                continue  # dead link (href="#"/javascript:) — nothing to visit
            if reason:
                if target not in skipped_seen:
                    skipped_seen.add(target)
                    site.skipped.append({"url": target, "reason": reason})
                continue
            if target not in seen:
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
