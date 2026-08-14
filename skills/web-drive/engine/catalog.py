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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_url": self.entry_url,
            "origin": self.origin,
            "with_session": self.with_session,
            "route_count": len(self.routes),
            # `capped` must travel with the data: a truncated crawl that looks
            # complete is how a generated driver silently omits half a site.
            "capped": self.capped,
            "routes": [r.to_dict() for r in self.routes],
            "skipped": list(self.skipped),
        }
