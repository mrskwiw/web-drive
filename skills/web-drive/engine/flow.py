"""Stateful multi-step flows (Phase H, spec §4.2).

A *flow* runs an ordered list of steps in ONE persistent browser context, so
state (auth cookies/tokens, wizard progress) carries across steps — the thing
stateless ``act`` cannot do. Each step yields one evidence bundle + gate (same
§5/§6 contract as ``act``), plus an optional per-step **assertion** that the
expected state change actually happened.

This module holds the *pure* pieces (no browser, fully unit-testable):

- :func:`resolve_str` / :func:`build_action` — turn a step dict into an
  :class:`~engine.models.Action`, resolving secrets by env-var reference so
  credentials are never inlined in ``steps.json``.
- :func:`evaluate_assertion` — check an :class:`~engine.models.EvidenceBundle`
  against a step's ``assert`` spec.

The async orchestration (drive the steps, halt on failure) lives in ``cli.py``
alongside ``act``. A step dict is an ``act``-style action plus optional keys:
``label``, ``assert`` (see :func:`evaluate_assertion`), ``await_response``
(``{method?, path_contains, timeout_ms?}`` — wait for that response to land
before capturing, for slow async actions), and ``settle_ms`` (extra idle wait).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .models import Action, ActionType, EvidenceBundle, GateResult, Serializable

_ENV_REF = re.compile(r"\$\{(\w+)\}")


class MissingSecretError(KeyError):
    """A ``${VAR}`` / ``{"env": "VAR"}`` reference had no matching env var."""


def resolve_str(value: Any, env: Mapping[str, str]) -> Any:
    """Resolve a step field, substituting secrets from ``env``.

    - ``{"env": "NAME"}`` → ``env["NAME"]`` (whole-value form).
    - a string containing ``${NAME}`` → each ref replaced by ``env["NAME"]``.
    - anything else (incl. ``None``) → returned unchanged.

    Raises :class:`MissingSecretError` if a referenced var is absent, so a flow
    fails fast instead of sending an empty/`${VAR}`-literal credential.
    """
    if isinstance(value, dict) and "env" in value:
        name = value["env"]
        if name not in env:
            raise MissingSecretError(name)
        return env[name]
    if isinstance(value, str):

        def _sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name not in env:
                raise MissingSecretError(name)
            return env[name]

        return _ENV_REF.sub(_sub, value)
    return value


def build_action(step: Dict[str, Any], env: Mapping[str, str]) -> Action:
    """Construct an :class:`Action` from a step dict, resolving secrets."""
    return Action(
        type=ActionType(step["type"]),
        selector=resolve_str(step.get("selector"), env),
        url=resolve_str(step.get("url"), env),
        text=resolve_str(step.get("text"), env),
        key=step.get("key"),
        value=resolve_str(step.get("value"), env),
        inferred_intent=step.get("inferred_intent") or step.get("label"),
    )


# ---------------------------------------------------------------------------
# Assertions (pure)
# ---------------------------------------------------------------------------


@dataclass
class AssertionCheck(Serializable):
    name: str
    passed: bool
    detail: Optional[str] = None


@dataclass
class AssertionResult(Serializable):
    passed: bool
    checks: List[AssertionCheck] = field(default_factory=list)


def _match_call(call, spec: Dict[str, Any]) -> bool:
    """Whether one network call matches an ``http_any`` matcher."""
    if "method" in spec and call.method.upper() != str(spec["method"]).upper():
        return False
    if "path_contains" in spec and spec["path_contains"] not in call.url:
        return False
    if "status" in spec and call.status != int(spec["status"]):
        return False
    if "status_lt" in spec and not (call.status < int(spec["status_lt"])):
        return False
    if "status_min" in spec and not (call.status >= int(spec["status_min"])):
        return False
    if "status_max" in spec and not (call.status <= int(spec["status_max"])):
        return False
    return True


def evaluate_assertion(
    bundle: EvidenceBundle, spec: Optional[Dict[str, Any]]
) -> AssertionResult:
    """Check a bundle against a step's ``assert`` spec (all keys must hold).

    Supported keys (all optional):

    - ``url_changed`` (bool) — whether ``url_after`` differs from ``url_before``.
    - ``url_contains`` (str) — substring expected in ``url_after``.
    - ``dom_contains`` / ``dom_absent`` (str) — substring present/absent in the
      post-action DOM outline (case-insensitive).
    - ``content_contains`` / ``content_absent`` (str) — substring present/absent
      in the readable rendered *content* (case-insensitive). Use for a coarse
      outcome check (result panel shows the entity / does NOT say "No results" /
      "Error"). NOTE: this is a deterministic substring net only — judging whether
      produced content is *relevant and good* is the agent's semantic call
      (SKILL.md "Outcome verification"), not something an assertion can decide.
    - ``http_any`` (obj) — at least one step network call matches
      ``{method?, path_contains?, status?, status_lt?, status_min?, status_max?}``.
      The canonical "the write actually fired" check.
    - ``no_console_errors`` (bool) — no error-level console output this step.
    - ``no_http_errors`` (bool) — no step network call returned >= 400.

    An empty/absent spec asserts nothing and passes.
    """
    checks: List[AssertionCheck] = []
    if not spec:
        return AssertionResult(True, checks)

    if "url_changed" in spec:
        want = bool(spec["url_changed"])
        got = bundle.url_after != bundle.url_before
        checks.append(
            AssertionCheck(
                "url_changed",
                got == want,
                f"want changed={want}; before={bundle.url_before!r} after={bundle.url_after!r}",
            )
        )
    if "url_contains" in spec:
        sub = str(spec["url_contains"])
        checks.append(
            AssertionCheck(
                "url_contains",
                sub in bundle.url_after,
                f"{sub!r} in {bundle.url_after!r}",
            )
        )
    if "dom_contains" in spec:
        sub = str(spec["dom_contains"])
        checks.append(
            AssertionCheck(
                "dom_contains",
                sub.lower() in bundle.dom_outline_after.lower(),
                f"expected {sub!r} in DOM outline",
            )
        )
    if "dom_absent" in spec:
        sub = str(spec["dom_absent"])
        checks.append(
            AssertionCheck(
                "dom_absent",
                sub.lower() not in bundle.dom_outline_after.lower(),
                f"expected {sub!r} absent from DOM outline",
            )
        )
    if "content_contains" in spec:
        sub = str(spec["content_contains"])
        checks.append(
            AssertionCheck(
                "content_contains",
                sub.lower() in bundle.content_after.lower(),
                f"expected {sub!r} in rendered content",
            )
        )
    if "content_absent" in spec:
        sub = str(spec["content_absent"])
        checks.append(
            AssertionCheck(
                "content_absent",
                sub.lower() not in bundle.content_after.lower(),
                f"expected {sub!r} absent from rendered content",
            )
        )
    if "http_any" in spec:
        matcher = spec["http_any"]
        ok = any(_match_call(c, matcher) for c in bundle.http)
        seen = [f"{c.method} {c.url} {c.status}" for c in bundle.http]
        checks.append(
            AssertionCheck(
                "http_any",
                ok,
                f"expected a call matching {matcher}; saw {seen[:8]}",
            )
        )
    if spec.get("no_console_errors"):
        errs = bundle.console.errors
        checks.append(
            AssertionCheck(
                "no_console_errors",
                not errs,
                None if not errs else f"{len(errs)} error(s); first: {errs[0][:120]}",
            )
        )
    if spec.get("no_http_errors"):
        bad = [c for c in bundle.http if c.status >= 400]
        checks.append(
            AssertionCheck(
                "no_http_errors",
                not bad,
                None if not bad else f"{bad[0].status} {bad[0].url}",
            )
        )

    return AssertionResult(all(c.passed for c in checks), checks)


def fail_reason(
    gate: Optional[GateResult],
    assertion: AssertionResult,
    perform_error: Optional[str],
) -> str:
    """One-line why a step failed, for the flow's ``halted_at`` record."""
    if perform_error:
        return f"action raised: {perform_error}"
    parts: List[str] = []
    if gate and not gate.passed:
        failed = [c.name for c in gate.checks if not c.passed]
        parts.append("gate failed: " + ", ".join(failed))
    if not assertion.passed:
        failed = [c.name for c in assertion.checks if not c.passed]
        parts.append("assertion failed: " + ", ".join(failed))
    return "; ".join(parts) or "unknown failure"


def slug(text: str) -> str:
    """Filesystem-safe short slug for per-step screenshot names."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "step")[:40]
