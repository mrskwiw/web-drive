"""Serialization contract for the engine models."""

from engine.models import (
    Action,
    ActionType,
    ConsoleDelta,
    EvidenceBundle,
    GateCheckResult,
    GateResult,
    NetworkCall,
)


def test_to_dict_converts_enums_to_values():
    action = Action(
        type=ActionType.CLICK, selector="button.x", inferred_intent="submit"
    )
    d = action.to_dict()
    assert d["type"] == "click"  # enum -> value, not the Enum object
    assert d["selector"] == "button.x"
    assert d["inferred_intent"] == "submit"


def test_evidence_bundle_nested_serialization():
    bundle = EvidenceBundle(
        action=Action(type=ActionType.NAVIGATE, url="https://e.com"),
        url_before="https://e.com",
        url_after="https://e.com/next",
        http=[NetworkCall(method="GET", url="https://e.com/next", status=200)],
        console=ConsoleDelta(errors=["boom"], warnings=[]),
        gate=GateResult(
            passed=True, checks=[GateCheckResult(name="no_crash", passed=True)]
        ),
    )
    d = bundle.to_dict()
    assert d["action"]["type"] == "navigate"
    assert d["http"][0] == {"method": "GET", "url": "https://e.com/next", "status": 200}
    assert d["console"] == {"errors": ["boom"], "warnings": []}
    assert d["gate"]["passed"] is True
    assert d["gate"]["checks"][0]["name"] == "no_crash"
    assert d["screenshot"] is None


def test_evidence_bundle_schema_is_frozen():
    """§5 engine<->agent contract: the evidence-bundle top-level key set is a
    stable interface. This golden guards it against an accidental add/rename/remove
    of a contract field — the seam guard Phase F is meant to provide. If you change
    the bundle shape on purpose, update this set AND the spec §5 example together."""
    bundle = EvidenceBundle(
        action=Action(type=ActionType.NAVIGATE, url="https://e.com"),
        url_before="https://e.com",
        url_after="https://e.com",
    )
    assert set(bundle.to_dict().keys()) == {
        "action",
        "url_before",
        "url_after",
        "http",
        "opened",
        "console",
        "dom_outline_after",
        "content_after",
        "focus_after",
        "cookies_delta",
        "page_errors",
        "screenshot",
        "target_present",
        "gate",
    }
