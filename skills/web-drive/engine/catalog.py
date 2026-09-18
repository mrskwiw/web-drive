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
    # Buttons that changed the page's OWN content without changing the URL --
    # invisible to every other check in this engine, which all key off URL
    # equality (`probe_buttons`'s own `outcome`, and probe.py's `probe_control`/
    # `probe_precondition`, treat "no navigation" as "nothing happened"). A
    # same-URL wizard step is exactly this case: content-jumpstart.com's
    # Project Wizard advances through Client -> Research -> Templates -> ...
    # entirely on `/dashboard/wizard`, so a fully exhaustive, zero-throttled
    # `map --probe-buttons` run reported nothing for it -- not partial
    # coverage, no signal at all. This field exists so a click that DID
    # something is at least visible as a lead, even though naming what it does
    # stays the agent's job (spec's engine/agent split).
    state_changing_controls: List[str] = field(default_factory=list)
    # Buttons whose click failed because Playwright's own actionability check
    # found the element present but not enabled -- the disabled-button shape of
    # a precondition-gated control (e.g. a wizard "Continue" button before its
    # combobox is filled), reported distinctly from a click that simply throws
    # for some other reason.
    gated_controls: List[str] = field(default_factory=list)
    # Disclosure for BUGS.md 2026-08-26: the probe budget is spent in DOM order,
    # so a page whose primary CTA renders last (every control tied at one rank
    # on React Native Web, or just a long control list) can exhaust `--max-probes`
    # before ever reaching it -- and nothing in the old output revealed that a
    # button was simply never clicked, as opposed to clicked and found inert.
    # These two counts make "we clicked N of M probe-safe buttons" a fact you can
    # read off the map instead of infer.
    controls_probed: int = 0
    controls_skipped_budget: int = 0

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
            "state_changing_controls": list(self.state_changing_controls),
            "gated_controls": list(self.gated_controls),
            "controls_probed": self.controls_probed,
            "controls_skipped_budget": self.controls_skipped_budget,
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
            state_changing_controls=list(data.get("state_changing_controls", [])),
            gated_controls=list(data.get("gated_controls", [])),
            controls_probed=data.get("controls_probed", 0),
            controls_skipped_budget=data.get("controls_skipped_budget", 0),
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
    # How asset blocking actually resolved ("chromium-cdp" / "off" /
    # "unavailable: ..."). Recorded because every requests-per-route figure the
    # crawl reports is only interpretable against what was actually fetched.
    asset_blocking: str = "off"
    routes: List[RouteNode] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    capped: bool = False
    # Set when the target rate-limited us. Like `capped`, this is a statement
    # about the TRUSTWORTHINESS of the data, not a log line — a consumer that
    # ignores it can build a driver from routes the crawler itself starved.
    rate_limited: bool = False
    # Stopped on the wall-clock guard rather than because the site ran out. Like
    # `capped`, a statement about how far to trust the map -- and like `capped`,
    # recoverable: the frontier rides in the output, so --resume continues it.
    timed_out: bool = False
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
    # How this site actually navigates. A crawl that follows only <a href> can
    # exhaust its frontier on a site it barely touched, and then reports
    # `frontier: 0` — complete-looking and wrong. Measuring link yield against
    # the number of buttons lets the map say so instead of implying success.
    navigation_hint: Optional[str] = None
    buttons_seen: int = 0
    link_discoveries: int = 0
    templates: List[Dict[str, Any]] = field(default_factory=list)
    collapsed_routes: int = 0
    # How many button probes were served from the cross-template outcome cache
    # instead of paying another full-page-reload click (`sitemap.probe_buttons`).
    # Disclosed so a lower `rate_limit.requests_total` never reads as a smaller
    # or less-verified crawl -- every cache hit was itself built from two real,
    # empirically-consistent clicks elsewhere in this same run.
    probe_cache_hits: int = 0
    _template_seen: Dict[str, int] = field(default_factory=dict)
    _collapsed: Dict[str, int] = field(default_factory=dict)
    _variants: Dict[str, List[str]] = field(default_factory=dict)
    _variants_collapsed: Dict[str, int] = field(default_factory=dict)
    _params: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_url": self.entry_url,
            "origin": self.origin,
            "with_session": self.with_session,
            "asset_blocking": self.asset_blocking,
            "route_count": len(self.routes),
            # `capped` must travel with the data: a truncated crawl that looks
            # complete is how a generated driver silently omits half a site.
            "capped": self.capped,
            "rate_limited": self.rate_limited,
            "timed_out": self.timed_out,
            "throttled_routes": self.throttled_routes,
            "stopped_reason": self.stopped_reason,
            "rate_limit": self.rate_limit.to_dict(),
            "frontier": [list(f) for f in self.frontier],
            "navigation_hint": self.navigation_hint,
            "buttons_seen": self.buttons_seen,
            "link_discoveries": self.link_discoveries,
            "templates": list(self.templates),
            "collapsed_routes": self.collapsed_routes,
            "probe_cache_hits": self.probe_cache_hits,
            "_template_seen": dict(self._template_seen),
            "_collapsed": dict(self._collapsed),
            "_variants": {k: list(v) for k, v in self._variants.items()},
            "_variants_collapsed": dict(self._variants_collapsed),
            "_params": dict(self._params),
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
        # Carried, unlike the timing counters: `buttons_seen` is recomputed from
        # ALL restored routes, so a link count that restarted at 0 each leg would
        # compare this leg's links against the whole run's buttons and flag a
        # perfectly link-navigable site as button-routed on every resume.
        site.link_discoveries = int(data.get("link_discoveries", 0))
        site.frontier = [list(f) for f in data.get("frontier", [])]
        site._template_seen = dict(data.get("_template_seen", {}))
        site._collapsed = dict(data.get("_collapsed", {}))
        return site


# ---------------------------------------------------------------------------
# `read` (Phase B) — one page's DECLARED surface, the "claims" half of spec §3.
# `probe` (Phase C) is what checks these against what navigation proves; until
# then everything here is unverified by construction.
# ---------------------------------------------------------------------------


@dataclass
class SurfaceField:
    """One form control as the markup declares it — including the validation
    attributes spec §3 calls out by name (`required`, `maxlength`, `pattern`,
    `<select>` options), which `map`'s lighter `forms[]` capture never recorded."""

    selector: str
    role: str
    name: Optional[str] = None
    label: str = ""
    type: str = "text"
    required: bool = False
    placeholder: Optional[str] = None
    maxlength: Optional[int] = None
    minlength: Optional[int] = None
    pattern: Optional[str] = None
    options: List[str] = field(default_factory=list)  # <select> option labels
    default_value: Optional[str] = None
    aria_label: Optional[str] = None
    aria_describedby: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selector": self.selector,
            "role": self.role,
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "placeholder": self.placeholder,
            "maxlength": self.maxlength,
            "minlength": self.minlength,
            "pattern": self.pattern,
            "options": list(self.options),
            "default_value": self.default_value,
            "aria_label": self.aria_label,
            "aria_describedby": self.aria_describedby,
        }


@dataclass
class SurfaceForm:
    selector: str
    role_name: Optional[str] = None
    fields: List[SurfaceField] = field(default_factory=list)
    submit_selector: Optional[str] = None
    submit_text: Optional[str] = None
    # Same heuristic as `map`'s form capture (password-outside-login, or
    # pay/delete/subscribe wording) — kept consistent so a form doesn't
    # change classification depending on which command looked at it.
    destructive: bool = False
    # True only for the whole-document fallback group (BUGS.md 2026-09-16):
    # a JS-managed multi-step flow with real input/select/textarea fields but
    # no native <form> boundary at all. `selector` is then a document-wide
    # marker, not a real submit scope -- this flag is what tells the agent
    # not to read it as one, since "what counts as one form" with no <form>
    # tag is a per-site judgment call this engine deliberately does not guess.
    implicit: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selector": self.selector,
            "role_name": self.role_name,
            "fields": [f.to_dict() for f in self.fields],
            "submit_selector": self.submit_selector,
            "submit_text": self.submit_text,
            "destructive": self.destructive,
            "implicit": self.implicit,
        }


@dataclass
class SurfaceControl:
    """A non-form interactive element, addressed role-first (spec §5:
    'Locators are role + accessible-name first, CSS only as fallback')."""

    selector: str
    role: str
    name: str
    kind: str = "other"  # cta | nav | footer | other

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selector": self.selector,
            "role": self.role,
            "name": self.name,
            "kind": self.kind,
        }


@dataclass
class Landmark:
    role: str
    name: Optional[str] = None
    selector: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "name": self.name, "selector": self.selector}


@dataclass
class Heading:
    level: int
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "text": self.text}


@dataclass
class PageSurface:
    """One page's declared surface — `read`'s whole output."""

    url: str
    title: str
    headings: List[Heading] = field(default_factory=list)
    landmarks: List[Landmark] = field(default_factory=list)
    controls: List[SurfaceControl] = field(default_factory=list)
    forms: List[SurfaceForm] = field(default_factory=list)
    # Visible role=alert / aria-live regions — what the app SAYS went wrong.
    errors: List[str] = field(default_factory=list)
    # "No results", "nothing here yet", etc. — what the app says when a list
    # is empty, which `extract` (Phase F) needs to tell "empty" from "broken".
    empty_states: List[str] = field(default_factory=list)
    copy: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "headings": [h.to_dict() for h in self.headings],
            "landmarks": [lm.to_dict() for lm in self.landmarks],
            "controls": [c.to_dict() for c in self.controls],
            "forms": [f.to_dict() for f in self.forms],
            "errors": list(self.errors),
            "empty_states": list(self.empty_states),
            "copy": list(self.copy),
        }


# ---------------------------------------------------------------------------
# `probe` (Phase C) — the NAVIGATE half of spec §3: check a claim from `read`
# or `map` against what actually happens. Deterministic detection only; the
# agent (Phase D+) judges whether a finding matters and names the verb it
# changes (spec §3's table, "Effect on the catalog" column).
# ---------------------------------------------------------------------------


class ReconciliationKind(str, Enum):
    LABEL_ROUTE_MISMATCH = "label_route_mismatch"
    OPTIONAL_BUT_REQUIRED = "optional_but_required"
    ADVERTISED_ABSENT = "advertised_absent"
    UNDOCUMENTED_PRECONDITION = "undocumented_precondition"


@dataclass
class Reconciliation:
    """One disagreement between what the app claims and what navigation proved."""

    kind: ReconciliationKind
    subject: str  # the label/field/route this concerns
    claimed: str  # what the markup/label said
    observed: str  # what navigation proved
    evidence: Optional[str] = None  # selector/URL/status detail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "subject": self.subject,
            "claimed": self.claimed,
            "observed": self.observed,
            "evidence": self.evidence,
        }
