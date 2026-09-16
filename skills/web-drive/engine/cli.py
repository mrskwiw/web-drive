"""Engine CLI — the agent's hands.

Ships ``map`` (route-graph crawl), ``read`` (one page's declared surface),
``probe`` (check a claim against what navigation proves), ``verify`` (execute
a candidate capability), and ``extract`` (container/field specs -> structured
records). ``generate`` (Phase G, the driver/runtime) is not built yet — see
``docs/WEB_DRIVE_SPECIFICATION.md``.

Session bundles are the SAME format web-qa's ``flow --save-session`` writes, so
a session established by either skill is replayable by the other. That
compatibility is deliberate: establishing auth is the expensive, rate-limited
step, and making the two skills share it means a site only has to be logged
into once.

Invoke as a module from the skill dir::

    python -m engine.cli map --url https://example.com --output sitemap.json
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

import click

from .browser import BrowserController
from .extract import extract_records
from .generate import write_driver
from .models import BrowserEngine
from .probe import (
    is_probe_safe,
    probe_control,
    probe_form_requiredness,
    probe_precondition,
)
from .read import read_surface
from .sitemap import crawl
from .verify import verify_capability

_ENGINE_CHOICE = click.Choice([e.value for e in BrowserEngine])


def _emit(payload: Dict[str, Any], output: str | None) -> None:
    """Print JSON to stdout, and also write it to ``output`` when given."""
    text = json.dumps(payload, indent=2)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    click.echo(text)


def _load_session(session: str | None) -> tuple[Any, str | None]:
    """Load a saved auth session bundle -> (storage_state, user_agent)."""
    if not session:
        return None, None
    data = json.loads(Path(session).read_text(encoding="utf-8"))
    return data.get("storage_state"), data.get("user_agent")


def _controller(
    engine: str,
    headless: bool,
    session: str | None = None,
    user_agent: str | None = None,
    block_assets: bool = False,
) -> BrowserController:
    """Build a controller, seeding a saved auth session when provided.

    An explicit ``--user-agent`` overrides the bundle's. Pin the SAME UA used at
    login: auth tokens are commonly bound to a UA+IP fingerprint, so a replay
    under a different UA is rejected.
    """
    storage_state, session_ua = _load_session(session)
    return BrowserController(
        engine=BrowserEngine(engine),
        headless=headless,
        storage_state=storage_state,
        user_agent=user_agent or session_ua,
        block_assets=block_assets,
    )


@click.group()
def cli() -> None:
    """web-drive deterministic engine."""


@cli.command()
@click.option("--url", required=True, help="Entry URL to crawl from.")
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--max-pages",
    default=None,
    type=int,
    help="Stop after this many routes. UNSET BY DEFAULT -- traversal is bounded "
    "by --time-budget-s, not by a page count, because a page cap decides in "
    "advance which parts of a site matter. Disclosed as `capped` when set and hit.",
)
@click.option(
    "--max-depth",
    default=None,
    type=int,
    help="Link depth from the entry URL. Unset by default (no depth limit); the "
    "crawl terminates on same-origin plus visit-once, or on the time budget.",
)
@click.option(
    "--delay-ms",
    default=750,
    help="Pause between routes. On by default -- crawling at full speed trips "
    "real apps' rate limiters, and a throttled page is indistinguishable from "
    "an empty one. Raise it for a strict target; 0 disables (not advised).",
)
@click.option(
    "--max-retries",
    default=3,
    help="Retries with exponential backoff when a route's requests return 429.",
)
@click.option(
    "--max-rpm",
    default=120,
    help="Request-rate budget in requests/minute -- the unit limiters actually "
    "meter. Paces on captured requests, so an asset-heavy page earns a longer "
    "pause than a light one. 0 disables (not advised).",
)
@click.option(
    "--block-assets/--no-block-assets",
    default=True,
    help="Abort image/font/media requests while mapping. They cannot change "
    "routes, titles or links, but dominate the request count that rate limiters "
    "meter. Scripts and STYLESHEETS are never blocked -- an SPA needs scripts to "
    "render, and without CSS the visibility filter reports the wrong controls.",
)
@click.option(
    "--max-per-template",
    default=None,
    type=int,
    help="How many instances of one route template (e.g. /quiz/{uuid}) to walk "
    "before counting the rest as collapsed. Unset by default -- every instance "
    "is walked. Set it for a fast structural survey of a content-heavy site.",
)
@click.option(
    "--max-probes",
    default=12,
    type=int,
    help="Cap button probes per route. This is a WORK cap, not a coverage cap -- "
    "it bounds effort per page, not what is reachable -- and unsetting it makes "
    "the map SMALLER: unlimited, a 146-control page spends ~650 requests on "
    "itself and a breadth-first walk never leaves depth 1 (measured: 13 routes "
    "uncapped vs 20 at 8). 0 means no cap.",
)
@click.option(
    "--max-query-variants",
    default=3,
    type=int,
    help="How many distinct query strings to walk per path template. A faceted "
    "browse page is ONE route with a parameter space: every filter chip mints a "
    "URL, so the space is combinatorial and an uncapped crawl of one never "
    "converges (isekaizero's /explore produced 37 variants and ate a 45-minute "
    "budget). Variants beyond the sample are still COUNTED, and their parameter "
    "names and values are recorded in `templates[].params` regardless. 0 means "
    "no cap.",
)
@click.option(
    "--time-budget-s",
    default=3600,
    help="Wall-clock guard for the WHOLE run, including auto-resume legs. This "
    "is the primary bound now that coverage caps are off: it limits cost without "
    "pre-judging which parts of a site matter, and it is recoverable -- the "
    "frontier travels in the output, so --resume continues from the stopping "
    "point. 0 disables it, which makes the crawl genuinely unbounded.",
)
@click.option(
    "--probe-buttons/--no-probe-buttons",
    default=False,
    help="Also click navigation-looking buttons to discover routes no <a href> "
    "exposes -- SPAs route through onClick constantly. Controls whose label "
    "suggests a state change (delete/save/publish/buy/...) are skipped even so: "
    "the map should describe the site, not what the crawl changed.",
)
@click.option(
    "--until-exhausted/--single-pass",
    default=True,
    help="Keep going until the frontier is empty: on a rate-limit stop, cool "
    "down, LOWER the request budget, and resume from the saved frontier. "
    "--single-pass does one leg and returns whatever it got.",
)
@click.option(
    "--max-legs",
    default=None,
    type=int,
    help="Bound auto-resume legs. Unset by default -- --time-budget-s already "
    "stops a site that never exhausts, and a leg count was a second cap doing "
    "the same job in a unit nobody can reason about.",
)
@click.option(
    "--cooldown-s",
    default=60,
    help="Wait after a rate-limited leg before resuming. Doubles each time a "
    "leg is throttled again, since the limiter's window is unknown.",
)
@click.option(
    "--fill-forms/--no-fill-forms",
    default=False,
    help="Fill and submit non-destructive forms to reach what is behind them "
    "(search, filters, 'continue' gates). Forms marked destructive, and any "
    "form containing a password field, are always skipped.",
)
@click.option(
    "--resume",
    "resume_path",
    type=click.Path(exists=True),
    default=None,
    help="Continue a previous sitemap.json from its saved frontier instead of "
    "re-walking routes already visited. Use after a run stopped on the limiter "
    "or the page cap.",
)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (web-qa `flow --save-session` format) to map "
    "authenticated routes.",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Also write the sitemap JSON here.",
)
def map(  # noqa: A001 — the subcommand really is called `map`
    url: str,
    engine: str,
    headless: bool,
    max_pages: int | None,
    max_depth: int | None,
    delay_ms: int,
    max_retries: int,
    max_rpm: int,
    block_assets: bool,
    max_per_template: int | None,
    max_probes: int,
    max_query_variants: int,
    time_budget_s: int,
    probe_buttons: bool,
    fill_forms: bool,
    until_exhausted: bool,
    max_legs: int | None,
    cooldown_s: int,
    resume_path: str | None,
    session: str | None,
    user_agent: str | None,
    output: str | None,
) -> None:
    """Crawl the same-origin route graph and emit it as JSON.

    Continuable: the emitted sitemap carries its remaining `frontier`, so a run
    stopped by a limiter or a cap can be handed back via --resume rather than
    restarted -- which matters most when the target is the scarce resource.

    Records, per route, where it actually landed (not where a link claimed it
    would go), the document status, and whether access appeared to require
    authentication. Off-origin and non-http links are reported under `skipped`
    rather than silently dropped.

    Polite by default: it pauses --delay-ms between routes, retries a 429'd route
    with exponential backoff, and STOPS rather than emitting rows for pages it
    starved -- reporting `rate_limited` and `stopped_reason` in the output.
    """

    resume = (
        json.loads(Path(resume_path).read_text(encoding="utf-8"))
        if resume_path
        else None
    )

    async def run():
        controller = _controller(engine, headless, session, user_agent, block_assets)
        await controller.launch()
        try:
            return await _crawl_until_done(
                controller,
                url,
                max_pages=max_pages,
                max_depth=max_depth,
                with_session=session is not None,
                delay_ms=delay_ms,
                max_retries=max_retries,
                max_rpm=max_rpm,
                max_per_template=max_per_template,
                max_probes=max_probes or None,
                max_query_variants=max_query_variants or None,
                probe_buttons_enabled=probe_buttons,
                fill_forms_enabled=fill_forms,
                resume=resume,
                until_exhausted=until_exhausted,
                max_legs=max_legs,
                cooldown_s=cooldown_s,
                # Computed ONCE for the whole run, not per leg: a per-leg budget
                # multiplied by an unbounded leg count is not a bound at all.
                deadline=(
                    time.monotonic() + time_budget_s if time_budget_s > 0 else None
                ),
            )
        finally:
            await controller.close()

    site = asyncio.run(run())
    if site.asset_blocking.startswith("unavailable"):
        # The `asset_blocking` field already records this, but a field is only a
        # disclosure to whoever reads it. --block-assets is default-ON, so the
        # operator who never chose it is exactly the one who will not go looking
        # -- and a request budget tuned for a blocked crawl will be ~4x too
        # generous for an unblocked one, i.e. this is a rate-limit foot-gun.
        click.echo(
            f"WARNING: --block-assets requested but {site.asset_blocking}. "
            f"This crawl fetched every image and media file, so expect several "
            f"times the request count -- lower --max-rpm accordingly, or use "
            f"--browser chromium.",
            err=True,
        )
    _emit(site.to_dict(), output)


@cli.command()
@click.option("--url", required=True, help="Page to read.")
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (same format as `map --session`) to read "
    "an authenticated page.",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Also write the surface JSON here.",
)
def read(  # noqa: A001 — the subcommand really is called `read`
    url: str,
    engine: str,
    headless: bool,
    session: str | None,
    user_agent: str | None,
    output: str | None,
) -> None:
    """Extract ONE page's declared surface -> `surface.json`.

    Controls, form schemas (incl. `required`/`maxlength`/`pattern`/`<select>`
    options), headings, aria landmarks, and visible error / empty-state copy.

    This is the READ half of spec §3 -- what the app *claims*. `probe`
    (Phase C, not yet built) is the NAVIGATE half that checks these claims
    against what actually happens; until then, everything here is unverified
    by construction.
    """

    async def run():
        controller = _controller(engine, headless, session, user_agent, False)
        await controller.launch()
        try:
            await controller.navigate(url)
            return await read_surface(controller)
        finally:
            await controller.close()

    surface = asyncio.run(run())
    _emit(surface.to_dict(), output)


@cli.command()
@click.option("--url", required=True, help="Page whose declared surface to probe.")
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (same format as `map --session`).",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--check-preconditions/--no-check-preconditions",
    default=False,
    help="Also re-navigate each navigating control's landing URL in a FRESH "
    "context (same auth, none of the accumulated client-side state) to check "
    "for UNDOCUMENTED_PRECONDITION. Off by default: a second browser launch "
    "per navigating control.",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Also write the reconciliation JSON here.",
)
def probe(
    url: str,
    engine: str,
    headless: bool,
    session: str | None,
    user_agent: str | None,
    check_preconditions: bool,
    output: str | None,
) -> None:
    """Navigate candidate transitions from a page's declared surface and
    record what actually happened -> `reconciliation[]` (spec §3).

    Reads the page fresh (same as `read`), then for each non-form control
    that looks safe to click (spec's mutating-word skip, same as `map
    --probe-buttons`) clicks it and checks ADVERTISED_ABSENT /
    LABEL_ROUTE_MISMATCH; for each non-destructive form, submits once per
    optional field left blank to check OPTIONAL_BUT_REQUIRED; with
    `--check-preconditions`, also checks UNDOCUMENTED_PRECONDITION.

    This is the NAVIGATE half of spec §3 -- what the app actually does, run
    against `read`'s claims. Findings here are facts, not verdicts: naming
    the verb a reconciliation changes is Phase D's job, not this command's.
    """

    async def run():
        controller = _controller(engine, headless, session, user_agent, False)
        await controller.launch()
        try:
            await controller.navigate(url)
            # Captured BEFORE any control is clicked: a precondition check must
            # withhold whatever client-side state this page's OWN probing (or an
            # earlier control's) would otherwise leak into the "cold" context.
            base_state = await controller.context.storage_state()
            surface = await read_surface(controller)
            findings = []
            for c in surface.controls:
                control = c.to_dict()
                if not is_probe_safe(control.get("name", "")):
                    continue
                found = await probe_control(controller, url, control)
                if found:
                    findings.append(found)
                if check_preconditions:
                    pre = await probe_precondition(
                        controller,
                        url,
                        control,
                        base_state=base_state,
                        browser_engine=BrowserEngine(engine),
                        user_agent=user_agent,
                    )
                    if pre:
                        findings.append(pre)
            for f in surface.forms:
                findings.extend(
                    await probe_form_requiredness(controller, url, f.to_dict())
                )
            return findings
        finally:
            await controller.close()

    findings = asyncio.run(run())
    _emit(
        {"url": url, "reconciliation": [f.to_dict() for f in findings]},
        output,
    )


@cli.command()
@click.option("--url", required=True, help="Entry URL to navigate to first.")
@click.option("--verb", required=True, help="Capability name, e.g. 'quiz list'.")
@click.option(
    "--steps",
    "steps_path",
    required=True,
    type=click.Path(exists=True),
    help="JSON file: a list of flow-style step objects (web-qa's schema, "
    "spec D6). Secrets referenced as {\"env\": \"VAR\"} or ${VAR}.",
)
@click.option(
    "--assert",
    "assert_file",
    type=click.Path(exists=True),
    default=None,
    help="JSON file: the capability-level assertion, checked against the "
    "LAST step's evidence (same keys as a per-step `assert` -- see "
    "engine.flow.evaluate_assertion). Omit to assert nothing beyond every "
    "step's own gate and per-step assertion passing.",
)
@click.option(
    "--destructive/--no-destructive",
    default=False,
    help="Declare this candidate destructive. Refuses to run (exit 4) unless "
    "--yes is also passed -- spec §7: verifying a mutating/destructive verb "
    "means really performing it, so it requires explicit confirmation.",
)
@click.option(
    "--yes", is_flag=True, default=False, help="Confirm running a --destructive candidate."
)
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (same format as `map --session`).",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Also write the verify-result JSON here.",
)
def verify(
    url: str,
    verb: str,
    steps_path: str,
    assert_file: str | None,
    destructive: bool,
    yes: bool,
    engine: str,
    headless: bool,
    session: str | None,
    user_agent: str | None,
    output: str | None,
) -> None:
    """Execute a candidate capability's steps and report verified/withheld.

    Runs every step in ONE persistent context (auth carries across steps),
    checking each step's deterministic gate + per-step `assert`, then the
    capability-level `--assert` against the last step's evidence. Halts at
    the first failing step -- a capability whose steps stop partway is
    unverified, with a reason naming exactly where, never "verified with
    caveats" (spec §5's verify-or-withhold discipline).

    Exit codes: 0 verified, 1 ran but did not verify, 4 refused (destructive
    without --yes).
    """
    if destructive and not yes:
        refused: dict[str, Any] = {
            "verb": verb,
            "verified": False,
            "reason": "refused: destructive candidate requires --yes",
            "steps": [],
        }
        _emit(refused, output)
        sys.exit(4)

    steps = json.loads(Path(steps_path).read_text(encoding="utf-8"))
    final_assert = (
        json.loads(Path(assert_file).read_text(encoding="utf-8")) if assert_file else None
    )

    async def run():
        controller = _controller(engine, headless, session, user_agent, False)
        await controller.launch()
        try:
            await controller.navigate(url)
            return await verify_capability(controller, verb, steps, final_assert)
        finally:
            await controller.close()

    result = asyncio.run(run())
    _emit(result.to_dict(), output)
    if not result.verified:
        sys.exit(1)


@cli.command()
@click.option("--url", required=True, help="Page to extract records from.")
@click.option(
    "--spec",
    "spec_path",
    required=True,
    type=click.Path(exists=True),
    help="JSON file: {\"container\": {...}, \"fields\": {...}} -- spec §5's "
    "`extract` key. `container` finds each repeated item (`selector` or "
    "`role`, e.g. {\"role\": \"listitem\"}); each entry in `fields` finds one "
    "value inside it (`selector`/`role` + optional `attr`; no `attr` reads "
    "text content).",
)
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option("--headless/--no-headless", default=True)
@click.option(
    "--session",
    type=click.Path(exists=True),
    default=None,
    help="Reuse a saved auth session (same format as `map --session`).",
)
@click.option(
    "--user-agent",
    default=None,
    help="Override the user-agent (defaults to the one saved in --session).",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Also write the extracted records JSON here.",
)
def extract(
    url: str,
    spec_path: str,
    engine: str,
    headless: bool,
    session: str | None,
    user_agent: str | None,
    output: str | None,
) -> None:
    """Pull structured records from a listing/detail page -> `records.json`.

    One record per element `container` matches, each field read from inside
    it per `fields`. Deterministic and literal: it does not infer a container
    or guess field names -- write the spec from what `read` already told you
    about the page's controls and landmarks.
    """
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))

    async def run():
        controller = _controller(engine, headless, session, user_agent, False)
        await controller.launch()
        try:
            await controller.navigate(url)
            return await extract_records(
                controller, spec.get("container", {}), spec.get("fields", {})
            )
        finally:
            await controller.close()

    records = asyncio.run(run())
    _emit({"url": url, "records": records}, output)


@cli.command()
@click.option(
    "--catalog",
    "catalog_path",
    required=True,
    type=click.Path(exists=True),
    help="site.json to generate a driver from (spec §5's schema).",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(),
    default=None,
    help="Output directory. Default: repo-root drivers/<slug> (../../../drivers "
    "from a skill dir), alongside reports/ and blueprints/.",
)
def generate(catalog_path: str, out_dir: str | None) -> None:
    """Render `drivers/<slug>/` -- shim, catalog, manual, and a self-contained
    `_engine/` (copied byte-identical, spec D2) -- from a capability catalog.

    The result is standalone: a cold shell given only `drivers/<slug>/` can
    run every capability without the web-drive skill installed (spec §12).
    """
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    slug = catalog["site"]["slug"]
    target = Path(out_dir) if out_dir else Path(__file__).resolve().parents[4] / "drivers" / slug
    write_driver(catalog, target, source_engine_dir=Path(__file__).resolve().parent)
    _emit(
        {
            "generated": str(target),
            "slug": slug,
            "capabilities": len(catalog.get("capabilities", [])),
            "unverified": len(catalog.get("unverified", [])),
        },
        None,
    )


def _apply_totals(site, totals) -> None:
    rl = site.rate_limit
    rl.requests_total = totals["requests"]
    rl.elapsed_s = totals["elapsed"]
    rl.throttled_requests = totals["throttled"]
    rl.effective_rpm = (
        totals["requests"] / totals["elapsed"] * 60.0 if totals["elapsed"] else 0.0
    )
    if site.routes:
        rl.requests_per_route = totals["requests"] / len(site.routes)


async def _crawl_until_done(
    controller, url, *, until_exhausted, max_legs, cooldown_s, deadline, **kw
):
    """Run legs until the frontier empties, adapting to the limiter as it goes.

    A rate-limited stop is not a failure, it is information: the crawl already
    knows how to record its frontier, so the only thing a human was adding by
    re-running `--resume` was patience. Each throttled leg cools down longer and
    lowers the request budget, so the crawl converges on a rate the target will
    actually tolerate instead of guessing one up front.
    """
    state = kw.pop("resume", None)
    rpm = kw.pop("max_rpm")
    cooldown = cooldown_s
    site = None
    totals = {"requests": 0, "elapsed": 0.0, "throttled": 0, "legs": 0}
    leg = 0
    routes_before = -1
    while max_legs is None or leg < max_legs:
        leg += 1
        routes_before = len(site.routes) if site is not None else -1
        site = await crawl(
            controller, url, max_rpm=rpm, resume=state, deadline=deadline, **kw
        )
        # The profile must describe the whole RUN. Reporting only the final leg
        # made a run whose last leg found nothing report zero requests -- a
        # tuning number that is not merely wrong but inverted.
        totals["requests"] += site.rate_limit.requests_total
        totals["elapsed"] += site.rate_limit.elapsed_s
        totals["throttled"] += site.rate_limit.throttled_requests
        totals["legs"] = leg
        _apply_totals(site, totals)
        if not until_exhausted or not site.frontier:
            break
        # A page cap is a TOTAL, so a resumed leg starts already at it, fetches
        # nothing and hands back the same frontier -- forever. `--max-legs` was
        # quietly serving as the livelock guard; removing it exposed that the
        # loop never had a real one. Progress, not a leg count, is the condition
        # that actually matters here.
        if site.capped:
            site.stopped_reason = (
                (site.stopped_reason or "")
                + f" | stopped at the --max-pages cap with {len(site.frontier)} "
                f"route(s) queued; resuming cannot pass a cap counted over the "
                f"whole map. Raise --max-pages or drop it."
            ).strip(" |")
            break
        if len(site.routes) <= routes_before:
            site.stopped_reason = (
                (site.stopped_reason or "")
                + f" | auto-resume made no progress on leg {leg} with "
                f"{len(site.frontier)} route(s) still queued -- every remaining "
                f"route failed or was refused, so further legs would only repeat."
            ).strip(" |")
            break
        if site.timed_out or (deadline is not None and time.monotonic() >= deadline):
            # `crawl` already recorded why and left the frontier intact. Starting
            # another leg past the deadline would spend the budget the guard
            # exists to hold.
            break
        if site.rate_limited:
            # Back off on BOTH axes: wait longer, and ask for less next time.
            # Never cool down past the deadline -- sleeping through the budget
            # and then reporting "stopped on time" would blame the clock for a
            # wait we chose.
            if deadline is not None:
                cooldown = min(cooldown, max(0, deadline - time.monotonic()))
            await asyncio.sleep(cooldown)
            cooldown = min(max(cooldown, 1) * 2, 600)
            rpm = max(20, int(rpm * 0.6))
        state = site.to_dict()
    if site is not None:
        _apply_totals(site, totals)
    # Only an auto-resume run can 'give up', and only for a reason not already
    # recorded. On --single-pass a non-empty frontier is the expected outcome,
    # and a timed-out run already carries `crawl`'s own explanation -- appending
    # a leg-exhaustion note there would attribute the stop to the wrong guard.
    if (
        until_exhausted
        and site is not None
        and site.frontier
        and not site.timed_out
        and max_legs is not None
        and leg >= max_legs
    ):
        site.stopped_reason = (
            (site.stopped_reason or "")
            + f" | auto-resume stopped after {max_legs} legs with "
            f"{len(site.frontier)} route(s) still queued -- re-run with --resume."
        ).strip(" |")
    return site


async def wait_for_manual_login(
    controller: BrowserController,
    until_url: str | None,
    until_selector: str | None,
    timeout_s: int,
    poll_ms: int = 500,
) -> bool:
    """Poll until the human has finished authenticating, or the clock runs out.

    Polling rather than a fixed sleep matters: an SSO round trip can take five
    seconds or ninety depending on whether a 2FA prompt appears, and a fixed wait
    either wastes the difference or saves a half-authenticated context.

    With no condition given we simply wait out ``timeout_s`` and save whatever
    state exists -- crude, but it is the honest fallback for a flow whose success
    page we cannot predict.
    """
    waited = 0
    while waited < timeout_s * 1000:
        if until_url and until_url in controller.page.url:
            return True
        if until_selector:
            try:
                if await controller.is_present(until_selector):
                    return True
            except Exception:  # noqa: BLE001 — mid-navigation; try again next poll
                pass
        await asyncio.sleep(poll_ms / 1000)
        waited += poll_ms
    return not (until_url or until_selector)


@cli.command()
@click.option(
    "--url", required=True, help="Where to start -- usually the app's login page."
)
@click.option(
    "--save-session",
    "save_path",
    required=True,
    type=click.Path(),
    help="Where to write the session bundle, for later --session replay.",
)
@click.option(
    "--until-url",
    default=None,
    help="Substring of the URL that means you're in (e.g. /dashboard). Polled.",
)
@click.option(
    "--until-selector",
    default=None,
    help="Selector that appears once authenticated. Polled. Use instead of "
    "--until-url when the app lands back on the same path.",
)
@click.option(
    "--timeout-s", default=300, help="How long you get to finish. Default 5 min."
)
@click.option(
    "--browser", "engine", default=BrowserEngine.CHROMIUM.value, type=_ENGINE_CHOICE
)
@click.option(
    "--headless/--no-headless",
    default=False,
    help="Headed by DEFAULT -- you cannot complete a login you cannot see.",
)
@click.option(
    "--user-agent",
    default=None,
    help="Pin the user-agent. Whatever is used here MUST be reused on every "
    "--session replay: tokens are commonly bound to a UA+IP fingerprint, so a "
    "bundle saved under one UA and replayed under another is rejected.",
)
def login(
    url: str,
    save_path: str,
    until_url: str | None,
    until_selector: str | None,
    timeout_s: int,
    engine: str,
    headless: bool,
    user_agent: str | None,
) -> None:
    """Open a real browser, let a HUMAN authenticate, then save the session.

    This exists for the logins a script cannot drive: Google/SSO (which actively
    blocks automated browsers), passkeys, MFA, magic links. Automating those is
    an arms race a QA tool should not enter -- but nothing stops us from letting
    you sign in once by hand and reusing the result.

    What gets saved is the APP's session (cookies + localStorage), not anything
    of the identity provider's. After the redirect completes the provider is out
    of the picture, which is why one manual login unlocks every later headless
    run until the token expires.

        python -m engine.cli login --url https://app.example.com/login             --until-url /dashboard --save-session .qa/session.json
        python -m engine.cli map --url https://app.example.com/dashboard             --session .qa/session.json
    """

    async def run():
        controller = _controller(engine, headless, None, user_agent, False)
        await controller.launch()
        try:
            await controller.navigate(url)
            click.echo(f"Browser open at {url}", err=True)
            click.echo(
                "Sign in however you need to -- OAuth, SSO, MFA, magic link.",
                err=True,
            )
            if until_url:
                cond = f"until the URL contains {until_url!r}"
            elif until_selector:
                cond = f"until {until_selector!r} appears"
            else:
                cond = "the full window (no success condition given)"
            click.echo(f"Waiting {cond}, up to {timeout_s}s.", err=True)
            ok = await wait_for_manual_login(
                controller, until_url, until_selector, timeout_s
            )
            saved = await controller.save_session(save_path, user_agent=user_agent)
            state = await controller.context.storage_state()
            # Report the UA that was actually SAVED, not the flag we were handed:
            # with no --user-agent the two differ, and the saved one is what every
            # later --session replay must be pinned to. Reporting the flag would
            # tell the operator "null" for a bundle that has a real UA in it.
            saved_ua = json.loads(Path(saved).read_text(encoding="utf-8")).get(
                "user_agent"
            )
            return {
                "saved": saved,
                "detected_login": ok,
                "final_url": controller.page.url,
                "cookies": len(state.get("cookies", [])),
                "user_agent": saved_ua,
            }
        finally:
            await controller.close()

    result = asyncio.run(run())
    if not result["detected_login"]:
        # Saved anyway -- a bundle from a half-finished login is still worth
        # inspecting -- but never reported as success.
        click.echo(
            "WARNING: the success condition was never met. The bundle was saved "
            "but may not be authenticated; verify with a --session run before "
            "trusting it.",
            err=True,
        )
    _emit(result, None)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
