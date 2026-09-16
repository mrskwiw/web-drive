"""``probe`` — the NAVIGATE half of spec §3: check a claim from `read`/`map`
against what actually happens, and emit typed `Reconciliation` findings
(spec §3's table). Phase C.

Deterministic only, per the engine/agent split this whole family is built on:
this module DETECTS a disagreement (a route existed, a field failed anyway, a
click produced nothing, a page behaves differently reached cold). It never
decides whether the disagreement MATTERS to a user, or what verb/param it
should change in the catalog -- that judgment stays the agent's, in SKILL.md
(Phase D+). Conservative by construction throughout: a missed reconciliation
just means one more candidate the agent reviews unaided; a FALSE one could
rename or withhold a verb that was actually fine, which is the expensive
direction to be wrong in.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .browser import BrowserController
from .catalog import Reconciliation, ReconciliationKind
from .models import Action, ActionType, BrowserEngine
from .read import read_surface
from .sitemap import _status_for, is_probe_safe, normalize

_WORD_RE = re.compile(r"[a-z0-9]+")

# Fill values for the requiredness probe -- plausible enough to pass a naive
# `type="email"` browser check without tripping a server-side format validator
# a probe has no business exercising (that's a DIFFERENT reconciliation kind).
_SAFE_VALUES: Dict[str, str] = {
    "email": "probe@example.com",
    "tel": "555-0100",
    "url": "https://example.com",
    "number": "1",
    "password": "ProbePass!1",
}
_TEXT_LIKE_TYPES = {
    "text",
    "email",
    "search",
    "tel",
    "url",
    "number",
    "textarea",
    "password",
}


def _significant_words(text: str, min_len: int = 4) -> List[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) >= min_len]


def _safe_value(field: Dict[str, Any]) -> str:
    return _SAFE_VALUES.get(field.get("type", "text"), "Probe value")


async def probe_control(
    controller: BrowserController,
    origin_url: str,
    control: Dict[str, Any],
) -> Optional[Reconciliation]:
    """Click one non-form control and check the claim it makes.

    Two reconciliation kinds come out of a single click: ADVERTISED_ABSENT when
    the click fails outright or lands on a document the server itself flagged
    as an error, and LABEL_ROUTE_MISMATCH when it navigates somewhere whose
    path shares none of the label's significant (>=4 char) words. Deliberately
    loose word-overlap rather than semantic matching -- false negatives here
    just mean one more candidate the agent reviews unaided.
    """
    label = control.get("name") or control["selector"]
    selector = control["selector"]
    await controller.navigate(origin_url)
    before_url = controller.page.url
    try:
        await controller.page.click(selector, timeout=5000)
        await controller.page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception as exc:  # noqa: BLE001 — the failure itself IS the finding
        return Reconciliation(
            kind=ReconciliationKind.ADVERTISED_ABSENT,
            subject=label,
            claimed=f"{label!r} does something when activated",
            observed=f"click failed: {exc}",
            evidence=selector,
        )
    after_url = controller.page.url
    status = _status_for(controller, after_url)
    if status and status >= 400:
        return Reconciliation(
            kind=ReconciliationKind.ADVERTISED_ABSENT,
            subject=label,
            claimed=f"{label!r} leads somewhere real",
            observed=f"landed on {after_url} with status {status}",
            evidence=after_url,
        )
    if normalize(after_url) == normalize(before_url):
        return None  # no navigation -- nothing to check a route claim against
    words = _significant_words(label)
    path = urlparse(after_url).path.lower()
    if words and not any(w in path for w in words):
        return Reconciliation(
            kind=ReconciliationKind.LABEL_ROUTE_MISMATCH,
            subject=label,
            claimed=f"label {label!r}",
            observed=f"lands on {path}",
            evidence=after_url,
        )
    return None


async def probe_form_requiredness(
    controller: BrowserController,
    origin_url: str,
    form: Dict[str, Any],
) -> List[Reconciliation]:
    """Submit the form once per NON-required field, leaving that ONE field
    blank while every other field gets a safe value, and see if submission is
    rejected anyway.

    Skips forms the markup already flags destructive (never submitted -- spec
    §7: mutating verbs verify only with explicit confirmation) and fields
    outside the text-like types (a blank checkbox/select is a different claim
    than a blank text field). A NEW error/alert appearing after a blank submit
    that was not present before it is the signal: the field the markup called
    optional, the server did not.
    """
    if form.get("destructive"):
        return []
    submit_sel = form.get("submit_selector")
    if not submit_sel:
        return []
    candidates = [
        f
        for f in form["fields"]
        if not f.get("required") and f.get("type") in _TEXT_LIKE_TYPES
    ]
    findings: List[Reconciliation] = []
    for target in candidates:
        await controller.navigate(origin_url)
        before = await read_surface(controller)
        before_errors = set(before.errors)
        for f in form["fields"]:
            if f is target or f.get("type") not in _TEXT_LIKE_TYPES:
                continue
            try:
                await controller.perform(
                    Action(
                        type=ActionType.FILL,
                        selector=f["selector"],
                        value=_safe_value(f),
                    )
                )
            except Exception:  # noqa: BLE001 — best-effort; not this field's turn
                pass
        try:
            await controller.page.click(submit_sel, timeout=5000)
            await controller.page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 — an unclickable submit isn't this finding
            continue
        after = await read_surface(controller)
        new_errors = [e for e in after.errors if e not in before_errors]
        if new_errors:
            name = target.get("name") or target["selector"]
            findings.append(
                Reconciliation(
                    kind=ReconciliationKind.OPTIONAL_BUT_REQUIRED,
                    subject=name,
                    claimed=f"{name} is optional",
                    observed=f"submit rejected with: {new_errors[0]}",
                    evidence=submit_sel,
                )
            )
    return findings


async def probe_precondition(
    controller: BrowserController,
    origin_url: str,
    control: Dict[str, Any],
    *,
    base_state: Any,
    browser_engine: BrowserEngine,
    user_agent: Optional[str],
) -> Optional[Reconciliation]:
    """Click a control, then check whether reaching its target COLD -- a fresh
    context seeded with the SAME auth but none of the client-side state the
    click-through accumulated -- reproduces the same outcome.

    A route that renders one way reached through the recorded UI path and
    another way hit directly (identical auth, identical URL) depends on
    something besides authentication: an onboarding flag, a wizard step,
    in-memory state a cold page load cannot reconstruct. That gap IS the
    precondition; naming it is Phase D's job. Costs a second browser launch
    per candidate, so the CLI makes this opt-in.

    ``base_state`` MUST be captured before ANY control on this page has been
    probed -- capturing it here, mid-loop, would already carry whatever
    localStorage/cookie changes an earlier click (this one's own `probe_control`
    pass included) had made, silently seeding the "fresh" context with the very
    state this check exists to withhold from it.
    """
    label = control.get("name") or control["selector"]
    selector = control["selector"]
    await controller.navigate(origin_url)
    before_url = controller.page.url
    try:
        await controller.page.click(selector, timeout=5000)
        await controller.page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:  # noqa: BLE001 — probe_control already reports this case
        return None
    after_url = controller.page.url
    if normalize(after_url) == normalize(before_url):
        return None
    via_status = _status_for(controller, after_url)

    fresh = BrowserController(
        engine=browser_engine,
        headless=True,
        storage_state=base_state,
        user_agent=user_agent,
    )
    await fresh.launch()
    try:
        await fresh.navigate(after_url)
        cold_url = fresh.page.url
        cold_status = _status_for(fresh, cold_url)
    finally:
        await fresh.close()

    if normalize(cold_url) != normalize(after_url) or (
        via_status and cold_status and via_status != cold_status
    ):
        return Reconciliation(
            kind=ReconciliationKind.UNDOCUMENTED_PRECONDITION,
            subject=label,
            claimed=f"{after_url} is reachable directly",
            observed=(
                f"direct navigation lands on {cold_url} (status {cold_status}) "
                f"vs {after_url} (status {via_status}) via the recorded click path"
            ),
            evidence=selector,
        )
    return None


__all__ = [
    "is_probe_safe",
    "probe_control",
    "probe_form_requiredness",
    "probe_precondition",
]
