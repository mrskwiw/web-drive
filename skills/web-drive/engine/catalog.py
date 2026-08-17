"""web-drive's own data models.

These live here rather than in ``models.py`` on purpose. ``models.py`` is a
byte-identical copy of web-qa's (spec section 4.3) and stays that way so a
`diff` between the two engines remains the porting tool; every web-drive-specific
type therefore goes in this module instead.

Serialization follows the house pattern: an explicit ``to_dict()`` that converts
enums to ``.value`` and recurses into nested dataclasses, because ``asdict``
would leave enum objects in the JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AuthState(str, Enum):
    """What a crawl observed about a route's access control.

    Deliberately observational, not a verdict: the engine records what happened,
    the agent decides what it means (the deterministic/judgment split the whole
    skill family is built on).
    """

    PUBLIC = "public"  # reached and rendered without a session
    REQUIRED = "required"  # bounced to a login route, or 401/403
    UNKNOWN = "unknown"  # reached with a session, or an inconclusive response


@dataclass
class RouteNode:
    """One route as actually visited — never as merely advertised."""

    path: str
    url: str
    final_url: str
    status: int
    title: str = ""
    depth: int = 0
    auth: AuthState = AuthState.UNKNOWN
    redirected: bool = False
    reached_by: List[str] = field(default_factory=list)
    error: Optional[str] = None
    # True when any request this route triggered came back 429. The row is then
    # SUSPECT: a throttled page renders empty, which is indistinguishable from a
    # genuinely empty one, so consumers must not treat it as observed truth.
    throttled: bool = False
    # What you can DO here. The snapshot is already taken to find links, so
    # keeping its controls costs no extra request and turns a route list into a
    # map you can actually walk: every route with its addressable controls.
    controls: List[Dict[str, Any]] = field(default_factory=list)
    forms: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "title": self.title,
            "depth": self.depth,
            "auth": self.auth.value,
            "redirected": self.redirected,
            "reached_by": list(self.reached_by),
            "error": self.error,
            "throttled": self.throttled,
            "controls": list(self.controls),
            "forms": list(self.forms),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteNode":
        """Rebuild a route recorded by an earlier run (see SiteMap.resume_from)."""
        return cls(
            path=data["path"],
            url=data["url"],
            final_url=data.get("final_url", data["url"]),
            status=data.get("status", 0),
            title=data.get("title", ""),
            depth=data.get("depth", 0),
            auth=AuthState(data.get("auth", "unknown")),
            redirected=data.get("redirected", False),
            reached_by=list(data.get("reached_by", [])),
            error=data.get("error"),
            throttled=data.get("throttled", False),
            controls=list(data.get("controls", [])),
            forms=list(data.get("forms", [])),
        )


@dataclass
class RateLimitProfile:
    """What the crawl LEARNED about the target's limiter, not what we assumed.

    Emitted on every run so a later crawl (or a human) can pick a sane budget
    instead of guessing. The key number is requests, not pages: an asset-heavy
    SPA pulls ~15 requests per page, so a per-page pause tells you almost
    nothing about whether you are about to be throttled.
    """

    requests_total: int = 0
    elapsed_s: float = 0.0
    effective_rpm: float = 0.0
    throttled_requests: int = 0
    requests_per_route: float = 0.0
    first_throttle_after_requests: Optional[int] = None
    first_throttle_after_s: Optional[float] = None
    recovered_after_retry: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requests_total": self.requests_total,
            "elapsed_s": round(self.elapsed_s, 1),
            "effective_rpm": round(self.effective_rpm, 1),
            "requests_per_route": round(self.requests_per_route, 1),
            "throttled_requests": self.throttled_requests,
            "first_throttle_after_requests": self.first_throttle_after_requests,
            "first_throttle_after_s": (
                round(self.first_throttle_after_s, 1)
                if self.first_throttle_after_s is not None
                else None
            ),
            "recovered_after_retry": self.recovered_after_retry,
        }


@dataclass
class SiteMap:
    """The route graph produced by ``map`` — the input to every later phase."""

    entry_url: str
    origin: str
    with_session: bool = False
    routes: List[RouteNode] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    capped: bool = False
    # Set when the target rate-limited us. Like `capped`, this is a statement
    # about the TRUSTWORTHINESS of the data, not a log line — a consumer that
    # ignores it can build a driver from routes the crawler itself starved.
    rate_limited: bool = False
    throttled_routes: int = 0
    stopped_reason: Optional[str] = None
    rate_limit: RateLimitProfile = field(default_factory=RateLimitProfile)
    # Routes still queued when the crawl stopped. Carrying the frontier makes the
    # sitemap its own resume token: a run halted by a limiter or a cap can be
    # continued instead of restarted, which matters most precisely when the
    # target is rate-limited and re-walking what you already have is expensive.
    frontier: List[List[Any]] = field(default_factory=list)
    # Route templates observed. `collapsed` counts instances deliberately NOT
    # crawled once the per-template sample was met -- disclosed, like `capped`,
    # because "we saw 20 of these and walked 3" is a different claim from
    # "there are 3 of these".
    templates: List[Dict[str, Any]] = field(default_factory=list)
    collapsed_routes: int = 0
    _template_seen: Dict[str, int] = field(default_factory=dict)
    _collapsed: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_url": self.entry_url,
            "origin": self.origin,
            "with_session": self.with_session,
            "route_count": len(self.routes),
            # `capped` must travel with the data: a truncated crawl that looks
            # complete is how a generated driver silently omits half a site.
            "capped": self.capped,
            "rate_limited": self.rate_limited,
            "throttled_routes": self.throttled_routes,
            "stopped_reason": self.stopped_reason,
            "rate_limit": self.rate_limit.to_dict(),
            "frontier": [list(f) for f in self.frontier],
            "templates": list(self.templates),
            "collapsed_routes": self.collapsed_routes,
            "_template_seen": dict(self._template_seen),
            "_collapsed": dict(self._collapsed),
            "routes": [r.to_dict() for r in self.routes],
            "skipped": list(self.skipped),
        }

    @classmethod
    def resume_from(cls, data: Dict[str, Any]) -> "SiteMap":
        """Rebuild a partial crawl so it can be continued.

        Only the fields a continuation needs are restored; counters that describe
        *this* run (timings, effective rpm) start fresh, because averaging them
        across a gap of unknown length would produce a meaningless rate.
        """
        site = cls(
            entry_url=data["entry_url"],
            origin=data["origin"],
            with_session=data.get("with_session", False),
        )
        site.routes = [RouteNode.from_dict(r) for r in data.get("routes", [])]
        site.skipped = list(data.get("skipped", []))
        site.frontier = [list(f) for f in data.get("frontier", [])]
        site._template_seen = dict(data.get("_template_seen", {}))
        site._collapsed = dict(data.get("_collapsed", {}))
        return site
