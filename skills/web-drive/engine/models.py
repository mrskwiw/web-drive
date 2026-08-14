"""Data models for the web-qa engine (v2.0).

Salvaged and reshaped from the v1 standalone tool. Every model is a frozen-ish
``@dataclass`` that serializes via :meth:`Serializable.to_dict`, which converts
enum members to their ``.value`` and recurses into nested dataclasses/lists so
the emitted JSON is clean (plain ``dataclasses.asdict`` would leave enum objects
in place). Preserve this convention when adding fields.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def _clean(obj: Any) -> Any:
    """Recursively convert enums to their values within dicts/lists."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


class Serializable:
    """Mixin giving dataclasses an enum-safe ``to_dict``."""

    def to_dict(self) -> Dict[str, Any]:
        return _clean(asdict(self))  # type: ignore[arg-type,call-overload]


# ---------------------------------------------------------------------------
# Enums (the engine's vocabulary)
# ---------------------------------------------------------------------------


class BrowserEngine(str, Enum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    TYPE = "type"
    PRESS = "press"
    SCROLL = "scroll"
    WAIT_FOR = "wait_for"
    SELECT = "select"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IssueCategory(str, Enum):
    FUNCTIONAL = "functional"
    CONSOLE = "console"
    NETWORK = "network"
    NAVIGATION = "navigation"
    CONTENT = "content"
    ACCESSIBILITY = "accessibility"
    SECURITY = "security"


class A11yImpact(str, Enum):
    """axe-style impact ranking for an accessibility violation.

    ``serious``/``critical`` block assistive-tech users outright (an unlabeled
    control a screen reader can't announce); ``moderate``/``minor`` degrade the
    experience without fully blocking it. The agent maps these to issue
    ``Severity`` in SKILL.md (serious/critical → high/medium, etc.).
    """

    CRITICAL = "critical"
    SERIOUS = "serious"
    MODERATE = "moderate"
    MINOR = "minor"


class IssueSource(str, Enum):
    """Deterministic issues are authoritative; AI issues are advisory."""

    DETERMINISTIC = "deterministic"
    AI = "ai"


class Verdict(str, Enum):
    APPROPRIATE = "appropriate"
    INAPPROPRIATE = "inappropriate"
    UNCERTAIN = "uncertain"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Actions & raw observations
# ---------------------------------------------------------------------------


@dataclass
class Action(Serializable):
    """A single interaction the engine can perform.

    ``value`` is a generic string payload (form value, or scroll distance in px
    for ``SCROLL``). ``inferred_intent`` is the agent's note about what this
    action is *supposed* to accomplish — carried through so judgment has context.
    """

    type: ActionType
    selector: Optional[str] = None
    url: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    inferred_intent: Optional[str] = None


@dataclass
class ConsoleMessage(Serializable):
    level: str  # e.g. "error", "warning", "log"
    text: str


@dataclass
class NetworkCall(Serializable):
    method: str
    url: str
    status: int


# ---------------------------------------------------------------------------
# Page state & evidence
# ---------------------------------------------------------------------------


@dataclass
class PageState(Serializable):
    """A full snapshot of everything observable at one moment.

    ``console``/``network``/``page_errors`` are cumulative from browser launch;
    the :class:`EvidenceBundler` derives per-action deltas by slicing.
    """

    url: str
    title: str
    ready_state: str
    console: List[ConsoleMessage] = field(default_factory=list)
    network: List[NetworkCall] = field(default_factory=list)
    page_errors: List[str] = field(default_factory=list)
    focus: Optional[str] = None
    cookies: Dict[str, str] = field(default_factory=dict)
    dom_outline: str = ""
    content: str = ""  # readable visible text (for outcome judgment, capped)


@dataclass
class ConsoleDelta(Serializable):
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class GateCheckResult(Serializable):
    name: str
    passed: bool
    detail: Optional[str] = None
    advisory: bool = False  # reported but does NOT count toward gate.passed


@dataclass
class GateResult(Serializable):
    passed: bool
    checks: List[GateCheckResult] = field(default_factory=list)


@dataclass
class EvidenceBundle(Serializable):
    """The stable engine↔agent contract (spec §5).

    One bundle per executed action: what was done, what changed, and the
    deterministic gate verdict. ``gate`` is ``None`` until Phase B wires it in.
    """

    action: Action
    url_before: str
    url_after: str
    http: List[NetworkCall] = field(default_factory=list)
    opened: List[NetworkCall] = field(
        default_factory=list
    )  # document nav(s) in new tabs/popups
    console: ConsoleDelta = field(default_factory=ConsoleDelta)
    dom_outline_after: str = ""
    content_after: str = (
        ""  # readable rendered text after the action (outcome judgment)
    )
    focus_after: Optional[str] = None
    cookies_delta: Dict[str, str] = field(default_factory=dict)
    page_errors: List[str] = field(default_factory=list)
    screenshot: Optional[str] = None
    target_present: Optional[bool] = (
        None  # was the acted-on selector still in the DOM after?
    )
    gate: Optional[GateResult] = None


# ---------------------------------------------------------------------------
# Judgment & issues (populated by the agent / later phases)
# ---------------------------------------------------------------------------


@dataclass
class AIVerdict(Serializable):
    verdict: Verdict
    reasoning: str
    confidence: Confidence
    severity: Optional[Severity] = None


@dataclass
class Issue(Serializable):
    id: str
    title: str
    severity: Severity
    category: IssueCategory
    source: IssueSource
    url: str
    description: str
    gate: Optional[GateResult] = None
    ai_verdict: Optional[AIVerdict] = None
    screenshot: Optional[str] = None
    steps_to_reproduce: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exploration snapshot (Phase C consumer)
# ---------------------------------------------------------------------------


@dataclass
class InteractiveElement(Serializable):
    """A candidate interaction, ranked by importance for intent inference."""

    selector: str
    role: str
    text: str
    kind: str = "other"  # cta | nav | field | footer | other
    rank: int = 4  # lower = more important (0 = primary CTA)


@dataclass
class FormField(Serializable):
    selector: str
    type: str
    name: Optional[str] = None
    label: str = ""


@dataclass
class FormInfo(Serializable):
    selector: str
    fields: List[FormField] = field(default_factory=list)
    submit: Optional[str] = None
    destructive: bool = (
        False  # heuristic: password field or pay/delete/subscribe wording
    )


@dataclass
class LinkInfo(Serializable):
    selector: str
    text: str
    href: str
    scheme: str = "relative"  # http | https | mailto | tel | relative | unknown
    external: bool = False
    new_tab: bool = False  # target="_blank"
    dead: bool = False  # href is "#", empty, or javascript: — a link that goes nowhere


@dataclass
class IncompleteFeature(Serializable):
    """A visible not-built marker (Coming Soon / under construction / placeholder)."""

    marker: str  # the phrase matched, e.g. "coming soon"
    label: str  # short surrounding text (the feature it applies to)


# ---------------------------------------------------------------------------
# Accessibility audit (deterministic WCAG checks — objective, so engine-owned)
# ---------------------------------------------------------------------------


@dataclass
class A11yNode(Serializable):
    """One offending element for an accessibility rule."""

    selector: str
    snippet: str = ""  # truncated outerHTML, for the agent to locate the element


@dataclass
class A11yViolation(Serializable):
    """A single failed WCAG rule, aggregated across all offending nodes.

    Objective by construction (a missing ``alt`` attribute *is* missing), so this
    is deterministic evidence — the agent maps it to a ``category:"accessibility"``
    issue. ``help`` states the rule and the fix; ``nodes`` is capped for tokens.
    """

    rule: str  # e.g. "image-alt", "control-name", "html-has-lang"
    impact: A11yImpact
    help: str
    count: int = 0
    nodes: List[A11yNode] = field(default_factory=list)


@dataclass
class A11yReport(Serializable):
    """The accessibility audit for one page — the deterministic half of the
    blind/assistive-tech pass. Keyboard-nav and screen-reader *coherence* remain
    the agent's advisory judgment (SKILL.md §3d)."""

    url: str
    violations: List[A11yViolation] = field(default_factory=list)
    counts: Dict[str, int] = field(
        default_factory=dict
    )  # by impact: {critical,serious,moderate,minor,total}


@dataclass
class PageSnapshot(Serializable):
    """Structured page inventory the agent uses to infer expectations (spec §4 step 0)."""

    url: str
    title: str
    interactive: List[InteractiveElement] = field(default_factory=list)
    forms: List[FormInfo] = field(default_factory=list)
    links: List[LinkInfo] = field(default_factory=list)
    incomplete: List[IncompleteFeature] = field(default_factory=list)
    console: ConsoleDelta = field(default_factory=ConsoleDelta)
    accessibility: Optional[A11yReport] = None


# ---------------------------------------------------------------------------
# Report (Phase E consumer)
# ---------------------------------------------------------------------------


@dataclass
class RunMetadata(Serializable):
    run_id: str
    target_url: str
    engine: BrowserEngine
    actions_total: int = 0
    actions_capped: bool = False
    issues_total: int = 0


@dataclass
class QAReport(Serializable):
    metadata: RunMetadata
    evidence: List[EvidenceBundle] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
