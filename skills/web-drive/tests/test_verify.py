"""``verify``: execute a candidate capability's steps and report
verified/withheld (Phase E). Wires the already-copied, already-unit-tested
`flow.py` / `evidence.py` / `gate.py` pieces into a live CLI command for the
first time in web-drive.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.cli import cli

SEARCH_PAGE = b"""<!doctype html><title>Search</title>
<body>
<input id="q" name="q" type="text">
<button id="go" onclick="document.getElementById('result').textContent = 'Results for ' + document.getElementById('q').value;">Search</button>
<div id="result"></div>
</body>"""

BROKEN_PAGE = b"""<!doctype html><title>Broken</title>
<body>
<button id="go" onclick="undefinedFunctionCall();">Go</button>
</body>"""

PAGES = {"/search": SEARCH_PAGE, "/broken": BROKEN_PAGE}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — stdlib callback name
        path = self.path.split("?")[0].rstrip("/") or "/"
        body = PAGES.get(path)
        status = 200 if body else 404
        body = body or b"<!doctype html><title>404</title>"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Silence benign keep-alive disconnects."""


@contextmanager
def _server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def _invoke(args):
    res = CliRunner().invoke(cli, args)
    msg = str(res.exception or res.output)
    if "Executable doesn't exist" in msg or "playwright install" in msg:
        pytest.skip("Chromium not installed for Playwright")
    return res


def test_verify_passes_a_happy_path_capability(tmp_path):
    steps = [
        {"type": "fill", "selector": "#q", "value": "trees"},
        {"type": "click", "selector": "#go"},
    ]
    steps_path = _write(tmp_path, "steps.json", steps)
    assert_path = _write(tmp_path, "assert.json", {"content_contains": "Results for trees"})

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "site search",
                "--steps",
                steps_path,
                "--assert",
                assert_path,
            ]
        )

    assert res.exit_code == 0, res.output
    result = json.loads(res.output)
    assert result["verb"] == "site search"
    assert result["verified"] is True
    assert result["reason"] is None
    assert len(result["steps"]) == 2
    assert all(s["passed"] for s in result["steps"])


def test_verify_saves_a_session_on_success_but_not_on_failure(tmp_path):
    """`verify --save-session` is how a plain form login (no SSO/MFA, so the
    human-only `login` command is unnecessary) gets persisted headlessly:
    verify IS the login, and the resulting cookies get written out exactly
    when the capability actually verified."""
    steps = [
        {"type": "fill", "selector": "#q", "value": "trees"},
        {"type": "click", "selector": "#go"},
    ]
    steps_path = _write(tmp_path, "steps.json", steps)
    ok_assert = _write(tmp_path, "assert_ok.json", {"content_contains": "Results for trees"})
    bad_assert = _write(tmp_path, "assert_bad.json", {"content_contains": "nope"})
    saved = tmp_path / "session.json"

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "site search",
                "--steps",
                steps_path,
                "--assert",
                ok_assert,
                "--save-session",
                str(saved),
            ]
        )
        assert res.exit_code == 0, res.output
        assert saved.exists()
        bundle = json.loads(saved.read_text(encoding="utf-8"))
        assert "storage_state" in bundle
        assert bundle["user_agent"]  # read from the live page, never null

        saved.unlink()
        res2 = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "site search",
                "--steps",
                steps_path,
                "--assert",
                bad_assert,
                "--save-session",
                str(saved),
            ]
        )
        assert res2.exit_code == 1, res2.output
        assert not saved.exists()  # never written on an unverified run


def test_verify_a_zero_step_capability_checks_the_current_page(tmp_path):
    """An extract-only read verb (spec §5) has no action of its own beyond
    the caller's initial navigation -- `steps: []` must still be verifiable
    against the current page, not unconditionally unverified."""
    steps_path = _write(tmp_path, "steps.json", [])
    assert_path = _write(tmp_path, "assert.json", {"content_contains": "Search"})

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "site status",
                "--steps",
                steps_path,
                "--assert",
                assert_path,
            ]
        )

    assert res.exit_code == 0, res.output
    result = json.loads(res.output)
    assert result["verified"] is True
    assert result["steps"] == []


def test_verify_fails_and_reports_which_assertion_did_not_hold(tmp_path):
    steps = [
        {"type": "fill", "selector": "#q", "value": "trees"},
        {"type": "click", "selector": "#go"},
    ]
    steps_path = _write(tmp_path, "steps.json", steps)
    # Wrong expectation on purpose -- the page renders "Results for trees".
    assert_path = _write(tmp_path, "assert.json", {"content_contains": "No results ever"})

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "site search",
                "--steps",
                steps_path,
                "--assert",
                assert_path,
            ]
        )

    assert res.exit_code == 1, res.output
    result = json.loads(res.output)
    assert result["verified"] is False
    assert "final assertion failed" in result["reason"]
    # Both steps still ran and are reported -- only the CAPABILITY-level
    # assertion (checked after the last step) is what failed here.
    assert len(result["steps"]) == 2


def test_verify_halts_at_the_first_broken_step(tmp_path):
    steps = [
        {"type": "click", "selector": "#go"},
        {"type": "click", "selector": "#never-reached"},
    ]
    steps_path = _write(tmp_path, "steps.json", steps)

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/broken",
                "--verb",
                "broken thing",
                "--steps",
                steps_path,
            ]
        )

    assert res.exit_code == 1, res.output
    result = json.loads(res.output)
    assert result["verified"] is False
    assert "halted at" in result["reason"]
    # The step-2 click on a selector that was never reached must NOT run.
    assert len(result["steps"]) == 1


def test_verify_flags_a_real_locator_timeout_as_drift(tmp_path):
    """WD-T2/S2 (plan v2.0): a step that raises Playwright's OWN TimeoutError
    (a selector that genuinely never resolves -- the shape of real selector
    rot after a redeploy) must set `drift: true`, on both the failing step
    and the overall result, so `runtime.py` can classify it as exit code 3
    from a real signal instead of sniffing the error text for substrings like
    "timeout" or "not found" (which any unrelated exception's message could
    coincidentally contain). This test drives an ACTUAL 15s Playwright
    timeout against a selector that will never exist -- slow but real.
    """
    steps = [{"type": "click", "selector": "#this-selector-does-not-exist-anywhere"}]
    steps_path = _write(tmp_path, "steps.json", steps)

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "drift probe",
                "--steps",
                steps_path,
            ]
        )

    assert res.exit_code == 1, res.output  # raw `verify` CLI: 1 = ran, not verified
    result = json.loads(res.output)
    assert result["verified"] is False
    assert result["drift"] is True, "a genuine locator timeout must be flagged as drift"
    assert result["steps"][0]["drift"] is True
    assert "Timeout" in result["steps"][0]["error"]


def test_verify_does_not_flag_a_plain_assertion_failure_as_drift(tmp_path):
    """The other half of WD-T2/S2: a step that runs successfully but fails
    its OWN assertion (no exception at all, let alone a Playwright timeout)
    must never be misread as drift."""
    steps = [{"type": "click", "selector": "#go", "assert": {"content_contains": "never appears"}}]
    steps_path = _write(tmp_path, "steps.json", steps)

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "assertion probe",
                "--steps",
                steps_path,
            ]
        )

    assert res.exit_code == 1, res.output
    result = json.loads(res.output)
    assert result["verified"] is False
    assert result["drift"] is False
    assert result["steps"][0]["drift"] is False


def test_verify_refuses_a_destructive_candidate_without_yes(tmp_path):
    steps_path = _write(tmp_path, "steps.json", [{"type": "click", "selector": "#go"}])

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "quiz delete",
                "--steps",
                steps_path,
                "--destructive",
            ]
        )

    assert res.exit_code == 4, res.output
    result = json.loads(res.output)
    assert result["verified"] is False
    assert "refused" in result["reason"]
    assert result["steps"] == []


def test_verify_runs_a_destructive_candidate_with_yes(tmp_path):
    steps_path = _write(tmp_path, "steps.json", [{"type": "click", "selector": "#go"}])
    assert_path = _write(tmp_path, "assert.json", {"content_contains": "Results for"})

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "quiz delete",
                "--steps",
                steps_path,
                "--assert",
                assert_path,
                "--destructive",
                "--yes",
            ]
        )

    assert res.exit_code == 0, res.output
    result = json.loads(res.output)
    assert result["verified"] is True


def test_verify_refuses_a_costed_candidate_without_yes(tmp_path):
    """`--costs` is `--destructive`'s sibling gate: a candidate that spends
    real credits or money but leaves nothing destructive behind (a research
    tool, a paid-generation confirm) was previously ungated entirely --
    nothing stopped it from spending on the very first `verify` attempt.
    """
    steps_path = _write(tmp_path, "steps.json", [{"type": "click", "selector": "#go"}])

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "research run",
                "--steps",
                steps_path,
                "--costs",
            ]
        )

    assert res.exit_code == 4, res.output
    result = json.loads(res.output)
    assert result["verified"] is False
    assert "refused" in result["reason"]
    assert "costed" in result["reason"]
    assert result["steps"] == []


def test_verify_runs_a_costed_candidate_with_yes(tmp_path):
    steps_path = _write(tmp_path, "steps.json", [{"type": "click", "selector": "#go"}])
    assert_path = _write(tmp_path, "assert.json", {"content_contains": "Results for"})

    with _server() as base:
        res = _invoke(
            [
                "verify",
                "--url",
                base + "/search",
                "--verb",
                "research run",
                "--steps",
                steps_path,
                "--assert",
                assert_path,
                "--costs",
                "--yes",
            ]
        )

    assert res.exit_code == 0, res.output
    result = json.loads(res.output)
    assert result["verified"] is True
