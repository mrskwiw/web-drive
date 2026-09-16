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
import shutil
import stat
from pathlib import Path
from typing import Any, Dict

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
        destructive = " — **destructive, needs --yes**" if cap.get("destructive") else ""
        lines.append(f"### `{verb}` ({kind}){destructive}")
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


def write_driver(catalog: Dict[str, Any], out_dir: Path, *, source_engine_dir: Path) -> Path:
    """Render `out_dir/` from `catalog`. Returns `out_dir`."""
    slug = catalog["site"]["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)

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
