"""DeterministicGate checks — pure, fixture-driven, no browser."""

from engine.gate import DeterministicGate
from engine.models import (
    Action,
    ActionType,
    ConsoleDelta,
    EvidenceBundle,
    NetworkCall,
)


def _bundle(**over) -> EvidenceBundle:
    """A clean, passing bundle with per-test overrides."""
    base = dict(
        action=Action(type=ActionType.CLICK, selector="button.go"),
        url_before="https://e.com/",
        url_after="https://e.com/next",
        http=[NetworkCall("GET", "https://e.com/next", 200)],
        console=ConsoleDelta(),
        dom_outline_after='main "Welcome" {main}',
        target_present=None,
    )
    base.update(over)
    return EvidenceBundle(**base)


def _check(result, name):
    return next(c for c in result.checks if c.name == name)


def test_clean_bundle_passes_all_checks():
    result = DeterministicGate().evaluate(_bundle())
    assert result.passed is True
    assert all(c.passed for c in result.checks)
    assert {c.name for c in result.checks} == {
        "no_console_errors",
        "http_status_ok",
        "no_crash",
        "no_error_page",
        "navigation_sane",
        "opened_pages_ok",
        "target_survived",
    }


def test_console_error_fails_gate():
    result = DeterministicGate().evaluate(
        _bundle(console=ConsoleDelta(errors=["Uncaught TypeError: x"]))
    )
    assert result.passed is False
    assert _check(result, "no_console_errors").passed is False


def test_http_4xx_and_5xx_fail_but_3xx_passes():
    bad = DeterministicGate().evaluate(
        _bundle(http=[NetworkCall("POST", "https://e.com/api", 500)])
    )
    assert _check(bad, "http_status_ok").passed is False

    redirect_only = DeterministicGate().evaluate(
        _bundle(
            http=[
                NetworkCall("GET", "https://e.com/a", 301),
                NetworkCall("GET", "https://e.com/next", 200),
            ]
        )
    )
    assert _check(redirect_only, "http_status_ok").passed is True


def test_page_error_fails_no_crash():
    result = DeterministicGate().evaluate(_bundle(page_errors=["Boom at app.js:1"]))
    assert _check(result, "no_crash").passed is False


def test_error_page_detected_by_url_and_by_text():
    by_url = DeterministicGate().evaluate(_bundle(url_after="https://e.com/404"))
    assert _check(by_url, "no_error_page").passed is False

    by_text = DeterministicGate().evaluate(
        _bundle(dom_outline_after='main "This page could not be found" {main}')
    )
    assert _check(by_text, "no_error_page").passed is False


def test_navigation_sane_flags_dead_url_and_redirect_loop():
    dead = DeterministicGate().evaluate(_bundle(url_after="about:blank"))
    assert _check(dead, "navigation_sane").passed is False

    loop = DeterministicGate().evaluate(
        _bundle(http=[NetworkCall("GET", "https://e.com/x", 302) for _ in range(12)])
    )
    assert _check(loop, "navigation_sane").passed is False


def test_opened_page_404_fails_gate():
    # a target="_blank" link opening a 404 in a new tab
    result = DeterministicGate().evaluate(
        _bundle(opened=[NetworkCall("GET", "https://sub.itch.io/", 404)])
    )
    assert result.passed is False
    assert _check(result, "opened_pages_ok").passed is False

    # a healthy new-tab link passes
    ok = DeterministicGate().evaluate(
        _bundle(opened=[NetworkCall("GET", "https://store.example.com/", 200)])
    )
    assert _check(ok, "opened_pages_ok").passed is True


def test_target_survived_is_advisory_not_gate_authoritative():
    # a bundle that fails ONLY target_survived still PASSES the gate — the check
    # is advisory (a vanished element is a semantic signal, not objective breakage)
    result = DeterministicGate().evaluate(
        _bundle(
            url_before="https://e.com/p",
            url_after="https://e.com/p",
            target_present=False,
            http=[],
        )
    )
    ts = _check(result, "target_survived")
    assert ts.passed is False
    assert ts.advisory is True
    assert result.passed is True  # advisory failure does not fail the gate


def test_target_survived_semantics():
    # vanished on the same page with NO activity -> fail
    vanished = DeterministicGate().evaluate(
        _bundle(
            url_before="https://e.com/p",
            url_after="https://e.com/p",
            target_present=False,
            http=[],
        )
    )
    assert _check(vanished, "target_survived").passed is False

    # vanished but the action triggered network activity (submit/run button
    # relabels/disables, or a GET-driven SPA update replaces it) -> pass. This
    # softest check stays lenient by design to avoid false short-circuits.
    for call in (
        NetworkCall("POST", "https://e.com/api/run", 200),
        NetworkCall("GET", "https://e.com/api/data", 200),
    ):
        active = DeterministicGate().evaluate(
            _bundle(
                url_before="https://e.com/p",
                url_after="https://e.com/p",
                target_present=False,
                http=[call],
            )
        )
        assert _check(active, "target_survived").passed is True

    # gone but navigated away -> n/a pass
    navigated = DeterministicGate().evaluate(
        _bundle(
            url_before="https://e.com/p",
            url_after="https://e.com/q",
            target_present=False,
        )
    )
    assert _check(navigated, "target_survived").passed is True

    # still present -> pass
    present = DeterministicGate().evaluate(
        _bundle(
            url_before="https://e.com/p",
            url_after="https://e.com/p",
            target_present=True,
        )
    )
    assert _check(present, "target_survived").passed is True
