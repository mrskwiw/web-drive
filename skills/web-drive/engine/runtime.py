"""``runtime`` — Phase G: the command tree a GENERATED driver's shim loads.

Not per-site code (spec D2): ``generate`` copies this module — and the pure
engine modules it depends on (``browser``, ``models``, ``accessibility``,
``evidence``, ``gate``, ``flow``, ``verify``, ``extract``) — BYTE-IDENTICAL
into every ``drivers/<slug>/_engine/``, so it is tested exactly once here and
never regenerated per site. It reads ``site.json``, builds one noun group and
one verb subcommand per capability, substitutes CLI flags into each step's
``${NAME}`` references (reusing ``flow.resolve_str`` verbatim — the same
mechanism a step already uses for env-var secrets), executes via
``verify_capability``, and enforces the terminal contract (spec §6):
``--json``/``--yes``/``--session``/``--dry-run``/``--timeout``, and exit
codes 0-5.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from .browser import BrowserController
from .extract import extract_records
from .models import BrowserEngine
from .verify import VerifyResult, verify_capability

# Exit codes (spec §6). 5 (engine/browser error) is whatever click's own
# uncaught-exception handling already does -- not special-cased here.
EXIT_OK = 0
EXIT_ASSERTION_FAILED = 1
EXIT_PRECONDITION_FAILED = 2
EXIT_DRIFT = 3
EXIT_REFUSED = 4

def _load_session(session_path: Optional[str]) -> tuple[Any, Optional[str]]:
    if not session_path or not Path(session_path).exists():
        return None, None
    data = json.loads(Path(session_path).read_text(encoding="utf-8"))
    return data.get("storage_state"), data.get("user_agent")


def _exit_code_for(result: VerifyResult) -> int:
    if result.verified:
        return EXIT_OK
    reason = (result.reason or "").lower()
    if "missing secret" in reason:
        return EXIT_PRECONDITION_FAILED
    # Drift is a real signal now (VerifyResult.drift, set only when a step's
    # OWN failure was Playwright's TimeoutError -- a locator that genuinely
    # never resolved), not a substring match against an arbitrary exception's
    # text. The old `_DRIFT_MARKERS` list (searching `reason` for "timeout",
    # "not found", etc.) misclassified any unrelated error whose message
    # happened to contain one of those words -- a network timeout, a custom
    # app exception -- as selector drift. See plan v2.0 WD-T2/S2.
    if result.drift:
        return EXIT_DRIFT
    return EXIT_ASSERTION_FAILED


_PARAM_TYPES: Dict[str, type] = {"number": float}


def _param_option(p: Dict[str, Any]) -> click.Option:
    name = p["name"]
    flag = f"--{name}"
    if p.get("type") == "boolean":
        return click.Option([flag], is_flag=True, default=False)
    py_type = _PARAM_TYPES.get(str(p.get("type", "string")), str)
    return click.Option(
        [flag], type=py_type, required=bool(p.get("required")), default=None
    )


def _run_capability(
    site: Dict[str, Any],
    cap: Dict[str, Any],
    kwargs: Dict[str, Any],
) -> None:
    param_names = {p["name"] for p in cap.get("params", [])}
    values = {k: v for k, v in kwargs.items() if k in param_names and v is not None}
    yes = bool(kwargs.get("yes"))
    as_json = bool(kwargs.get("as_json"))
    dry_run = bool(kwargs.get("dry_run"))
    session_path = kwargs.get("session")
    timeout_ms = int(kwargs.get("timeout_ms") or 15000)

    steps: List[Dict[str, Any]] = cap.get("steps", [])

    if dry_run:
        click.echo(json.dumps({"verb": cap["verb"], "dry_run": True, "steps": steps}, indent=2))
        raise SystemExit(EXIT_OK)

    if (cap.get("destructive") or cap.get("costs")) and not yes:
        reasons = []
        if cap.get("destructive"):
            reasons.append("destructive")
        if cap.get("costs"):
            reasons.append("costed")
        click.echo(
            json.dumps(
                {
                    "verb": cap["verb"],
                    "verified": False,
                    "reason": f"refused: {'/'.join(reasons)} verb requires --yes",
                }
            )
        )
        raise SystemExit(EXIT_REFUSED)

    storage_state, session_ua = _load_session(session_path)
    if "auth" in cap.get("preconditions", []) and storage_state is None:
        click.echo(
            json.dumps(
                {
                    "verb": cap["verb"],
                    "verified": False,
                    "reason": "precondition failed: not authenticated -- run `login` "
                    "or pass --session",
                }
            )
        )
        raise SystemExit(EXIT_PRECONDITION_FAILED)

    env = {**os.environ, **{k: str(v) for k, v in values.items()}}

    async def run() -> tuple[VerifyResult, List[Dict[str, Any]]]:
        controller = BrowserController(
            engine=BrowserEngine.CHROMIUM,
            headless=True,
            timeout_ms=timeout_ms,
            storage_state=storage_state,
            user_agent=session_ua,
        )
        await controller.launch()
        try:
            base_url = site["site"]["base_url"]
            await controller.navigate(base_url)
            result = await verify_capability(controller, cap["verb"], steps, cap.get("assert"), env)
            records: List[Dict[str, Any]] = []
            if result.verified and cap.get("extract"):
                spec = cap["extract"]
                records = await extract_records(
                    controller, spec.get("container", {}), spec.get("fields", {})
                )
            return result, records
        finally:
            await controller.close()

    result, records = asyncio.run(run())
    code = _exit_code_for(result)
    payload: Dict[str, Any] = {"verb": cap["verb"], "verified": result.verified}
    if result.reason:
        payload["reason"] = result.reason
    if records:
        payload["records"] = records
    if as_json or not result.verified:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"{cap['verb']}: ok")
    raise SystemExit(code)


def _make_verb_command(site: Dict[str, Any], cap: Dict[str, Any]) -> click.Command:
    _, _, verb_part = cap["verb"].partition(" ")
    params: List[click.Parameter] = [_param_option(p) for p in cap.get("params", [])]
    params += [
        click.Option(
            ["--yes"],
            is_flag=True,
            default=False,
            help="Confirm a destructive and/or costed verb.",
        ),
        click.Option(["--json", "as_json"], is_flag=True, default=False, help="JSON output."),
        click.Option(
            ["--dry-run"], is_flag=True, default=False, help="Print steps without executing."
        ),
        click.Option(
            ["--session"], type=click.Path(), default=None, help="Auth session bundle."
        ),
        click.Option(
            ["--timeout"], "timeout_ms", type=int, default=None, help="Per-action timeout (ms)."
        ),
    ]

    def callback(**kwargs: Any) -> None:
        _run_capability(site, cap, kwargs)

    return click.Command(
        name=verb_part or cap["verb"],
        params=params,
        callback=callback,
        help=cap.get("summary"),
    )


def _capabilities_command(site: Dict[str, Any]) -> click.Command:
    def callback() -> None:
        click.echo(json.dumps(site.get("capabilities", []), indent=2))

    return click.Command(
        name="capabilities", callback=callback, help="Machine-readable capability catalog."
    )


def _manual_command(site: Dict[str, Any], guide_path: Optional[Path]) -> click.Command:
    def callback() -> None:
        if guide_path and guide_path.exists():
            click.echo(guide_path.read_text(encoding="utf-8"))
        else:
            click.echo("No SITEGUIDE.md found alongside this driver.")

    return click.Command(name="manual", callback=callback, help="Print SITEGUIDE.md.")


def _doctor_command(site: Dict[str, Any]) -> click.Command:
    def callback() -> None:
        async def run() -> Dict[str, Any]:
            controller = BrowserController(engine=BrowserEngine.CHROMIUM, headless=True)
            await controller.launch()
            try:
                await controller.navigate(site["site"]["base_url"])
                snapshot = await controller.capture_snapshot()
            finally:
                await controller.close()
            expected = site["site"].get("fingerprint", {})
            observed_title = snapshot.title
            drift = bool(expected.get("title")) and expected["title"] != observed_title
            return {
                "drift": drift,
                "expected_title": expected.get("title"),
                "observed_title": observed_title,
            }

        report = asyncio.run(run())
        click.echo(json.dumps(report, indent=2))
        raise SystemExit(EXIT_DRIFT if report["drift"] else EXIT_OK)

    return click.Command(name="doctor", callback=callback, help="Re-check the site fingerprint.")


def build_cli(site: Dict[str, Any], guide_path: Optional[Path] = None) -> click.Group:
    """Build the full command tree for one site's catalog."""
    slug = site.get("site", {}).get("slug", "driver")
    root = click.Group(name=slug, help=f"Generated driver for {slug}.")
    nouns: Dict[str, click.Group] = {}
    for cap in site.get("capabilities", []):
        noun, _, _ = cap["verb"].partition(" ")
        grp = nouns.setdefault(noun, click.Group(name=noun))
        grp.add_command(_make_verb_command(site, cap))
    for grp in nouns.values():
        root.add_command(grp)
    root.add_command(_capabilities_command(site))
    root.add_command(_doctor_command(site))
    root.add_command(_manual_command(site, guide_path))
    return root


def main(site_path: str, guide_path: Optional[str] = None, argv: Optional[List[str]] = None) -> None:
    """Entry point the generated shim calls: load site.json, build, dispatch."""
    site = json.loads(Path(site_path).read_text(encoding="utf-8"))
    guide = Path(guide_path) if guide_path else None
    build_cli(site, guide).main(args=argv, standalone_mode=True)
