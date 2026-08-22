"""Engine CLI — the agent's hands.

Phase A ships one subcommand: ``map`` (route-graph crawl). ``read``, ``probe``,
``verify``, ``extract`` and ``generate`` follow in Phases B-G — see
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
from pathlib import Path
from typing import Any, Dict

import click

from .browser import BrowserController
from .models import BrowserEngine
from .sitemap import crawl

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
    default=40,
    help="Stop after this many routes. The cap is disclosed as `capped` in the output.",
)
@click.option("--max-depth", default=3, help="Link depth from the entry URL.")
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
    default=3,
    help="How many instances of one route template (e.g. /quiz/{uuid}) to walk "
    "before counting the rest as collapsed.",
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
    default=12,
    help="Safety bound on auto-resume legs, so a site that never exhausts (or a "
    "limiter that never relents) cannot loop forever.",
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
    max_pages: int,
    max_depth: int,
    delay_ms: int,
    max_retries: int,
    max_rpm: int,
    block_assets: bool,
    max_per_template: int,
    probe_buttons: bool,
    fill_forms: bool,
    until_exhausted: bool,
    max_legs: int,
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
                probe_buttons_enabled=probe_buttons,
                fill_forms_enabled=fill_forms,
                resume=resume,
                until_exhausted=until_exhausted,
                max_legs=max_legs,
                cooldown_s=cooldown_s,
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
    controller, url, *, until_exhausted, max_legs, cooldown_s, **kw
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
    for leg in range(1, max_legs + 1):
        site = await crawl(controller, url, max_rpm=rpm, resume=state, **kw)
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
        if site.rate_limited:
            # Back off on BOTH axes: wait longer, and ask for less next time.
            await asyncio.sleep(cooldown)
            cooldown = min(cooldown * 2, 600)
            rpm = max(20, int(rpm * 0.6))
        state = site.to_dict()
    if site is not None:
        _apply_totals(site, totals)
    # Only an auto-resume run can 'give up'. On --single-pass a non-empty
    # frontier is the expected outcome, and claiming otherwise would report a
    # failure that never happened.
    if until_exhausted and site is not None and site.frontier:
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
            return {
                "saved": saved,
                "detected_login": ok,
                "final_url": controller.page.url,
                "cookies": len(state.get("cookies", [])),
                "user_agent": user_agent,
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
