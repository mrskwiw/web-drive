"""``generate`` + the runtime it emits (Phase G).

Covers both halves of spec D2 ("thin shim + declarative catalog, not emitted
Python"): `generate` writes the right files, and `runtime.build_cli` -- the
part that is tested ONCE and copied byte-identical into every driver --
dispatches correctly: `capabilities --json`, a read verb's extract, a
mutating verb, a destructive verb's --yes gate, exit codes, --dry-run, and
`doctor`. The final test drives a GENERATED shim as a real subprocess with no
PYTHONPATH pointing at this engine, to prove `drivers/<slug>/` is genuinely
self-sufficient (spec §12's "cold shell" success criterion).
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from engine.cli import cli
from engine.runtime import build_cli

PAGE = b"""<!doctype html><title>Quiz Squirrel Fixture</title>
<body>
<ul id="list">
  <li data-id="1"><h3>Trees</h3></li>
  <li data-id="2"><h3>Capitals</h3></li>
</ul>
<input id="title-field" type="text">
<button id="create-btn" onclick="
  var li = document.createElement('li');
  li.setAttribute('data-id', '3');
  var titleEl = document.createElement('h3');
  titleEl.textContent = document.getElementById('title-field').value || 'New Quiz';
  li.appendChild(titleEl);
  document.getElementById('list').appendChild(li);
  var a = document.createElement('div');
  a.setAttribute('role', 'alert');
  a.textContent = 'Created: ' + titleEl.textContent + '!';
  document.body.appendChild(a);
">Create</button>
<button id="delete-btn" onclick="
  document.getElementById('list').lastElementChild.remove();
  var a = document.createElement('div');
  a.setAttribute('role', 'alert');
  a.textContent = 'Deleted!';
  document.body.appendChild(a);
">Delete</button>
</body>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — stdlib callback name
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Silence benign keep-alive disconnects."""


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


def _catalog(base_url: str) -> dict:
    return {
        "schema_version": "1.0",
        "site": {
            "slug": "fixture",
            "base_url": base_url,
            "generated_at": "2026-09-16",
            "fingerprint": {"title": "Quiz Squirrel Fixture"},
        },
        "auth": {"required": False},
        "capabilities": [
            {
                "verb": "quiz list",
                "summary": "List quizzes.",
                "kind": "read",
                "destructive": False,
                "preconditions": [],
                "params": [],
                "steps": [],
                "extract": {
                    "container": {"role": "listitem"},
                    "fields": {
                        "id": {"attr": "data-id"},
                        "title": {"role": "heading"},
                    },
                },
            },
            {
                "verb": "quiz create",
                "summary": "Create a quiz.",
                "kind": "mutating",
                "destructive": False,
                "preconditions": [],
                "params": [{"name": "title", "type": "string", "required": True}],
                "steps": [
                    {"type": "fill", "selector": "#title-field", "value": "${title}"},
                    {"type": "click", "selector": "#create-btn"},
                ],
                "assert": {"content_contains": "Created: Trees Quiz!"},
            },
            {
                "verb": "quiz delete",
                "summary": "Delete a quiz.",
                "kind": "mutating",
                "destructive": True,
                "preconditions": [],
                "params": [],
                "steps": [{"type": "click", "selector": "#delete-btn"}],
                "assert": {"content_contains": "Deleted!"},
            },
            {
                "verb": "quiz broken",
                "kind": "mutating",
                "destructive": False,
                "preconditions": [],
                "params": [],
                "steps": [{"type": "click", "selector": "#create-btn"}],
                "assert": {"content_contains": "this text never appears"},
            },
        ],
        "unverified": [{"verb": "quiz archive", "reason": "route 404s"}],
        "reconciliation": [],
    }


def _skip_if_no_chromium(res_or_exc):
    msg = str(res_or_exc)
    if "Executable doesn't exist" in msg or "playwright install" in msg:
        pytest.skip("Chromium not installed for Playwright")


# -- generate: file layout ----------------------------------------------------


def test_generate_writes_a_self_contained_driver(tmp_path):
    catalog_path = tmp_path / "site.json"
    catalog_path.write_text(json.dumps(_catalog("http://example.invalid")), encoding="utf-8")
    out_dir = tmp_path / "drivers" / "fixture"

    res = CliRunner().invoke(
        cli, ["generate", "--catalog", str(catalog_path), "--out", str(out_dir)]
    )
    assert res.exit_code == 0, res.output
    result = json.loads(res.output)
    assert result["capabilities"] == 4
    assert result["unverified"] == 1

    assert (out_dir / "site.json").exists()
    assert (out_dir / "SITEGUIDE.md").exists()
    assert (out_dir / "fixture").exists()  # POSIX shim
    assert (out_dir / "fixture.cmd").exists()  # Windows wrapper
    assert (out_dir / "_engine" / "__init__.py").exists()
    assert (out_dir / "_engine" / "runtime.py").exists()
    assert (out_dir / "_engine" / "browser.py").exists()


def test_generate_refuses_a_malformed_one_word_verb(tmp_path):
    """WD-E2 (plan v2.0): `runtime.build_cli` does
    `noun, _, _ = cap["verb"].partition(" ")`, which silently degrades a
    one-word verb into its own noun-group with no verb subcommand instead of
    erroring -- so a hand-authored catalog typo (`"verb": "quizlist"`) would
    previously ship a working-looking but wrong command tree. `generate` must
    refuse it instead."""
    catalog = _catalog("http://example.invalid")
    catalog["capabilities"][0]["verb"] = "quizlist"

    catalog_path = tmp_path / "site.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    out_dir = tmp_path / "drivers" / "fixture"

    res = CliRunner().invoke(
        cli, ["generate", "--catalog", str(catalog_path), "--out", str(out_dir)]
    )
    assert res.exit_code != 0
    assert "noun-verb" in res.output.lower() or "quizlist" in res.output
    assert not out_dir.exists(), "a rejected catalog must not write a partial driver"


def test_generate_copies_and_redacts_referenced_evidence(tmp_path):
    """spec §5's evidence trail: a capability naming a real `evidence` file
    gets that file copied into the driver's own `runs/`, with any typed-in
    step value redacted (an `{"env": "VAR"}` secret resolves to the literal
    value before `verify` ever records it -- nothing in `flow.py`/`evidence.py`
    redacts that before serialization, so a raw copy would leak it)."""
    catalog = _catalog("http://example.invalid")
    evidence_src = tmp_path / "login-result.json"
    evidence_src.write_text(
        json.dumps(
            {
                "verb": "auth login",
                "verified": True,
                "steps": [
                    {
                        "label": "password",
                        "bundle": {
                            "action": {
                                "type": "fill",
                                "selector": "#password",
                                "value": "Sup3rSecret!",
                            },
                            "gate": {"passed": True},
                        },
                        "passed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog["capabilities"][0]["evidence"] = str(evidence_src)

    catalog_path = tmp_path / "site.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    out_dir = tmp_path / "drivers" / "fixture"

    res = CliRunner().invoke(
        cli, ["generate", "--catalog", str(catalog_path), "--out", str(out_dir)]
    )
    assert res.exit_code == 0, res.output

    copied = out_dir / "runs" / "quiz-list.json"
    assert copied.exists(), "evidence file was not copied into the driver's runs/"
    copied_body = json.loads(copied.read_text(encoding="utf-8"))
    assert copied_body["steps"][0]["bundle"]["action"]["value"] != "Sup3rSecret!", (
        "the resolved secret value must never survive into a distributable driver"
    )
    assert copied_body["steps"][0]["bundle"]["action"]["selector"] == "#password", (
        "redaction must strip the VALUE, not the whole action -- the selector "
        "and gate result are what prove the capability actually ran"
    )
    assert copied_body["steps"][0]["bundle"]["gate"]["passed"] is True

    written_catalog = json.loads((out_dir / "site.json").read_text(encoding="utf-8"))
    assert written_catalog["capabilities"][0]["evidence"] == "runs/quiz-list.json", (
        "the OUTPUT site.json must repoint evidence at the copy, not the "
        "original (possibly outside the driver, possibly gone tomorrow) path"
    )


def test_generate_reports_missing_evidence_without_failing(tmp_path):
    """A capability that CLAIMS evidence at a path that doesn't exist must be
    disclosed, not silently ignored or treated as a generation failure --
    same discipline as `capped`/`rate_limited`: say when something is not
    what it claims, don't just drop it."""
    catalog = _catalog("http://example.invalid")
    catalog["capabilities"][0]["evidence"] = str(tmp_path / "does-not-exist.json")

    catalog_path = tmp_path / "site.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    out_dir = tmp_path / "drivers" / "fixture"

    res = CliRunner().invoke(
        cli, ["generate", "--catalog", str(catalog_path), "--out", str(out_dir)]
    )
    assert res.exit_code == 0, res.output
    assert "not found" in res.output, "a missing evidence file must be reported, not swallowed"
    assert not (out_dir / "runs").exists(), "nothing should be copied for a miss"

    guide = (out_dir / "SITEGUIDE.md").read_text(encoding="utf-8")
    assert "quiz list" in guide
    assert "quiz delete" in guide
    assert "destructive, needs --yes" in guide
    assert "quiz archive" in guide  # withheld, still documented


# -- runtime: dispatch, in-process -------------------------------------------


def test_runtime_capabilities_lists_the_catalog():
    site = _catalog("http://example.invalid")
    app = build_cli(site)
    res = CliRunner().invoke(app, ["capabilities"])
    assert res.exit_code == 0, res.output
    caps = json.loads(res.output)
    assert {c["verb"] for c in caps} == {"quiz list", "quiz create", "quiz delete", "quiz broken"}


def test_runtime_read_verb_extracts_records():
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(app, ["quiz", "list", "--json"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["verified"] is True
    assert payload["records"] == [
        {"id": "1", "title": "Trees"},
        {"id": "2", "title": "Capitals"},
    ]


def test_runtime_mutating_verb_runs_and_verifies():
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(app, ["quiz", "create", "--title", "Trees Quiz", "--json"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["verified"] is True


def test_runtime_param_substitutes_into_a_step_via_dollar_brace():
    """The claim in SKILL.md §6: a capability's `${title}` step value is
    filled from the matching --title CLI flag at runtime (flow.resolve_str,
    reused verbatim) -- not just accepted and ignored."""
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(
            app, ["quiz", "create", "--title", "A Totally Different Title", "--json"]
        )

    if res.exception:
        _skip_if_no_chromium(res.exception)
    # The catalog's own assert only accepts "Trees Quiz" -- a DIFFERENT title
    # must fail verification, which is only possible if the param actually
    # reached the fill step rather than being dropped.
    assert res.exit_code == 1, res.output
    assert json.loads(res.output)["verified"] is False


def test_runtime_destructive_verb_refuses_without_yes():
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(app, ["quiz", "delete"])

    if res.exception and "playwright" in str(res.exception).lower():
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 4, res.output


def test_runtime_destructive_verb_runs_with_yes():
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(app, ["quiz", "delete", "--yes", "--json"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["verified"] is True


def test_runtime_failed_assertion_exits_1():
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(app, ["quiz", "broken", "--json"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 1, res.output
    assert json.loads(res.output)["verified"] is False


def test_runtime_real_drift_exits_3():
    """WD-T2 (plan v2.0): exit code 3 (drift) must fire from a REAL locator
    timeout driven through the generated driver's own runtime dispatch, not
    just asserted against `verify.py` directly. A step targeting a selector
    that will never exist raises Playwright's own TimeoutError, which
    `_exit_code_for` now reads via `VerifyResult.drift` (a real signal) rather
    than string-matching the error text -- see verify.py/runtime.py."""
    site = {
        "schema_version": "1.0",
        "site": {"slug": "drift", "base_url": None, "generated_at": "2026-09-17",
                  "fingerprint": {}},
        "auth": {"required": False},
        "capabilities": [
            {
                "verb": "thing drift",
                "kind": "mutating",
                "destructive": False,
                "preconditions": [],
                "params": [],
                "steps": [{"type": "click", "selector": "#totally-does-not-exist-anywhere"}],
                "assert": {"content_contains": "unreachable"},
            }
        ],
        "unverified": [],
        "reconciliation": [],
    }
    with _server() as base:
        site["site"]["base_url"] = base
        app = build_cli(site)
        res = CliRunner().invoke(app, ["thing", "drift", "--json"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 3, res.output
    payload = json.loads(res.output)
    assert payload["verified"] is False


def test_runtime_dry_run_prints_steps_without_executing():
    # An unreachable port: a dry run must not try to navigate there at all,
    # or this test would hang/error instead of returning instantly.
    site = _catalog("http://127.0.0.1:1")
    app = build_cli(site)
    res = CliRunner().invoke(app, ["quiz", "create", "--title", "Trees Quiz", "--dry-run"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["dry_run"] is True
    # Un-resolved -- dry-run shows the catalog's own template, not a filled-in
    # preview, so `${title}` is still literal here.
    assert payload["steps"] == [
        {"type": "fill", "selector": "#title-field", "value": "${title}"},
        {"type": "click", "selector": "#create-btn"},
    ]


def test_runtime_doctor_reports_no_drift_when_title_matches():
    with _server() as base:
        site = _catalog(base)
        app = build_cli(site)
        res = CliRunner().invoke(app, ["doctor"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["drift"] is False


def test_runtime_doctor_reports_drift_when_title_changed():
    with _server() as base:
        site = _catalog(base)
        site["site"]["fingerprint"]["title"] = "A Completely Different Site"
        app = build_cli(site)
        res = CliRunner().invoke(app, ["doctor"])

    if res.exception:
        _skip_if_no_chromium(res.exception)
    assert res.exit_code == 3, res.output
    assert json.loads(res.output)["drift"] is True


# -- cold-shell subprocess: the actual generated shim, standalone -----------


def test_generated_driver_runs_standalone_as_a_subprocess(tmp_path):
    """The real spec §12 gate: given ONLY drivers/<slug>/, no PYTHONPATH into
    this skill's engine, a cold shell can run a capability and get correct
    JSON back."""
    with _server() as base:
        catalog_path = tmp_path / "site.json"
        catalog_path.write_text(json.dumps(_catalog(base)), encoding="utf-8")
        out_dir = tmp_path / "drivers" / "fixture"
        gen = CliRunner().invoke(
            cli, ["generate", "--catalog", str(catalog_path), "--out", str(out_dir)]
        )
        assert gen.exit_code == 0, gen.output

        shim = out_dir / "fixture"
        proc = subprocess.run(
            [sys.executable, str(shim), "quiz", "list", "--json"],
            cwd=str(tmp_path),  # NOT the skill dir -- proves no reliance on cwd
            capture_output=True,
            text=True,
            timeout=60,
        )

    if "Executable doesn't exist" in proc.stderr or "playwright install" in proc.stderr:
        pytest.skip("Chromium not installed for Playwright")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["verified"] is True
    assert payload["records"] == [
        {"id": "1", "title": "Trees"},
        {"id": "2", "title": "Capitals"},
    ]
