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
        shallow = _map(base, "--single-pass", "--max-depth", "1")
        assert "/deeper" not in {r["path"] for r in shallow["routes"]}
        assert shallow["capped"] is False  # bounded by depth, not by the cap

        capped = _map(base, "--single-pass", "--max-pages", "2")
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
        site = _map(base, "--single-pass", "--max-retries", "1", "--delay-ms", "0")

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
        site = _map(
            base,
            "--single-pass",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-pages",
            "3",
        )
    assert site["rate_limited"] is False
    assert site["throttled_routes"] == 0
    assert site["stopped_reason"] is None
    assert all(r["throttled"] is False for r in site["routes"])


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
        site = _map(
            base,
            "--single-pass",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-pages",
            "3",
        )
    rl = site["rate_limit"]
    assert rl["requests_total"] > 0
    assert rl["requests_per_route"] > 0, "per-route request cost is the tuning number"
    assert rl["effective_rpm"] > 0
    assert rl["throttled_requests"] == 0
    assert rl["first_throttle_after_requests"] is None


def test_rate_limit_profile_records_where_throttling_began():
    with _throttling_server() as base:
        site = _map(
            base,
            "--single-pass",
            "--max-retries",
            "1",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
        )
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
        blocked = _map(
            base,
            "--single-pass",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-pages",
            "3",
        )
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
        site = _map(
            base,
            "--single-pass",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-pages",
            "2",
        )
    root = next(r for r in site["routes"] if r["path"] == "/")
    assert root["controls"], "no controls captured for the entry route"
    assert all({"role", "text", "selector"} <= set(c) for c in root["controls"])


def test_auto_resume_exhausts_the_frontier_without_manual_legs():
    """The default must finish the site on its own.

    Finishing content-jumpstart took four hand-run --resume invocations; the
    crawl already recorded its frontier each time, so the only thing the human
    added was patience. A capped first leg must now be continued automatically
    until nothing is queued.
    """
    with _server() as base:
        one = _map(
            base,
            "--single-pass",
            "--max-pages",
            "2",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
        )
        assert one["capped"] is True and one["frontier"], "setup: leg 1 must stop early"

        auto = _map(base, "--max-pages", "50", "--delay-ms", "0", "--max-rpm", "0")
    assert auto["frontier"] == [], f"auto-resume left {len(auto['frontier'])} queued"
    assert auto["route_count"] > one["route_count"]


# -- form-based discovery + incompleteness disclosure -------------------------


FORM_INDEX = (
    b"<!doctype html><title>Home</title><h1>Home</h1>"
    b"<form action='/results'><input name='q' type='search'>"
    b"<button type='submit'>Search</button></form>"
    b"<form action='/wiped'><input name='confirm' type='text'>"
    b"<button type='submit'>Delete account</button></form>"
    b"<form action='/loggedin'><input name='u' type='text'>"
    b"<input name='p' type='password'><button type='submit'>Sign in</button></form>"
)


class _FormHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/results":
            body = b"<!doctype html><title>Results</title><h1>Results</h1>"
        elif path == "/wiped":
            body = b"<!doctype html><title>Wiped</title><h1>Wiped</h1>"
        elif path == "/loggedin":
            body = b"<!doctype html><title>In</title><h1>In</h1>"
        else:
            body = FORM_INDEX
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextmanager
def _form_server():
    srv = _Server(("127.0.0.1", 0), _FormHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_form_filling_reaches_behind_a_search_but_never_submits_a_login_or_a_delete():
    """Search and filter forms hide whole sections from a link crawler, so a
    discovery crawl has to fill them. It must not become a WRITE while doing it:
    a destructive form is skipped, and a form with a password field is skipped
    because guessing at a login is both useless and hostile.
    """
    with _form_server() as base:
        site = _map(
            base,
            "--fill-forms",
            "--single-pass",
            "--delay-ms",
            "0",
            "--max-rpm",
            "0",
            "--max-depth",
            "2",
        )
    paths = {r["path"] for r in site["routes"]}
    assert "/results" in paths, f"search form not followed: {sorted(paths)}"
    assert "/wiped" not in paths, "submitted a form labelled 'Delete account'"
    assert "/loggedin" not in paths, "submitted a form containing a password field"


LINKS_AND_BUTTONS = (
    b"<!doctype html><title>Both</title><h1>Both</h1>"
    + b"".join(f"<button>Panel {i}</button>".encode() for i in range(14))
    + b"<a href='/about'>About</a><a href='/deep'>Deep</a>"
)


class _LinkButtonHandler(_Handler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(LINKS_AND_BUTTONS)))
            self.end_headers()
            self.wfile.write(LINKS_AND_BUTTONS)
            return
        super().do_GET()


@contextmanager
def _link_and_button_server():
    srv = _Server(("127.0.0.1", 0), _LinkButtonHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


MANY_BUTTONS = (
    b"<!doctype html><title>App</title><h1>App</h1>"
    + b"".join(f"<button>Panel {i}</button>".encode() for i in range(14))
    + b"<a href='https://elsewhere.example.com/x'>Off site</a>"
)


class _ManyButtonHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(MANY_BUTTONS)))
        self.end_headers()
        self.wfile.write(MANY_BUTTONS)

    def log_message(self, *a):
        pass


@contextmanager
def _many_button_server():
    srv = _Server(("127.0.0.1", 0), _ManyButtonHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_button_routed_site_is_flagged_as_likely_incomplete():
    """`frontier: 0` on a button-routed site reads as success and is not. The
    map must say so itself -- this is the isekaizero case, where 39 buttons
    yielded 3 link-discovered routes and the output looked complete."""
    with _many_button_server() as base:
        site = _map(base, "--single-pass", "--delay-ms", "0", "--max-rpm", "0")
    assert site["frontier"] == [] and site["route_count"] == 1
    hint = site["navigation_hint"]
    assert hint and "LIKELY INCOMPLETE" in hint, f"no incompleteness hint: {hint!r}"
    assert "--probe-buttons" in hint


def test_no_incompleteness_hint_when_links_actually_yield_routes():
    """No crying wolf -- and the fixture must be able to trigger the hint.

    An earlier version used a button-free page, so the heuristic returned early
    on the button count and the link-yield half was never exercised. It passed
    while `link_discoveries` was permanently 0, i.e. while the discriminator was
    dead. This page carries enough buttons to clear that gate, so silence here
    can only come from links actually yielding routes.
    """
    with _link_and_button_server() as base:
        site = _map(base, "--single-pass", "--delay-ms", "0", "--max-rpm", "0")
    assert site["buttons_seen"] >= 10, "fixture cannot reach the link-yield check"
    assert site["link_discoveries"] > 0, "link discovery counter is not wired"
    assert site["navigation_hint"] is None, site["navigation_hint"]


# One template, many instances, plus a heavy button surface -- the exact shape
# isekaizero returned: 140 storyline links found, 3 walked, 137 collapsed.
CATALOG_PAGE = (
    b"<!doctype html><title>Catalog</title><h1>Catalog</h1>"
    + b"".join(f"<button>Panel {i}</button>".encode() for i in range(14))
    + b"".join(
        f"<a href='/storylines/{i:024x}'>Story {i}</a>".encode() for i in range(30)
    )
)


class _CatalogHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        body = (
            CATALOG_PAGE
            if path == "/"
            else b"<!doctype html><title>Story</title><p>a story</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextmanager
def _catalog_server():
    srv = _Server(("127.0.0.1", 0), _CatalogHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_template_collapsed_links_still_count_as_link_navigation():
    """Our own sampling cap must not be reported as the site's failure to link.

    isekaizero's map showed `collapsed_routes: 137` beside `link_discoveries: 3`
    -- i.e. link crawling found 140 routes and we chose to walk 3. Counting only
    the walked ones made the hint say "only 3 came from <a href>" and blame the
    site for a limit we imposed. Any site with one high-cardinality template
    (a blog, a catalog, a storyline list) would be libelled the same way.
    """
    with _catalog_server() as base:
        site = _map(base, "--single-pass", "--delay-ms", "0", "--max-rpm", "0")
    assert site["collapsed_routes"] > 20, "fixture did not exercise the cap"
    assert site["buttons_seen"] >= 14, "fixture cannot reach the link-yield check"
    assert site["link_discoveries"] == 30, (
        "link_discoveries counted only the links that survived the per-template "
        f"cap: {site['link_discoveries']} vs 30 found"
    )
    assert site["navigation_hint"] is None, site["navigation_hint"]


# -- asset blocking ---------------------------------------------------------

# Body bytes are irrelevant: Playwright derives `resource_type` from the element
# that initiated the request (an <img> is an "image"), not from the response, and
# being REQUESTED AT ALL is the whole signal here.
_FAKE_IMG = b"not-really-a-png"

ASSET_PAGE = (
    b"<!doctype html><title>Assets</title>"
    b"<link rel='stylesheet' href='/style.css'>"
    + b"".join(f"<img src='/img/{i}.png'>".encode() for i in range(6))
    + b"<h1>Assets</h1>"
)


class _AssetHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        type(self).seen.append(path)
        if path.startswith("/img/"):
            body, ctype = _FAKE_IMG, "image/png"
        elif path == "/style.css":
            body, ctype = b"h1{color:#111}", "text/css"
        else:
            body, ctype = ASSET_PAGE, "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextmanager
def _asset_server():
    _AssetHandler.seen = []
    srv = _Server(("127.0.0.1", 0), _AssetHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_block_assets_actually_blocks_images_and_spares_stylesheets():
    """`--block-assets` was a default-ON flag that did nothing for eight releases.

    The value threaded CLI -> controller -> `self._block_assets` and was then
    never read, so every crawl pulled every image while the help text promised
    otherwise -- and every requests-per-route figure the tool reported (the one
    number an operator uses to choose --max-rpm) was measured on traffic the
    operator believed had been suppressed. A no-op flag is not a missing
    feature; it is a false claim the tool makes about its own behaviour.

    The stylesheet half is the other half of the contract: CSS must still load,
    because every snapshot decides what is `visible()` from computed style, so a
    crawl without it would report a different set of controls.
    """
    common = ("--single-pass", "--delay-ms", "0", "--max-rpm", "0", "--max-depth", "0")
    with _asset_server() as base:
        site = _map(base, *common, "--block-assets")
        blocked = list(_AssetHandler.seen)
    assert not [p for p in blocked if p.startswith("/img/")], (
        f"--block-assets still fetched images: {blocked}"
    )
    assert "/style.css" in blocked, "stylesheets must NOT be blocked"
    # Requested and effective are different claims; the map must state the latter.
    assert site["asset_blocking"] == "chromium-cdp", site["asset_blocking"]

    with _asset_server() as base:
        site = _map(base, *common, "--no-block-assets")
        allowed = list(_AssetHandler.seen)
    # Without this the assertion above could pass on a page that never had images.
    assert [p for p in allowed if p.startswith("/img/")], (
        f"fixture never loaded images even unblocked: {allowed}"
    )
    assert site["asset_blocking"] == "off", site["asset_blocking"]


def test_fonts_are_never_in_the_asset_blocklist():
    """Fonts look like the safest thing to block and are the most dangerous.

    Blocking them took isekaizero.com from 159 controls to ZERO with an empty
    body: its nav is an icon font and the app gates its render on the font
    resolving. That failure is invisible -- the crawl does not error, it reports
    a confident empty map -- so the constant is asserted directly rather than
    trusted to a fixture, since reproducing a font-gated render locally would
    test the fixture more than the rule.
    """
    from engine.browser import _BLOCKED_URL_PATTERNS

    for ext in ("woff", "woff2", "ttf", "otf", "eot", "css", "js"):
        assert f"*.{ext}" not in _BLOCKED_URL_PATTERNS, (
            f"*.{ext} must never be blocked: fonts and CSS gate render and "
            f"visibility; scripts are the DOM on an SPA"
        )
    assert "*.png" in _BLOCKED_URL_PATTERNS, "the blocklist must still block images"

