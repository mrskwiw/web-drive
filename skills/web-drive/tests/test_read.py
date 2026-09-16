"""``read``: one page's declared surface (spec §3's "claims" half, Phase B).

Three fixture pages, matching the spec's Phase B gate ("`surface.json` on 3
fixture pages"): a form-heavy page (validation attrs, `<select>` options,
destructive vs. non-destructive forms), a content page (headings, landmarks,
non-form controls, general copy), and a status page (error + empty-state copy).
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.cli import cli

FORM_PAGE = b"""<!doctype html><title>Sign up</title>
<body>
<form aria-label="Create account">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required maxlength="80"
         placeholder="you@example.com" aria-describedby="email-hint">
  <span id="email-hint">We'll never share it.</span>
  <input name="username" type="text" pattern="[a-z0-9]+" minlength="3">
  <select name="plan">
    <option>Free</option>
    <option>Pro</option>
    <option>Team</option>
  </select>
  <button type="submit">Create account</button>
</form>
<form>
  <input name="q" type="text">
  <button type="submit">Search</button>
</form>
</body>"""

CONTENT_PAGE = b"""<!doctype html><title>Dashboard</title>
<body>
<header><nav><a href="/">Home</a> <button>Menu</button></nav></header>
<main>
  <h1>Welcome back</h1>
  <h2>Recent activity</h2>
  <p>Here is a summary of what happened this week across your account.</p>
  <li>Second bullet item with enough text to pass the length floor.</li>
  <button id="new-quiz">New quiz</button>
</main>
<aside aria-label="Tips">Helpful sidebar content.</aside>
<footer><a href="/privacy">Privacy</a></footer>
</body>"""

STATUS_PAGE = b"""<!doctype html><title>Quizzes</title>
<body>
<main>
  <div role="alert">Could not load your quizzes. Please try again.</div>
  <p>No results found for this search.</p>
</main>
</body>"""

PAGES = {"/form": FORM_PAGE, "/content": CONTENT_PAGE, "/status": STATUS_PAGE}


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


def _read(url, *extra):
    res = CliRunner().invoke(cli, ["read", "--url", url, *extra])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"read failed (exit {res.exit_code}): {msg}")
    return json.loads(res.output)


# -- form page: validation attrs, select options, destructive classification --


def test_read_extracts_form_validation_attrs_and_select_options():
    with _server() as base:
        surface = _read(base + "/form")

    signup = next(f for f in surface["forms"] if f["role_name"] == "Create account")

    by_name = {f["name"]: f for f in signup["fields"]}
    email = by_name["email"]
    assert email["type"] == "email"
    assert email["required"] is True
    assert email["maxlength"] == 80
    assert email["placeholder"] == "you@example.com"
    assert email["aria_describedby"] == "email-hint"
    # <label for="email"> resolves as the accessible name, not the placeholder
    assert email["label"] == "Email"

    username = by_name["username"]
    assert username["pattern"] == "[a-z0-9]+"
    assert username["minlength"] == 3
    assert username["required"] is False

    plan = by_name["plan"]
    assert plan["options"] == ["Free", "Pro", "Team"]
    assert plan["role"] == "combobox"

    assert signup["submit_text"] == "Create account"
    assert len(surface["forms"]) == 2  # the signup form and the plain search form
    # two unlabeled forms both resolve to the same base "form" CSS selector --
    # the disambiguator must still tell them apart (spec §5's locator promise)
    assert len({f["selector"] for f in surface["forms"]}) == 2


def test_read_flags_destructive_and_non_destructive_forms_correctly():
    with _server() as base:
        surface = _read(base + "/form")

    by_submit_text = {f["submit_text"]: f for f in surface["forms"]}
    # sign-up wording ("create account") is destructive; a bare search isn't
    assert by_submit_text["Create account"]["destructive"] is True
    assert by_submit_text["Search"]["destructive"] is False


# -- content page: headings, landmarks, non-form controls, copy --------------


def test_read_extracts_headings_landmarks_and_controls():
    with _server() as base:
        surface = _read(base + "/content")

    assert {"level": 1, "text": "Welcome back"} in surface["headings"]
    assert {"level": 2, "text": "Recent activity"} in surface["headings"]

    landmark_roles = {lm["role"] for lm in surface["landmarks"]}
    assert {"banner", "navigation", "main", "complementary", "contentinfo"} <= landmark_roles
    tips = next(lm for lm in surface["landmarks"] if lm["role"] == "complementary")
    assert tips["name"] == "Tips"

    control_names = {c["name"] for c in surface["controls"]}
    assert "Home" in control_names  # nav link
    assert "New quiz" in control_names  # main-content button
    assert "Menu" in control_names

    home = next(c for c in surface["controls"] if c["name"] == "Home")
    assert home["role"] == "link"
    assert home["kind"] == "nav"
    new_quiz = next(c for c in surface["controls"] if c["name"] == "New quiz")
    assert new_quiz["role"] == "button"

    assert any("summary of what happened" in c for c in surface["copy"])


# -- status page: error + empty-state copy ------------------------------------


def test_read_extracts_error_and_empty_state_copy():
    with _server() as base:
        surface = _read(base + "/status")

    assert any("Could not load your quizzes" in e for e in surface["errors"])
    assert any("No results found" in e for e in surface["empty_states"])


def test_read_writes_output_file(tmp_path):
    out = tmp_path / "surface.json"
    with _server() as base:
        _read(base + "/status", "--output", str(out))
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["url"].endswith("/status")
    assert any("Could not load your quizzes" in e for e in written["errors"])
