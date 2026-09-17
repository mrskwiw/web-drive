"""``verify`` — Phase E: execute a candidate capability's steps in ONE
persistent context, capture the success signal, and report verified or
demoted-with-reason.

Recipes reuse web-qa's flow step schema verbatim (spec D6) — this is the flow
runner the copied `flow.py`/`evidence.py`/`gate.py` already provide (Phase A's
copy, until now unwired to any CLI command), with a verb-shaped result on top.
Verify-or-withhold is the quality bar (spec §5): a capability becomes a
runnable verb only if this actually ran it and it actually passed. Nothing
here judges whether the *outcome* was semantically good — that stays the
agent (this module only runs the deterministic gate + the author's own
assertion, same split as every other engine piece in this family).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser import BrowserController
from .evidence import EvidenceBundler
from .flow import MissingSecretError, build_action, evaluate_assertion, fail_reason
from .gate import DeterministicGate
from .models import Action, ActionType


@dataclass
class StepResult:
    label: str
    bundle: Dict[str, Any]
    gate: Dict[str, Any]
    assertion: Dict[str, Any]
    passed: bool
    error: Optional[str] = None
    # True only when `error` came from Playwright's OWN TimeoutError -- a
    # locator that genuinely never resolved (site redeployed, selector rot).
    # Deliberately NOT inferred by string-matching `error`'s text (the prior
    # approach: substrings like "timeout"/"not found" in ANY exception's
    # message, which misclassifies an unrelated error -- a network timeout,
    # a custom app exception -- as selector drift just because its wording
    # happens to overlap). The exception's TYPE is the actual signal.
    drift: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "bundle": self.bundle,
            "gate": self.gate,
            "assertion": self.assertion,
            "passed": self.passed,
            "error": self.error,
            "drift": self.drift,
        }


@dataclass
class VerifyResult:
    verb: str
    verified: bool
    reason: Optional[str] = None
    steps: List[StepResult] = field(default_factory=list)
    # Carries the halting step's own `drift` flag (see StepResult), so
    # `runtime.py` can pick exit code 3 from a real signal instead of
    # sniffing `reason`'s text for substrings.
    drift: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verb": self.verb,
            "verified": self.verified,
            "reason": self.reason,
            "steps": [s.to_dict() for s in self.steps],
            "drift": self.drift,
        }


async def verify_capability(
    controller: BrowserController,
    verb: str,
    steps: List[Dict[str, Any]],
    final_assert: Optional[Dict[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> VerifyResult:
    """Run every step; verified only if EVERY step's own gate + per-step
    assertion passed AND the capability's `final_assert` passes against the
    LAST step's evidence.

    Halts at the first failing step, same discipline as `flow`: a capability
    whose third step never ran because the second one broke is not "verified
    with caveats" — it is unverified, with a reason naming exactly where.
    """
    resolved_env: Dict[str, str] = dict(env if env is not None else os.environ)
    bundler = EvidenceBundler()
    gate_eval = DeterministicGate()
    results: List[StepResult] = []
    last_bundle = None

    if not steps:
        # A read verb that does nothing beyond the caller's initial navigation
        # (spec §5's `extract`-only capabilities) has no action to build a real
        # EvidenceBundle from -- synthesize one from the CURRENT page, with
        # before == after, so `final_assert` still has something to check
        # against. Verified trivially when no assertion was given at all.
        state = await controller.capture_state()
        bundle = bundler.build(Action(type=ActionType.NAVIGATE, url=state.url), state, state)
        final = evaluate_assertion(bundle, final_assert)
        if not final.passed:
            failed = [c.name for c in final.checks if not c.passed]
            return VerifyResult(verb, False, f"final assertion failed: {', '.join(failed)}", [])
        return VerifyResult(verb, True, None, [])

    for step in steps:
        label = step.get("label") or step.get("type", "step")
        try:
            action = build_action(step, resolved_env)
        except MissingSecretError as exc:
            reason = f"missing secret: {exc}"
            results.append(StepResult(label, {}, {}, {}, False, reason))
            return VerifyResult(verb, False, reason, results)

        before = await controller.capture_state()
        perform_error: Optional[str] = None
        step_drift = False
        try:
            await controller.perform(action)
            if step.get("settle_ms"):
                await controller.settle(int(step["settle_ms"]))
        except PlaywrightTimeoutError as exc:
            perform_error = str(exc)
            step_drift = True
        except Exception as exc:  # noqa: BLE001 — the failure IS the result
            perform_error = str(exc)
        after = await controller.capture_state()
        bundle = bundler.build(action, before, after)
        gate = gate_eval.evaluate(bundle) if perform_error is None else None
        bundle.gate = gate
        assertion = evaluate_assertion(bundle, step.get("assert"))
        last_bundle = bundle
        step_passed = (
            perform_error is None and (gate is None or gate.passed) and assertion.passed
        )
        results.append(
            StepResult(
                label,
                bundle.to_dict(),
                gate.to_dict() if gate else {},
                assertion.to_dict(),
                step_passed,
                perform_error,
                step_drift,
            )
        )
        if not step_passed:
            reason = f"halted at {label!r}: {fail_reason(gate, assertion, perform_error)}"
            return VerifyResult(verb, False, reason, results, step_drift)

    if last_bundle is None:
        return VerifyResult(verb, False, "no steps to run", results)
    final = evaluate_assertion(last_bundle, final_assert)
    if not final.passed:
        failed = [c.name for c in final.checks if not c.passed]
        reason = f"final assertion failed: {', '.join(failed)}"
        return VerifyResult(verb, False, reason, results)
    return VerifyResult(verb, True, None, results)
