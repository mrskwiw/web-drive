"""Route-graph crawl: URL normalization, auth classification, and a live BFS.

The pure helpers are tested directly because they encode the decisions that
silently corrupt a graph when wrong (three entries for one route, a guessed
auth state presented as an observation). The crawl itself runs against a local
stdlib server with a real link topology, including an auth-gated route, an
off-origin link and a dead one.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.catalog import AuthState
from engine.cli import cli
from engine.sitemap import classify_auth, looks_like_login, normalize, origin_of

# / -> about, dashboard (gated), deep; deep -> deeper; plus off-origin + dead links
PAGES = {
    "/": b"""<!doctype html><title>Home</title><h1>Home</h1>
        <a href="/about">About</a>
        <a href="/dashboard">Dashboard</a>
        <a href="/deep">Deep</a>
        <a href="https://example.com/external">External</a>
        <a href="#">Dead</a>
        <a href="mailto:a@b.c">Mail</a>
        <a href="/about/">About trailing</a>
        <a href="/about#frag">About frag</a>""",
    "/about": b"<!doctype html><title>About</title><h1>About</h1><a href='/'>Home</a>",
    "/deep": b"<!doctype html><title>Deep</title><a href='/deeper'>Deeper</a>",
    "/deeper": b"<!doctype html><title>Deeper</title><p>bottom</p>",
    "/login": b"<!doctype html><title>Login</title><h1>Sign in</h1>",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — stdlib callback name
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/dashboard":  # gated: bounce to the login route
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
        """Silence benign keep-alive disconnects, which socketserver would print
        to stdout and thereby corrupt the CLI JSON these tests parse."""


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


# -- pure helpers -----------------------------------------------------------


def test_normalize_collapses_fragment_and_trailing_slash():
    base = "http://h/a"
    assert normalize("http://h/a/") == base
    assert normalize("http://h/a#frag") == base
    assert normalize("http://h/a/#frag") == base
    # a query is meaningful (it selects different content) and must survive
    assert normalize("http://h/a?tab=1") == "http://h/a?tab=1"
    # root must not normalize away to empty
    assert normalize("http://h/") == "http://h/"


def test_origin_and_login_detection():
    assert origin_of("https://h:8080/a/b?c=1") == "https://h:8080"
    assert looks_like_login("http://h/login")
    assert looks_like_login("http://h/account/login?next=/x")
    assert not looks_like_login("http://h/blog/auth-in-rails")  # substring, not a path


def test_classify_auth_reports_observations_not_guesses():
    assert classify_auth("http://h/a", "http://h/a", 200, False) is AuthState.PUBLIC
    assert classify_auth("http://h/a", "http://h/a", 401, False) is AuthState.REQUIRED
    assert classify_auth("http://h/a", "http://h/a", 403, True) is AuthState.REQUIRED
    # bounced to login => gated
    assert (
        classify_auth("http://h/dash", "http://h/login", 200, False)
        is AuthState.REQUIRED
    )
    # WITH a session everything authenticates, so "public" would be a guess
    # dressed as an observation. Must stay UNKNOWN.
    assert classify_auth("http://h/a", "http://h/a", 200, True) is AuthState.UNKNOWN
    # a redirect that is NOT to a login route is not an auth signal
    assert classify_auth("http://h/a", "http://h/b", 200, False) is AuthState.PUBLIC


# -- live crawl -------------------------------------------------------------


def _map(base, *extra):
    res = CliRunner().invoke(cli, ["map", "--url", base, *extra])
    if res.exit_code != 0:
        msg = str(res.exception or res.output)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise AssertionError(f"map failed (exit {res.exit_code}): {msg}")
    return json.loads(res.output)


def test_crawl_walks_the_graph_and_records_what_it_observed():
    with _server() as base:
        site = _map(base)

    by_path = {r["path"]: r for r in site["routes"]}
    assert {"/", "/about", "/deep", "/deeper"} <= set(by_path)

    # the same route linked three ways (/about, /about/, /about#frag) is ONE node
    assert len([r for r in site["routes"] if r["path"] == "/about"]) == 1

    assert by_path["/"]["status"] == 200
    assert by_path["/"]["auth"] == "public"
    assert by_path["/deeper"]["depth"] == 2  # reached via /deep

    # the gated route is recorded where it LANDED, flagged as a redirect and
    # as auth-required -- what navigation proved, not what the link claimed
    dash = [r for r in site["routes"] if "dashboard" in r["url"]]
    assert dash, "the gated route was never visited"
    assert dash[0]["redirected"] is True
    assert dash[0]["final_url"].endswith("/login")
    assert dash[0]["auth"] == "required"

    # off-origin and non-http links are reported, not silently dropped
    reasons = {s["reason"] for s in site["skipped"]}
    assert "off-origin" in reasons
    assert "non-http scheme" in reasons
    assert any("example.com" in s["url"] for s in site["skipped"])


def test_depth_limit_and_cap_are_disclosed():
    with _server() as base:
        shallow = _map(base, "--max-depth", "1")
        assert "/deeper" not in {r["path"] for r in shallow["routes"]}
        assert shallow["capped"] is False  # bounded by depth, not by the cap

        capped = _map(base, "--max-pages", "2")
        assert capped["route_count"] == 2
        assert capped["capped"] is True, (
            "a truncated crawl must announce it -- a driver generated from a "
            "silently-truncated graph looks complete while missing half the site"
        )
