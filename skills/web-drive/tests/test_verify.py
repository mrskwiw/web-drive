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
