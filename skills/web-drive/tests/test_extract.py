"""``extract``: container/field specs -> structured records (Phase F).

Mirrors spec §5's own worked example almost exactly: a `<ul>` of quiz
`<li>` items, each carrying a `data-id`, a heading title, and a `<time>`
with a `datetime` attribute.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.cli import cli

LIST_PAGE = b"""<!doctype html><title>My Quizzes</title>
<body>
<ul>
  <li data-id="41">
    <h3>Trees of North America</h3>
    <time datetime="2026-09-01T10:00:00Z">Sep 1</time>
  </li>
  <li data-id="42">
    <h3>Capital Cities</h3>
    <time datetime="2026-09-05T10:00:00Z">Sep 5</time>
  </li>
  <li data-id="43">
    <h3>90s Movies</h3>
    <time datetime="2026-09-10T10:00:00Z">Sep 10</time>
  </li>
</ul>
</body>"""

CARD_PAGE = b"""<!doctype html><title>Cards</title>
<body>
<div role="listitem" class="card" data-slug="alpha">
  <div role="heading">Alpha</div>
</div>
<div role="listitem" class="card" data-slug="beta">
  <div role="heading">Beta</div>
</div>
</body>"""

PAGES = {"/list": LIST_PAGE, "/cards": CARD_PAGE}


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


def _write_spec(tmp_path, spec):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def _extract(url, spec_path):
    res = CliRunner().invoke(cli, ["extract", "--url", url, "--spec", spec_path])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"extract failed (exit {res.exit_code}): {msg}")
    return json.loads(res.output)


def test_extract_pulls_typed_records_from_a_list_with_role_and_selector_fields(tmp_path):
    spec = {
        "container": {"role": "listitem"},
        "fields": {
            "id": {"attr": "data-id"},
            "title": {"role": "heading"},
            "updated": {"selector": "time", "attr": "datetime"},
        },
    }
    spec_path = _write_spec(tmp_path, spec)

    with _server() as base:
        result = _extract(base + "/list", spec_path)

    assert result["url"].endswith("/list")
    records = result["records"]
    assert len(records) == 3
    assert records[0] == {
        "id": "41",
        "title": "Trees of North America",
        "updated": "2026-09-01T10:00:00Z",
    }
    assert records[2]["id"] == "43"
    assert records[2]["title"] == "90s Movies"


def test_extract_uses_explicit_css_selectors_for_container_and_fields(tmp_path):
    spec = {
        "container": {"selector": ".card"},
        "fields": {
            "slug": {"attr": "data-slug"},
            "name": {"selector": "[role=heading]"},
        },
    }
    spec_path = _write_spec(tmp_path, spec)

    with _server() as base:
        result = _extract(base + "/cards", spec_path)

    records = result["records"]
    assert records == [
        {"slug": "alpha", "name": "Alpha"},
        {"slug": "beta", "name": "Beta"},
    ]


def test_extract_returns_empty_list_when_container_matches_nothing(tmp_path):
    spec = {"container": {"selector": ".nonexistent"}, "fields": {"x": {}}}
    spec_path = _write_spec(tmp_path, spec)

    with _server() as base:
        result = _extract(base + "/list", spec_path)

    assert result["records"] == []


def test_extract_writes_output_file(tmp_path):
    spec = {"container": {"role": "listitem"}, "fields": {"id": {"attr": "data-id"}}}
    spec_path = _write_spec(tmp_path, spec)
    out = tmp_path / "records.json"

    with _server() as base:
        res = CliRunner().invoke(
            cli,
            [
                "extract",
                "--url",
                base + "/list",
                "--spec",
                spec_path,
                "--output",
                str(out),
            ],
        )
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"extract failed (exit {res.exit_code}): {msg}")

    written = json.loads(out.read_text(encoding="utf-8"))
    assert len(written["records"]) == 3
