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
    help="Abort image/font/media/stylesheet requests while mapping. They cannot "
    "change routes, titles or links, but dominate the request count that rate "
    "limiters meter. Scripts are never blocked -- an SPA needs them to render.",
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
            return await crawl(
                controller,
                url,
                max_pages=max_pages,
                max_depth=max_depth,
                delay_ms=delay_ms,
                max_retries=max_retries,
                max_rpm=max_rpm,
                max_per_template=max_per_template,
                probe_buttons_enabled=probe_buttons,
                resume=resume,
                with_session=session is not None,
            )
        finally:
            await controller.close()

    site = asyncio.run(run())
    _emit(site.to_dict(), output)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
