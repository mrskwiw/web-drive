"""Flow helpers — secret resolution + per-step assertions (pure, no browser)."""

import pytest

from engine.flow import (
    MissingSecretError,
    build_action,
    evaluate_assertion,
    fail_reason,
    resolve_str,
    slug,
)
from engine.models import (
    Action,
    ActionType,
    ConsoleDelta,
    EvidenceBundle,
    GateCheckResult,
    GateResult,
    NetworkCall,
)


def _bundle(**over) -> EvidenceBundle:
    base = dict(
        action=Action(type=ActionType.CLICK, selector="button.save"),
        url_before="https://e.com/wizard",
        url_after="https://e.com/wizard",
        http=[],
        console=ConsoleDelta(),
        dom_outline_after='main "Client Brief: Saved" {main}',
        content_after="Client Brief: Saved. Keywords: project management, team collaboration.",
        target_present=None,
    )
    base.update(over)
    return EvidenceBundle(**base)


def _check(result, name):
    return next(c for c in result.checks if c.name == name)


# -- secrets ---------------------------------------------------------------


def test_resolve_str_env_object_form():
    assert resolve_str({"env": "PW"}, {"PW": "s3cret"}) == "s3cret"


def test_resolve_str_interpolates_placeholders():
    out = resolve_str("Bearer ${TOK} for ${USER}", {"TOK": "abc", "USER": "jane"})
    assert out == "Bearer abc for jane"


def test_resolve_str_passthrough_and_none():
    assert resolve_str("plain", {}) == "plain"
    assert resolve_str(None, {}) is None
    assert resolve_str(42, {}) == 42


def test_resolve_str_missing_secret_raises():
    with pytest.raises(MissingSecretError):
        resolve_str({"env": "NOPE"}, {})
    with pytest.raises(MissingSecretError):
        resolve_str("${NOPE}", {})


def test_build_action_resolves_secrets():
    step = {
        "type": "fill",
        "selector": "#password",
        "value": {"env": "PW"},
        "label": "enter password",
    }
    action = build_action(step, {"PW": "hunter2"})
    assert action.type is ActionType.FILL
    assert action.selector == "#password"
    assert action.value == "hunter2"
    assert action.inferred_intent == "enter password"  # label used as intent


# -- assertions ------------------------------------------------------------


def test_empty_assertion_passes():
    assert evaluate_assertion(_bundle(), None).passed is True
    assert evaluate_assertion(_bundle(), {}).passed is True


def test_url_changed_and_contains():
    nav = _bundle(url_after="https://e.com/dashboard")
    r = evaluate_assertion(nav, {"url_changed": True, "url_contains": "/dashboard"})
    assert r.passed is True

    stayed = evaluate_assertion(_bundle(), {"url_changed": True})
    assert stayed.passed is False
    assert _check(stayed, "url_changed").passed is False


def test_dom_contains_and_absent():
    r = evaluate_assertion(
        _bundle(), {"dom_contains": "Saved", "dom_absent": "Something Went Wrong"}
    )
    assert r.passed is True
    bad = evaluate_assertion(_bundle(), {"dom_contains": "Nonexistent Label"})
    assert bad.passed is False


def test_content_contains_and_absent_for_outcome_checks():
    # coarse outcome net: result content shows real keywords, no error/empty marker
    good = evaluate_assertion(
        _bundle(),
        {"content_contains": "project management", "content_absent": "No results"},
    )
    assert good.passed is True

    # an empty/failed research result would trip these
    empty = evaluate_assertion(
        _bundle(content_after="No results found."),
        {"content_contains": "keywords", "content_absent": "No results"},
    )
    assert empty.passed is False


def test_http_any_matches_method_path_and_status():
    b = _bundle(
        http=[
            NetworkCall("GET", "https://e.com/api/credits/balance", 200),
            NetworkCall("POST", "https://e.com/api/clients/", 201),
        ]
    )
    ok = evaluate_assertion(
        b,
        {
            "http_any": {
                "method": "POST",
                "path_contains": "/api/clients",
                "status_lt": 400,
            }
        },
    )
    assert ok.passed is True

    miss = evaluate_assertion(
        b, {"http_any": {"method": "POST", "path_contains": "/api/projects"}}
    )
    assert miss.passed is False


def test_no_http_errors_and_no_console_errors():
    b = _bundle(
        http=[NetworkCall("POST", "https://e.com/api/x", 500)],
        console=ConsoleDelta(errors=["Boom"]),
    )
    r = evaluate_assertion(b, {"no_http_errors": True, "no_console_errors": True})
    assert r.passed is False
    assert _check(r, "no_http_errors").passed is False
    assert _check(r, "no_console_errors").passed is False


# -- fail reasons / slug ---------------------------------------------------


def test_fail_reason_prioritizes_perform_error():
    reason = fail_reason(
        GateResult(True, []), evaluate_assertion(_bundle(), None), "timeout"
    )
    assert "action raised" in reason


def test_fail_reason_reports_gate_and_assertion():
    gate = GateResult(False, [GateCheckResult("http_status_ok", False, "500")])
    assertion = evaluate_assertion(_bundle(), {"url_changed": True})
    reason = fail_reason(gate, assertion, None)
    assert "gate failed: http_status_ok" in reason
    assert "assertion failed: url_changed" in reason


def test_slug():
    assert slug("Save Profile!") == "save-profile"
    assert slug("") == "step"
