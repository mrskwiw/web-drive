"""``probe``: check a page's declared surface against what actually happens
(spec §3's "proves" half, Phase C). Fixtures for each reconciliation kind,
matching the phase's own gate ("all four reconciliation kinds produced from
fixtures").
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.cli import cli

NAV_PAGE = b"""<!doctype html><title>Nav</title>
<a id="reports" href="/analytics">Reports</a>
<a id="oldfeature" href="/gone">Old Feature</a>
"""

ANALYTICS_PAGE = b"<!doctype html><title>Analytics</title><h1>Analytics</h1>"

FORM_PAGE = b"""<!doctype html><title>Contact</title>
<form id="contact">
  <input name="email" type="email" required>
  <input name="referral_code" type="text" placeholder="Referral code (optional)">
  <button type="submit">Submit</button>
</form>
<div id="alerts"></div>
<script>
document.getElementById('contact').addEventListener('submit', function (e) {
  e.preventDefault();
  var ref = document.querySelector('[name=referral_code]').value;
  if (!ref) {
    var d = document.createElement('div');
    d.setAttribute('role', 'alert');
    d.textContent = 'Referral code is required';
    document.getElementById('alerts').appendChild(d);
  }
});
</script>
"""

ONBOARDING_PAGE = b"""<!doctype html><title>Onboarding</title>
<button id="finish" onclick="localStorage.setItem('onboarded','1'); location.href='/dashboard';">
  Finish setup
</button>
"""

DASHBOARD_PAGE = b"""<!doctype html><title>Dashboard</title>
<script>
  if (!localStorage.getItem('onboarded')) { location.replace('/onboarding'); }
</script>
<h1>Dashboard</h1>
"""

PAGES = {
    "/nav": NAV_PAGE,
    "/analytics": ANALYTICS_PAGE,
    "/form": FORM_PAGE,
    "/onboarding": ONBOARDING_PAGE,
    "/dashboard": DASHBOARD_PAGE,
}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — stdlib callback name
        path = self.path.split("?")[0].rstrip("/") or "/"
        body = PAGES.get(path)
        status = 200 if body else 404
        body = body or b"<!doctype html><title>404</title><h1>Not Found</h1>"
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
        """Silence benign keep-alive disconnects (would corrupt the CLI JSON
        these tests parse from stdout)."""


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


def _probe(url, *extra):
    res = CliRunner().invoke(cli, ["probe", "--url", url, *extra])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"probe failed (exit {res.exit_code}): {msg}")
    return json.loads(res.output)


def _by_kind(result, kind):
    return [r for r in result["reconciliation"] if r["kind"] == kind]


def test_probe_finds_advertised_absent_and_label_route_mismatch():
    with _server() as base:
        result = _probe(base + "/nav")

    absent = _by_kind(result, "advertised_absent")
    assert any(r["subject"] == "Old Feature" for r in absent), result

    mismatch = _by_kind(result, "label_route_mismatch")
    assert any(r["subject"] == "Reports" and "/analytics" in r["observed"] for r in mismatch), result


def test_probe_finds_optional_but_required():
    with _server() as base:
        result = _probe(base + "/form")

    findings = _by_kind(result, "optional_but_required")
    assert any(r["subject"] == "referral_code" for r in findings), result


def test_probe_finds_undocumented_precondition():
    with _server() as base:
        result = _probe(base + "/onboarding", "--check-preconditions")

    findings = _by_kind(result, "undocumented_precondition")
    assert any(r["subject"] == "Finish setup" for r in findings), result


def test_probe_writes_output_file(tmp_path):
    out = tmp_path / "reconciliation.json"
    with _server() as base:
        _probe(base + "/form", "--output", str(out))
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["url"].endswith("/form")
    assert "reconciliation" in written
