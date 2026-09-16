"""Schema-freeze tests — the seam guards (Phase H), matching web-qa's and
web-replicate's own `test_evidence_bundle_schema_is_frozen`.

Each of these locks a JSON contract this engine's phases pass to each other
(or that `generate`/`runtime` read) against an accidental add/rename/remove.
If you change a shape on purpose, update the golden set here AND the matching
spec section together -- that pairing is the point of a freeze test: a silent
drift fails CI, a deliberate one is a one-line diff next to the reason.
"""

from __future__ import annotations

from engine.catalog import (
    Landmark,
    PageSurface,
    Reconciliation,
    ReconciliationKind,
    RouteNode,
    SiteMap,
)


def test_sitemap_schema_is_frozen():
    """Phase A's seam: `map`'s output, consumed by `read`/`probe` (an agent
    picks a route from here) and by nothing else in this engine directly."""
    site = SiteMap(entry_url="https://e.com", origin="https://e.com")
    assert set(site.to_dict().keys()) == {
        "entry_url",
        "origin",
        "with_session",
        "asset_blocking",
        "route_count",
        "capped",
        "rate_limited",
        "timed_out",
        "throttled_routes",
        "stopped_reason",
        "rate_limit",
        "frontier",
        "navigation_hint",
        "buttons_seen",
        "link_discoveries",
        "templates",
        "collapsed_routes",
        "_template_seen",
        "_collapsed",
        "_variants",
        "_variants_collapsed",
        "_params",
        "routes",
        "skipped",
    }


def test_route_node_schema_is_frozen():
    node = RouteNode(path="/a", url="https://e.com/a", final_url="https://e.com/a", status=200)
    assert set(node.to_dict().keys()) == {
        "path",
        "url",
        "final_url",
        "status",
        "title",
        "depth",
        "auth",
        "redirected",
        "reached_by",
        "error",
        "throttled",
        "controls",
        "forms",
    }


def test_page_surface_schema_is_frozen():
    """Phase B's seam: `read`'s output -- the "claims" half of spec §3,
    consumed by `probe` and by the agent authoring a capability catalog."""
    surface = PageSurface(url="https://e.com", title="Home")
    assert set(surface.to_dict().keys()) == {
        "url",
        "title",
        "headings",
        "landmarks",
        "controls",
        "forms",
        "errors",
        "empty_states",
        "copy",
    }


def test_landmark_and_control_and_form_field_schemas_are_frozen():
    assert set(Landmark(role="main").to_dict().keys()) == {"role", "name", "selector"}


def test_reconciliation_schema_is_frozen():
    """Phase C's seam: `probe`'s output -- spec §3's `reconciliation[]` table
    verbatim (kind/subject/claimed/observed/evidence), consumed by the agent
    deciding what a finding changes in the catalog (Phase D)."""
    r = Reconciliation(
        kind=ReconciliationKind.ADVERTISED_ABSENT,
        subject="Old Feature",
        claimed="does something",
        observed="404s",
    )
    d = r.to_dict()
    assert set(d.keys()) == {"kind", "subject", "claimed", "observed", "evidence"}
    assert d["kind"] == "advertised_absent"  # enum -> value, not the Enum object


# -- site.json (spec §5) -- the primary Phase H seam -------------------------

# `generate`/`runtime` read this shape by key (raw dict, not a dataclass --
# site.json is agent-authored per SKILL.md §3, not engine-emitted, so there is
# no single to_dict() to freeze; this golden IS the frozen contract instead).
_GOLDEN_SITE_JSON = {
    "schema_version": "1.0",
    "site": {
        "slug": "example",
        "base_url": "https://example.com",
        "generated_at": "2026-09-16",
        "fingerprint": {"title": "Example", "build_id": "abc", "nav_hash": "def"},
    },
    "auth": {
        "required": True,
        "recipe": "auth.login",
        "credentials": [{"param": "email", "env": "EXAMPLE_EMAIL"}],
        "session_file": ".session.json",
        "user_agent": "…",
    },
    "routes": [
        {
            "path": "/items",
            "title": "Items",
            "auth": "required",
            "reached_by": ["nav:Items"],
            "verified": True,
        }
    ],
    "capabilities": [
        {
            "verb": "item list",
            "summary": "List items.",
            "kind": "read",
            "destructive": False,
            "preconditions": ["auth"],
            "params": [],
            "steps": [],
            "assert": {"url_contains": "/items"},
            "extract": {"container": {"role": "listitem"}, "fields": {}},
            "verified_at": "2026-09-16T00:00:00Z",
            "evidence": "runs/item-list.json",
        },
        {
            "verb": "item delete",
            "kind": "mutating",
            "destructive": True,
            "confirm": True,
            "params": [{"name": "id", "type": "string", "required": True}],
        },
    ],
    "unverified": [{"verb": "item archive", "reason": "route 404s"}],
    "reconciliation": [
        {
            "kind": "optional_but_required",
            "verb": "item create",
            "param": "category",
            "evidence": "runs/item-create.json",
        }
    ],
}


def test_site_json_top_level_schema_is_frozen():
    """spec §5's site.json seam, top level. `generate` reads exactly this
    key set (site.json → site.json copy) and `runtime` reads `site`/
    `capabilities` directly off it."""
    assert set(_GOLDEN_SITE_JSON.keys()) == {
        "schema_version",
        "site",
        "auth",
        "routes",
        "capabilities",
        "unverified",
        "reconciliation",
    }
    assert set(_GOLDEN_SITE_JSON["site"].keys()) == {
        "slug",
        "base_url",
        "generated_at",
        "fingerprint",
    }


def test_site_json_capability_schema_is_frozen():
    """The fields `runtime.py` actually consumes (verb/kind/destructive/
    preconditions/params/steps/assert/extract) plus the generation-time
    provenance fields (summary/verified_at/evidence/confirm) spec §5 carries."""
    cap = _GOLDEN_SITE_JSON["capabilities"][0]
    assert set(cap.keys()) == {
        "verb",
        "summary",
        "kind",
        "destructive",
        "preconditions",
        "params",
        "steps",
        "assert",
        "extract",
        "verified_at",
        "evidence",
    }


def test_generate_accepts_the_golden_catalog_end_to_end(tmp_path):
    """The golden isn't just a shape check -- `generate` must actually accept
    it and produce a driver, so this test fails if `generate`/`runtime`'s
    field access ever falls out of sync with the frozen contract above."""
    import json

    from click.testing import CliRunner

    from engine.cli import cli

    catalog_path = tmp_path / "site.json"
    catalog_path.write_text(json.dumps(_GOLDEN_SITE_JSON), encoding="utf-8")
    out_dir = tmp_path / "drivers" / "example"

    res = CliRunner().invoke(
        cli, ["generate", "--catalog", str(catalog_path), "--out", str(out_dir)]
    )
    assert res.exit_code == 0, res.output
    result = json.loads(res.output)
    assert result["capabilities"] == 2
    assert result["unverified"] == 1
    assert (out_dir / "site.json").exists()
