"""``interact`` -- a persistent browser session spanning SEPARATE CLI
invocations (v1.8). Motivating case: `map`'s `gated_controls`/
`state_changing_controls` (v1.6) name a lead on a same-URL, precondition-gated
flow (content-jumpstart.com's Project Wizard); this is how an agent actually
FOLLOWS one up, one real click at a time, instead of guessing a whole step
sequence upfront from static markup.

Every test here shells out to a REAL separate `python -m engine.cli interact
...` subprocess per action -- not `CliRunner().invoke()` in the same
interpreter -- because the entire point being tested is that state survives
across genuinely separate OS processes, which an in-process CliRunner call
would not actually exercise.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

_PAGE = (
    b"<!doctype html><title>Interact Fixture</title>"
    b"<h1>Home</h1>"
    b"<button onclick=\"document.getElementById('out').textContent="
    b"'Step 2 unlocked'\">Continue</button>"
    b"<div id='out'></div>"
    b"<input id='field' type='text'>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *a):
        pass


@contextmanager
def _server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _interact(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "engine.cli", "interact", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=30,
    )


def _skip_if_no_chromium(output: str) -> None:
    if "Executable doesn't exist" in output or "playwright install" in output:
        pytest.skip("Chromium not installed for Playwright")


def test_interact_persists_state_across_separate_processes_and_cleans_up(tmp_path):
    """The whole point of `interact`: a click made by one OS process must be
    visible to a `read` made by a completely different one, and `stop` must
    leave nothing running."""
    state = str(tmp_path / "session.json")

    with _server() as base:
        r = _interact("start", "--url", base, "--state", state, "--headless")
        if r.returncode != 0:
            _skip_if_no_chromium(r.stdout + r.stderr)
            raise AssertionError(f"start failed: {r.stdout}\n{r.stderr}")
        assert Path(state).exists(), "start must write a state handle"
        started = json.loads(r.stdout)
        assert base in started["url"]

        r = _interact("read", "--state", state)
        assert r.returncode == 0, r.stderr
        read1 = json.loads(r.stdout)
        assert base in read1["url"], (
            "a SEPARATE process's read did not see the page `start` navigated -- "
            "state did not actually persist"
        )
        assert "Step 2 unlocked" not in read1["content_preview"]

        r = _interact("click", "--state", state, "--text", "Continue")
        assert r.returncode == 0, r.stderr
        clicked = json.loads(r.stdout)
        assert clicked["navigated"] is False
        assert clicked["changed"] is True, (
            "an in-page content change (no URL change) went undetected -- "
            "exactly the same-URL wizard-step shape this command exists for"
        )

        r = _interact("read", "--state", state)
        assert r.returncode == 0, r.stderr
        read2 = json.loads(r.stdout)
        assert "Step 2 unlocked" in read2["content_preview"], (
            "a THIRD separate process's read did not see the SECOND process's "
            "click -- state is not surviving across processes"
        )

        r = _interact("fill", "--state", state, "--selector", "#field", "--value", "hi")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["filled"] == "#field"

        pid = started_pid = json.loads(Path(state).read_text())["pid"]
        r = _interact("stop", "--state", state)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["stopped"] is True
        assert not Path(state).exists(), "stop must remove the state handle"

        r = _interact("read", "--state", state)
        assert r.returncode != 0, "read after stop must fail -- no session to read"

    if sys.platform == "win32":
        check = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        )
        assert str(pid) not in check.stdout, f"chromium pid {started_pid} still running after stop"
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_interact_start_refuses_to_overwrite_a_live_session(tmp_path):
    """Starting over an existing state file would leak the FIRST browser
    process -- nothing would ever kill it."""
    state = str(tmp_path / "session.json")

    with _server() as base:
        r = _interact("start", "--url", base, "--state", state, "--headless")
        if r.returncode != 0:
            _skip_if_no_chromium(r.stdout + r.stderr)
            raise AssertionError(f"start failed: {r.stdout}\n{r.stderr}")

        r2 = _interact("start", "--url", base, "--state", state, "--headless")
        assert r2.returncode != 0
        assert "already names a session" in (r2.stdout + r2.stderr)

        _interact("stop", "--state", state)


def test_interact_click_requires_text_or_selector(tmp_path):
    state = str(tmp_path / "session.json")

    with _server() as base:
        r = _interact("start", "--url", base, "--state", state, "--headless")
        if r.returncode != 0:
            _skip_if_no_chromium(r.stdout + r.stderr)
            raise AssertionError(f"start failed: {r.stdout}\n{r.stderr}")

        r = _interact("click", "--state", state)
        assert r.returncode != 0
        assert "--text or --selector" in (r.stdout + r.stderr)

        _interact("stop", "--state", state)
