"""Deterministic gate — objective, authoritative pass/fail checks (Phase B, spec §6).

The gate turns a raw evidence bundle into an objective verdict. It is **pure**:
it reads only the bundle (no browser access), so it is fully unit-testable. A
single failing check fails the gate; the agent uses ``gate.passed`` to
short-circuit semantic judgment — no tokens are spent on an objectively-broken
action.

Checks (spec §6):
- ``no_console_errors`` — no error-level console output during the action
- ``http_status_ok``    — no request the action triggered returned >= 400 (3xx redirects are fine)
- ``no_crash``          — no unhandled page exception
- ``no_error_page``     — did not land on a recognizable 404/500/error template
- ``navigation_sane``   — no dead-end URL (about:blank/empty) or redirect loop
- ``target_survived``   — the acted-on element did not vanish without navigating.
  **Advisory** (``advisory=True``): reported but does NOT count toward
  ``gate.passed`` — a vanished element is a semantic signal for the agent, not
  objective breakage, so it must not short-circuit the AI judgment.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .models import EvidenceBundle, GateCheckResult, GateResult

# URL-path fragments that signal an error page.
_ERROR_URL_MARKERS = ("/404", "/500", "/error")
# Lowercased content markers (matched against the DOM outline text).
_ERROR_TEXT_MARKERS = (
    "page not found",
    "404 not found",
    "this page could not be found",
    "internal server error",
    "500 internal",
)
_DEAD_URLS = ("", "about:blank")
_MAX_REDIRECTS = 10


class DeterministicGate:
    """Evaluate the objective checks over one :class:`EvidenceBundle`."""

    def evaluate(self, bundle: EvidenceBundle) -> GateResult:
        # Six authoritative checks — objective breakage that short-circuits AI.
        checks = [
            self._no_console_errors(bundle),
            self._http_status_ok(bundle),
            self._no_crash(bundle),
            self._no_error_page(bundle),
            self._navigation_sane(bundle),
            self._opened_pages_ok(bundle),
        ]
        # target_survived is ADVISORY: "element vanished" is a *semantic* signal
        # (a vanish is normal on many real interactions), so it is reported for
        # the agent to weigh but does NOT fail the gate or short-circuit the AI's
        # outcome-verification judgment. (Making it authoritative caused adversarial
        # reviews to oscillate on its threshold — BUGS.md 2026-07-13.)
        ts = self._target_survived(bundle)
        ts.advisory = True
        checks.append(ts)
        return GateResult(
            passed=all(c.passed for c in checks if not c.advisory), checks=checks
        )

    @staticmethod
    def _no_console_errors(b: EvidenceBundle) -> GateCheckResult:
        errors = b.console.errors
        if not errors:
            return GateCheckResult("no_console_errors", True)
        return GateCheckResult(
            "no_console_errors",
            False,
            f"{len(errors)} console error(s); first: {errors[0][:140]}",
        )

    @staticmethod
    def _http_status_ok(b: EvidenceBundle) -> GateCheckResult:
        bad = [c for c in b.http if c.status >= 400]
        if not bad:
            return GateCheckResult("http_status_ok", True)
        first = bad[0]
        return GateCheckResult(
            "http_status_ok",
            False,
            f"{len(bad)} request(s) >=400; first: {first.status} {first.url}",
        )

    @staticmethod
    def _no_crash(b: EvidenceBundle) -> GateCheckResult:
        errors = b.page_errors
        if not errors:
            return GateCheckResult("no_crash", True)
        return GateCheckResult(
            "no_crash", False, f"{len(errors)} page error(s); first: {errors[0][:140]}"
        )

    @staticmethod
    def _no_error_page(b: EvidenceBundle) -> GateCheckResult:
        path = urlparse(b.url_after).path.lower()
        marker = next((m for m in _ERROR_URL_MARKERS if m in path), None)
        if marker:
            return GateCheckResult(
                "no_error_page", False, f"URL path looks like an error page: {path!r}"
            )
        text = b.dom_outline_after.lower()
        hit = next((m for m in _ERROR_TEXT_MARKERS if m in text), None)
        if hit:
            return GateCheckResult(
                "no_error_page", False, f"page content matches error marker: {hit!r}"
            )
        return GateCheckResult("no_error_page", True)

    @staticmethod
    def _navigation_sane(b: EvidenceBundle) -> GateCheckResult:
        if b.url_after.strip().lower() in _DEAD_URLS:
            return GateCheckResult(
                "navigation_sane", False, f"landed on dead URL: {b.url_after!r}"
            )
        redirects = sum(1 for c in b.http if 300 <= c.status < 400)
        if redirects >= _MAX_REDIRECTS:
            return GateCheckResult(
                "navigation_sane",
                False,
                f"excessive redirects ({redirects}) — possible loop",
            )
        return GateCheckResult("navigation_sane", True)

    @staticmethod
    def _opened_pages_ok(b: EvidenceBundle) -> GateCheckResult:
        # New-tab / target="_blank" links: a document nav returning >=400 is a
        # broken external link the on-page checks would otherwise miss.
        bad = [c for c in b.opened if c.status >= 400]
        if not bad:
            return GateCheckResult("opened_pages_ok", True)
        first = bad[0]
        return GateCheckResult(
            "opened_pages_ok",
            False,
            f"new-tab link opened a broken page: {first.status} {first.url}",
        )

    @staticmethod
    def _target_survived(b: EvidenceBundle) -> GateCheckResult:
        # SOFTEST check — its design goal is to avoid false short-circuits, so it
        # is deliberately lenient. It fails ONLY when an element was acted on, the
        # page did not navigate, NOTHING was triggered (no request, no popup), and
        # the element is gone. Any network activity — even a GET — excuses a
        # disappearance: a submit/run button relabels after a POST, and a legit
        # GET-driven SPA update can replace the clicked element too. A vanish that
        # coincides only with an unrelated GET is a rare miss that outcome
        # verification (the AI pass) catches anyway — the right trade-off for a
        # check whose false positives would short-circuit that judgment.
        # (Verb-based tightening was tried and reverted: it false-flagged normal
        # GET-driven flows — see BUGS.md 2026-07-13.)
        if b.target_present is None:
            return GateCheckResult("target_survived", True, "n/a (no selector)")
        if b.url_after != b.url_before:
            return GateCheckResult("target_survived", True, "n/a (navigated)")
        if b.target_present:
            return GateCheckResult("target_survived", True)
        if b.http or b.opened:
            return GateCheckResult(
                "target_survived", True, "n/a (action triggered network activity)"
            )
        return GateCheckResult(
            "target_survived",
            False,
            "acted-on element vanished with no navigation or network activity",
        )
