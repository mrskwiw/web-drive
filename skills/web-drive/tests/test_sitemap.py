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


ENTRY_RETIRED = {"on": False}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — stdlib callback name
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/" and ENTRY_RETIRED["on"]:
            # Used by the resume test: with the entry gone, a run that RESTARTS
            # can reach nothing, while a genuine continuation works off its
            # saved frontier and never needs the entry again.
            body = b"<!doctype html><title>gone</title><h1>Gone</h1>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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


# -- entry redirect to a different origin ------------------------------------


@contextmanager
def _redirector(target: str):
    """A server whose root 302s to another origin — the bare-domain -> www case."""

    class _R(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = _Server(("127.0.0.1", 0), _R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_origin_rebases_onto_where_the_entry_landed():
    """quizsquirrel.com 301s to www.quizsquirrel.com. If the crawl keeps the
    REQUESTED origin as its same-origin baseline, every link on the landed page
    is off-origin and a whole site returns a one-route graph that looks complete.
    Found on a live run, 2026-08-16."""
    with _server() as real:
        with _redirector(real + "/") as entry:
            site = _map(entry)

    assert site["origin"] == real, "origin must follow the entry redirect"
    paths = {r["path"] for r in site["routes"]}
    assert {"/about", "/deep"} <= paths, (
        "links on the landed page were treated as off-origin — the crawl "
        f"stopped at {len(site['routes'])} route(s): {sorted(paths)}"
    )
    assert not any(
        s["reason"] == "off-origin" and real in s["url"] for s in site["skipped"]
    )


# -- rate-limit anticipation -------------------------------------------------


THROTTLE_PAGE = (
    b"<!doctype html><title>Throttled</title><h1>Throttled</h1>"
    b"<a href='/a'>A</a><a href='/b'>B</a>"
    b"<script>fetch('/api/data');</script>"
)


class _ThrottleHandler(http.server.BaseHTTPRequestHandler):
    """Documents always 200; the SPA's data endpoint always 429.

    This is the shape that defeated the first implementation: checking the
    document status alone reports a perfectly healthy crawl of starved pages.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/"):
            body, status = b"rate limited", 429
        else:
            body, status = THROTTLE_PAGE, 200
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def _throttling_server():
    srv = _Server(("127.0.0.1", 0), _ThrottleHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_crawl_detects_429_on_subresources_and_stops():
    """A 429'd data fetch behind a 200 document must stop the crawl and be
    disclosed -- not silently produce rows for pages the crawler starved."""
    with _throttling_server() as base:
        site = _map(base, "--max-retries", "1", "--delay-ms", "0")

    assert site["rate_limited"] is True, (
        "429s on sub-resources went undetected -- the document status was 200, "
        "which is exactly the case a document-only check misses"
    )
    assert site["throttled_routes"] >= 1
    assert site["stopped_reason"] and "rate limited" in site["stopped_reason"]
    assert site["routes"][-1]["throttled"] is True
    # It must STOP, not carry on collecting starved rows.
    assert (
        site["route_count"] <= 2
    ), f"kept crawling while throttled: {site['route_count']}"


def test_clean_site_is_not_flagged_as_rate_limited():
    """No false positives: the ordinary fixture must come back clean."""
    with _server() as base:
        site = _map(base, "--delay-ms", "0", "--max-pages", "3")
    assert site["rate_limited"] is False
    assert site["throttled_routes"] == 0
    assert site["stopped_reason"] is None
    assert all(r["throttled"] is False for r in site["routes"])


# -- continuation ------------------------------------------------------------


def test_capped_run_carries_a_frontier_and_resumes_without_rewalking(tmp_path):
    """A stopped crawl must be continuable, not restartable.

    Restarting re-spends requests on routes already recorded — the most
    expensive possible move against the very targets that stop you.

    The discriminator matters: an earlier version of this test asserted only
    "more routes, no duplicates", which a plain restart also satisfies, so it
    passed with resume disabled. Here the entry page is RETIRED between the two
    legs, so a restart can reach nothing at all (the entry 404s and yields no
    links) while a genuine continuation works purely off its saved frontier.
    """
    first_path = tmp_path / "first.json"
    with _server() as base:
        first = _map(
            base,
            "--max-pages",
            "2",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--output",
            str(first_path),
        )
        assert first["capped"] is True
        assert first["frontier"], "a capped run must carry its remaining queue"
        already = {r["path"] for r in first["routes"]}
        queued = {f[0] for f in first["frontier"]}

        ENTRY_RETIRED["on"] = True
        try:
            second = _map(
                base,
                "--resume",
                str(first_path),
                "--delay-ms",
                "0",
                "--max-rpm",
                "0",
                "--max-pages",
                "6",
            )
        finally:
            ENTRY_RETIRED["on"] = False

    paths = [r["path"] for r in second["routes"]]
    assert len(paths) == len(set(paths)), f"resume re-walked a route: {paths}"
    assert already <= set(paths), "resume dropped routes the first leg had found"
    # Only a real continuation can reach a queued route now that / is gone.
    reached = {r["url"] for r in second["routes"]}
    assert reached & queued, (
        "no frontier route was visited — the run restarted instead of resuming "
        f"(reached={sorted(paths)})"
    )
    assert second["route_count"] > first["route_count"], "resume made no progress"
    assert second["entry_url"] == first["entry_url"]
    assert second["origin"] == first["origin"]


def test_resume_clears_stale_trust_flags():
    """A clean continuation must not inherit the previous leg's rate_limited
    flag — that would permanently mark a map untrustworthy after one bad run."""
    stale = {
        "entry_url": "http://h/",
        "origin": "http://h",
        "with_session": False,
        "routes": [{"path": "/", "url": "http://h/", "auth": "public"}],
        "skipped": [],
        "frontier": [],
    }
    from engine.catalog import SiteMap

    site = SiteMap.resume_from(stale)
    assert site.rate_limited is False
    assert site.stopped_reason is None
    assert len(site.routes) == 1 and site.routes[0].path == "/"


# -- rate-limit mapping ------------------------------------------------------


def test_rate_limit_profile_measures_the_run():
    """The crawl reports what it OBSERVED about the limiter, so a later run can
    pick a budget from evidence instead of a guess."""
    with _server() as base:
        site = _map(base, "--delay-ms", "0", "--max-rpm", "0", "--max-pages", "3")
    rl = site["rate_limit"]
    assert rl["requests_total"] > 0
    assert rl["requests_per_route"] > 0, "per-route request cost is the tuning number"
    assert rl["effective_rpm"] > 0
    assert rl["throttled_requests"] == 0
    assert rl["first_throttle_after_requests"] is None


def test_rate_limit_profile_records_where_throttling_began():
    with _throttling_server() as base:
        site = _map(base, "--max-retries", "1", "--delay-ms", "0", "--max-rpm", "0")
    rl = site["rate_limit"]
    assert site["rate_limited"] is True
    assert rl["throttled_requests"] >= 1
    assert rl["first_throttle_after_requests"] is not None, (
        "the crawl must record WHERE the limiter bit, or a later run cannot "
        "choose a budget from it"
    )
    assert rl["first_throttle_after_s"] is not None


# -- route templating --------------------------------------------------------


def test_templatize_collapses_identifier_segments():
    from engine.sitemap import templatize

    assert templatize("/quiz/0b9ed04c-accd-4724-9767-80860271bcad") == "/quiz/{uuid}"
    assert templatize("/user/123") == "/user/{int}"
    assert templatize("/o/" + "a1b2c3d4e5f60718") == "/o/{hex}"
    # words that merely look id-ish must survive: collapsing a real route into a
    # template would hide it from the map entirely
    assert templatize("/quiz/create") == "/quiz/create"
    assert templatize("/profile/mrsquirrel") == "/profile/mrsquirrel"
    assert templatize("/") == "/"


TEMPLATED = (
    b"<!doctype html><title>Index</title><h1>Index</h1>"
    + b"".join(
        f"<a href='/item/{i:08d}-aaaa-bbbb-cccc-1234567890ab'>Item {i}</a>".encode()
        for i in range(8)
    )
    + b"<a href='/about'>About</a>"
)


class _TemplateHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = (
            b"<!doctype html><title>Item</title><h1>Item</h1>"
            if self.path.startswith("/item/")
            else (
                b"<!doctype html><title>About</title><h1>About</h1>"
                if self.path.startswith("/about")
                else TEMPLATED
            )
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextmanager
def _template_server():
    srv = _Server(("127.0.0.1", 0), _TemplateHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_instances_of_one_template_are_sampled_not_all_walked():
    """Eight instances of one route shape must cost three page loads, not eight.

    This is what makes a content-heavy site mappable at all: without it the
    request budget is spent re-learning one page shape, and the structural routes
    beyond it are never reached. The count of what was skipped still travels with
    the map, so "we saw 8 and walked 3" stays distinguishable from "there are 3".
    """
    with _template_server() as base:
        site = _map(
            base,
            "--max-per-template",
            "3",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-depth",
            "2",
        )

    items = [r for r in site["routes"] if r["path"].startswith("/item/")]
    assert len(items) == 3, f"walked {len(items)} instances, expected the 3-sample cap"
    assert site["collapsed_routes"] == 5, site["collapsed_routes"]

    by_t = {t["template"]: t for t in site["templates"]}
    assert "/item/{uuid}" in by_t, sorted(by_t)
    assert by_t["/item/{uuid}"]["instances_seen"] == 8
    assert by_t["/item/{uuid}"]["collapsed"] == 5
    # a non-templated sibling must NOT be collapsed away
    assert "/about" in {r["path"] for r in site["routes"]}


def test_asset_blocking_removes_asset_requests_but_keeps_routes():
    """Blocking images/fonts/media cuts the request count -- the unit limiters
    meter -- without changing what the map records."""
    with _server() as base:
        blocked = _map(base, "--delay-ms", "0", "--max-rpm", "0", "--max-pages", "3")
        allowed = _map(
            base,
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-pages",
            "3",
            "--no-block-assets",
        )
    assert {r["path"] for r in blocked["routes"]} == {
        r["path"] for r in allowed["routes"]
    }
    assert (
        blocked["rate_limit"]["requests_total"]
        <= allowed["rate_limit"]["requests_total"]
    )


# -- button-driven route discovery -------------------------------------------


BUTTON_INDEX = (
    b"<!doctype html><title>Gate</title><h1>Gate</h1>"
    b"<button onclick=\"location.href='/inner'\">Enter Dashboard</button>"
    b"<button onclick=\"location.href='/wiped'\">Delete everything</button>"
)


class _ButtonHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/inner"):
            body = b"<!doctype html><title>Inner</title><h1>Inner</h1><a href='/deep2'>Deep</a>"
        elif self.path.startswith("/deep2"):
            body = b"<!doctype html><title>Deep2</title><h1>Deep2</h1>"
        elif self.path.startswith("/wiped"):
            body = b"<!doctype html><title>Wiped</title><h1>Wiped</h1>"
        else:
            body = BUTTON_INDEX
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextmanager
def _button_server():
    srv = _Server(("127.0.0.1", 0), _ButtonHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_button_probing_finds_routes_no_link_exposes_and_skips_mutating_labels():
    """An interstitial whose only control is a button is invisible to a link
    crawler -- which is exactly how a real app mapped to a single route. Probing
    must find it, and must NOT fire a control whose label says it changes state:
    a map should describe the site, not what the crawl broke.
    """
    with _button_server() as base:
        without = _map(base, "--delay-ms", "0", "--max-rpm", "0")
        with_probe = _map(base, "--probe-buttons", "--delay-ms", "0", "--max-rpm", "0")

    assert without["route_count"] == 1, "link-only crawl should see just the gate page"

    paths = {r["path"] for r in with_probe["routes"]}
    assert "/inner" in paths, f"button-reachable route not discovered: {sorted(paths)}"
    # and its onward <a href> links are then followed normally
    assert "/deep2" in paths, "discovery did not feed the normal link frontier"
    assert "/wiped" not in paths, (
        "clicked a control labelled 'Delete everything' -- probing must skip "
        "state-changing labels even on a target we own"
    )


def test_routes_carry_their_controls_and_forms():
    """A route list is not a walkable map; a route plus its addressable controls
    is. This costs no extra request -- the snapshot is already taken to find
    links."""
    with _server() as base:
        site = _map(base, "--delay-ms", "0", "--max-rpm", "0", "--max-pages", "2")
    root = next(r for r in site["routes"] if r["path"] == "/")
    assert root["controls"], "no controls captured for the entry route"
    assert all({"role", "text", "selector"} <= set(c) for c in root["controls"])
