"""``generate`` — Phase G: render `drivers/<slug>/` from a capability catalog.

Spec D2: a thin shim + a copied, tested runtime, not per-site emitted code.
The shim is ~10 lines; everything else it needs travels with it in
`_engine/`, copied BYTE-IDENTICAL from this skill's own engine modules, so
`drivers/<slug>/` is self-sufficient — a cold shell with only that directory
can run it without the web-drive skill installed (spec §12's success
criterion).
"""

from __future__ import annotations

import json
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Dict

import click

# The pure, dependency-closed subset `runtime.py` actually needs at execution
# time. Deliberately NOT map.py/read.py/probe.py/sitemap.py/cli.py/generate.py
# themselves -- those are GENERATION-time tools; a driver never crawls or
# reconciles, it only replays verified steps.
_RUNTIME_MODULES = (
    "models.py",
    "accessibility.py",
    "browser.py",
    "evidence.py",
    "gate.py",
    "flow.py",
    "verify.py",
    "extract.py",
    "runtime.py",
)

_SHIM_TEMPLATE = '''#!/usr/bin/env python3
"""Generated driver shim for {slug}. Do not edit -- re-run `generate`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _engine.runtime import main  # noqa: E402

if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    main(str(here / "site.json"), str(here / "SITEGUIDE.md"), sys.argv[1:])
'''

_CMD_TEMPLATE = "@echo off\r\npython \"%~dp0{slug}\" %*\r\n"

# Fields on a step's recorded `action` that can hold a resolved secret or any
# other typed-in value (a real business name, an address, ...) -- redacted
# unconditionally in copied evidence, not just for params marked `secret`.
# The evidence trail exists to prove a gate/assertion passed, not to publish
# what was typed; `{"env": "VAR"}` / `${VAR}` resolve to the LITERAL value
# before `verify` ever records it (flow.py's own docstring: "resolving
# secrets by env-var reference so ... [nothing] downstream ever sees the
# literal secret" describes the CATALOG, not the recorded evidence -- the
# executed `Action.value` is real, and neither `evidence.py` nor `flow.py`
# redacts it before serialization).
_REDACTED_ACTION_FIELDS = ("value", "text")
_REDACTED = "[redacted by generate -- see SITEGUIDE.md/spec §5]"


def _redact_evidence(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Strip typed-in values from a verify-result bundle before it ships
    inside a driver anyone might receive. Keeps everything that proves a
    capability passed (gate checks, assertions, URLs, HTTP status/method,
    console/page errors) -- only the literal field VALUES a step typed are
    replaced, since those can be a resolved secret or just sensitive data
    (a real name, address, ...) nobody asked to redistribute.
    """
    bundle = json.loads(json.dumps(bundle))  # deep copy; this is small JSON
    for step in bundle.get("steps", []):
        action = step.get("bundle", {}).get("action")
        if not isinstance(action, dict):
            continue
        for field in _REDACTED_ACTION_FIELDS:
            if action.get(field):
                action[field] = _REDACTED
    return bundle


def _copy_evidence(catalog: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    """Copy each capability's referenced evidence file into `out_dir/runs/`,
    redacted, and repoint the OUTPUT catalog's `evidence` field at the copy.

    Evidence paths in the INPUT catalog are resolved relative to the current
    working directory -- the same directory `verify --output ...` and this
    `generate` call are both normally run from. A missing file is reported
    (never silently ignored) and left pointing at its original path rather
    than claiming a copy that didn't happen.
    """
    runs_dir = out_dir / "runs"
    for cap in catalog.get("capabilities", []):
        src_str = cap.get("evidence")
        if not src_str:
            continue
        src = Path(src_str)
        if not src.exists():
            click.echo(
                f"warning: evidence file for {cap.get('verb')!r} not found "
                f"at {src} -- 'evidence' left as-is, nothing copied",
                err=True,
            )
            continue
        try:
            bundle = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            click.echo(
                f"warning: evidence file for {cap.get('verb')!r} at {src} "
                f"could not be read ({exc}) -- 'evidence' left as-is",
                err=True,
            )
            continue
        runs_dir.mkdir(exist_ok=True)
        verb_slug = re.sub(r"[^a-z0-9]+", "-", cap["verb"].lower()).strip("-")
        dest_name = f"{verb_slug}.json"
        (runs_dir / dest_name).write_text(
            json.dumps(_redact_evidence(bundle), indent=2), encoding="utf-8"
        )
        cap["evidence"] = f"runs/{dest_name}"
    return catalog


def render_siteguide(catalog: Dict[str, Any]) -> str:
    """A human-facing operating manual -- SITEGUIDE.md. Machine-facing
    discovery is `capabilities --json`; this is the same information written
    for a person deciding what to call."""
    site = catalog.get("site", {})
    slug = site.get("slug", "driver")
    lines = [f"# {slug} — operating manual", ""]
    lines.append(f"Base URL: {site.get('base_url', '(unknown)')}")
    lines.append(f"Generated: {site.get('generated_at', '(unknown)')}")
    lines.append("")

    auth = catalog.get("auth", {})
    if auth.get("required"):
        lines.append("## Authentication")
        lines.append("")
        lines.append(f"Required. Run `./{slug} login` once, or pass `--session <bundle>`.")
        for cred in auth.get("credentials", []):
            secret = " (secret)" if cred.get("secret") else ""
            lines.append(f"- `{cred['param']}` from env var `{cred['env']}`{secret}")
        lines.append("")

    lines.append("## Capabilities")
    lines.append("")
    for cap in catalog.get("capabilities", []):
        verb = cap["verb"]
        kind = cap.get("kind", "read")
        needs_yes = []
        if cap.get("destructive"):
            needs_yes.append("destructive")
        if cap.get("costs"):
            needs_yes.append("costs credits/money")
        flag = f" — **{', '.join(needs_yes)}, needs --yes**" if needs_yes else ""
        lines.append(f"### `{verb}` ({kind}){flag}")
        if cap.get("summary"):
            lines.append("")
            lines.append(cap["summary"])
        params = cap.get("params", [])
        if params:
            lines.append("")
            lines.append("| flag | type | required |")
            lines.append("|---|---|---|")
            for p in params:
                lines.append(
                    f"| `--{p['name']}` | {p.get('type', 'string')} | "
                    f"{'yes' if p.get('required') else 'no'} |"
                )
        lines.append("")

    unverified = catalog.get("unverified", [])
    if unverified:
        lines.append("## Not available (withheld at generation time)")
        lines.append("")
        for u in unverified:
            lines.append(f"- `{u.get('verb')}` — {u.get('reason')}")
        lines.append("")

    return "\n".join(lines)


def _validate_verb_shapes(catalog: Dict[str, Any]) -> None:
    """Reject a malformed ``verb`` before anything is written.

    `runtime.py`'s `build_cli` does `noun, _, _ = cap["verb"].partition(" ")`
    -- if `verb` has no space at all, that degrades SILENTLY (a one-word verb
    becomes its own noun-group with no verb subcommand: `quizlist --json`
    quietly does something not intended, instead of `generate` refusing a
    catalog entry that doesn't match spec §5's noun-verb convention). Fail
    loud here instead, where the mistake is one line away from its source.
    """
    bad = [
        cap.get("verb") for cap in catalog.get("capabilities", [])
        if not (isinstance(cap.get("verb"), str) and len(cap["verb"].split(" ", 1)) == 2
                and all(cap["verb"].split(" ", 1)))
    ]
    if bad:
        raise click.ClickException(
            "capabilities must use two-word \"noun verb\" naming (spec §5); "
            f"found malformed verb(s): {bad!r}"
        )


def write_driver(catalog: Dict[str, Any], out_dir: Path, *, source_engine_dir: Path) -> Path:
    """Render `out_dir/` from `catalog`. Returns `out_dir`."""
    slug = catalog["site"]["slug"]
    _validate_verb_shapes(catalog)
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog = _copy_evidence(catalog, out_dir)
    (out_dir / "site.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    (out_dir / "SITEGUIDE.md").write_text(render_siteguide(catalog), encoding="utf-8")

    engine_dir = out_dir / "_engine"
    engine_dir.mkdir(exist_ok=True)
    (engine_dir / "__init__.py").write_text("", encoding="utf-8")
    for name in _RUNTIME_MODULES:
        shutil.copyfile(source_engine_dir / name, engine_dir / name)

    shim = out_dir / slug
    shim.write_text(_SHIM_TEMPLATE.format(slug=slug), encoding="utf-8", newline="\n")
    try:
        mode = shim.stat().st_mode
        shim.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:  # noqa: BLE001 — best-effort; Windows has no exec bit
        pass

    (out_dir / f"{slug}.cmd").write_text(
        _CMD_TEMPLATE.format(slug=slug), encoding="utf-8", newline=""
    )

    return out_dir
