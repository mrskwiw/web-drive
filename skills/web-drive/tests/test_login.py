"""Interactive login capture: `login` opens a real browser, a human signs in,
the resulting APP session is saved for headless replay.

The human is simulated by a page that redirects itself after a beat — which is
what an OAuth round trip looks like to the poller. That lets the success-
detection and save logic be tested headlessly even though the feature exists
precisely because some logins cannot be automated.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.cli import cli

COOKIE = "app_session=granted-by-idp"


class _H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, body, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/dashboard"):
            # The app sets ITS session here — this is the thing worth saving;
            # the identity provider is already out of the picture.
            self._send(
                200,
                b"<!doctype html><title>In</title><h1 id='home'>Signed in</h1>",
                headers=[("Set-Cookie", f"{COOKIE}; Path=/")],
            )
        elif self.path.startswith("/slow"):
            self._send(
                200, b"<!doctype html><title>Wait</title><h1>Nothing happens</h1>"
            )
        else:
            # Stands in for the human completing OAuth.
            self._send(
                200,
                b"<!doctype html><title>Login</title><h1>Login</h1>"
                b"<script>setTimeout(function(){location.href='/dashboard';},700);</script>",
            )

    def log_message(self, *a):
        pass


class _S(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Silence keep-alive disconnects that would corrupt the CLI's JSON."""


@contextmanager
def _server():
    srv = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _login(args):
    res = CliRunner().invoke(cli, ["login", *args])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"login failed (exit {res.exit_code}): {msg}")
    out = res.output[res.output.index("{") :]
    return json.loads(out)


def test_login_saves_a_replayable_session_once_the_condition_is_met(tmp_path):
    sess = tmp_path / "s.json"
    with _server() as base:
        r = _login(
            [
                "--url",
                base,
                "--until-url",
                "/dashboard",
                "--timeout-s",
                "20",
                "--headless",
                "--save-session",
                str(sess),
                "--user-agent",
                "PinnedUA/1.0",
            ]
        )
    assert r["detected_login"] is True
    assert r["final_url"].endswith("/dashboard")
    bundle = json.loads(sess.read_text(encoding="utf-8"))
    names = {c["name"] for c in bundle["storage_state"].get("cookies", [])}
    assert "app_session" in names, f"app session not captured: {names}"
    # The UA must ride along: tokens are commonly bound to a UA+IP fingerprint,
    # so a bundle that forgets it replays as an unauthenticated stranger.
    assert bundle["user_agent"] == "PinnedUA/1.0"


def test_login_reports_failure_when_the_condition_never_holds(tmp_path):
    """A half-finished login must never be reported as success. The bundle is
    still written (worth inspecting) but detected_login stays False."""
    sess = tmp_path / "s2.json"
    with _server() as base:
        r = _login(
            [
                "--url",
                f"{base}/slow",
                "--until-selector",
                "#never",
                "--timeout-s",
                "2",
                "--headless",
                "--save-session",
                str(sess),
            ]
        )
    assert r["detected_login"] is False
    assert sess.exists(), "bundle should still be written for inspection"


def test_selector_condition_works_when_the_url_does_not_change(tmp_path):
    """Apps that land back on the same path need a selector, not a URL match."""
    sess = tmp_path / "s3.json"
    with _server() as base:
        r = _login(
            [
                "--url",
                base,
                "--until-selector",
                "#home",
                "--timeout-s",
                "20",
                "--headless",
                "--save-session",
                str(sess),
            ]
        )
    assert r["detected_login"] is True
