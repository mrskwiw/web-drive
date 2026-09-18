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


def test_interact_stop_refuses_to_kill_a_pid_that_does_not_match_the_profile_dir(tmp_path):
    """Post-commit review (2026-09-18): a bare PID out of a JSON state file is
    unsafe to trust blindly -- the real process can have already exited and
    the PID been reused by something unrelated by the time `stop` runs, or the
    file could be tampered. `stop` must verify the live process's own command
    line actually references OUR profile dir before ever sending a kill.

    Uses this TEST's own pid with a profile_dir guaranteed not to appear in
    its command line -- if the guard were absent, `stop` would attempt to
    kill the test runner itself (or crash trying)."""
    from engine.interact import stop

    state = tmp_path / "session.json"
    bogus_profile = str(tmp_path / "wd-interact-not-actually-launched")
    state.write_text(
        json.dumps({
            "schema": 1,
            "pid": os.getpid(),
            "port": 0,
            "profile_dir": bogus_profile,
            "entry_url": "http://example.invalid",
        }),
        encoding="utf-8",
    )

    result = stop(str(state))

    assert result["stopped"] is True
    assert result["pid"] == os.getpid()
    assert not state.exists()  # the state handle is still cleaned up
    # the real point of the test: we are still executing, so THIS process was
    # not sent taskkill/SIGKILL despite the state file naming its own pid.


def test_interact_stop_refuses_the_kill_when_verification_itself_is_unavailable(tmp_path, monkeypatch):
    """Second post-commit review round (2026-09-18): first tried the OPPOSITE
    default (kill when verification can't run at all, to avoid leaking the
    chromium process on a missing tool) -- reverted after confirming live on
    this real Windows box that the original `wmic`-based check returns a
    non-zero exit for an ordinary, real, running process. "Unverifiable" was
    not the rare corner case that tradeoff assumed; with a broken `wmic` it
    was effectively the ALWAYS case, which would fire the kill-anyway
    fallback on every single `stop` call and defeat the PID check entirely.
    Switching to PowerShell's `Get-CimInstance` fixed the underlying
    reliability problem; `stop` itself stays safe-by-default regardless --
    an unverifiable check refuses the kill, same as a confirmed non-match.

    Mocks `_process_cmdline` to return `None` (the "couldn't check" case) and
    confirms `stop` does NOT attempt the kill."""
    import engine.interact as interact_module

    killed = {}

    def fake_cmdline(pid):
        return None  # simulates a verification tool that could not run at all

    def fake_run(args, **kwargs):
        killed["taskkill_args"] = args
        class _Result:
            returncode = 0
        return _Result()

    monkeypatch.setattr(interact_module, "_process_cmdline", fake_cmdline)
    monkeypatch.setattr(interact_module.subprocess, "run", fake_run)

    state = tmp_path / "session.json"
    state.write_text(
        json.dumps({
            "schema": 1,
            "pid": 999999,
            "port": 0,
            "profile_dir": str(tmp_path / "wd-interact-fake"),
            "entry_url": "http://example.invalid",
        }),
        encoding="utf-8",
    )

    result = interact_module.stop(str(state))

    assert result["stopped"] is True
    assert "taskkill_args" not in killed, (
        "an unverifiable check must refuse the kill, not assume a match"
    )


def test_interact_stop_refuses_a_non_integer_pid(tmp_path):
    """Third post-commit review round (2026-09-18): `pid` reaches a
    PowerShell command STRING (`_process_cmdline`) and a taskkill argv -- a
    state file is exactly the "could be tampered" input this module already
    reasons about elsewhere, so a hand-edited non-integer pid (a PowerShell
    injection attempt, e.g. "0; Remove-Item C:\\") must be rejected before
    it ever reaches either subprocess call, not silently stringified into
    one."""
    from engine.interact import InteractError, stop

    state = tmp_path / "session.json"
    state.write_text(
        json.dumps({
            "schema": 1,
            "pid": "0; Remove-Item C:\\ -Recurse -Force",
            "port": 0,
            "profile_dir": str(tmp_path / "wd-interact-fake"),
            "entry_url": "http://example.invalid",
        }),
        encoding="utf-8",
    )

    with pytest.raises(InteractError, match="non-integer pid"):
        stop(str(state))


def test_pick_page_refuses_to_guess_between_two_non_internal_pages():
    """Post-commit review (2026-09-18): silently picking a page by position
    (or any heuristic) after a click opens a popup/new tab risks a later
    action reading or mutating a surface the caller never intended. Must fail
    loudly and name the ambiguity instead of guessing.

    A direct unit test against `_pick_page` rather than a real popup driven
    through headless chromium: `window.open` timing in headless mode proved
    non-deterministic (flaky pass/hang depending on unrelated system load),
    while the actual decision this fix makes -- refuse on >1 candidate --
    has nothing to do with browser timing and is exactly and only what a
    fake two-page browser needs to exercise."""
    from engine.interact import InteractError, _pick_page

    class _FakePage:
        def __init__(self, url):
            self.url = url

    class _FakeContext:
        def __init__(self, pages):
            self.pages = pages

    class _FakeBrowser:
        def __init__(self, contexts):
            self.contexts = contexts

    browser = _FakeBrowser([_FakeContext([
        _FakePage("http://example.invalid/"),
        _FakePage("http://example.invalid/popup"),
    ])])

    with pytest.raises(InteractError, match="non-internal pages are open"):
        _pick_page(browser)
