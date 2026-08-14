"""Assemble an evidence bundle from before/after page states (Phase A).

The bundler is pure and browser-free: given two :class:`~engine.models.PageState`
snapshots (captured before and after an action), it derives the per-action deltas
that make up the spec §5 evidence bundle. This keeps it fully unit-testable
without launching a browser.
"""

from __future__ import annotations

from typing import List, Optional

from .models import (
    Action,
    ConsoleDelta,
    EvidenceBundle,
    GateResult,
    NetworkCall,
    PageState,
)


class EvidenceBundler:
    """Diff two page states into an :class:`EvidenceBundle`."""

    def build(
        self,
        action: Action,
        before: PageState,
        after: PageState,
        screenshot: Optional[str] = None,
        target_present: Optional[bool] = None,
        opened: Optional[List[NetworkCall]] = None,
        gate: Optional[GateResult] = None,
    ) -> EvidenceBundle:
        # Console/network/page-errors are cumulative and append-only, so the
        # delta for this action is whatever appeared beyond the "before" length.
        new_console = after.console[len(before.console) :]
        console = ConsoleDelta(
            errors=[m.text for m in new_console if m.level == "error"],
            warnings=[m.text for m in new_console if m.level in ("warning", "warn")],
        )
        http = after.network[len(before.network) :]
        page_errors = after.page_errors[len(before.page_errors) :]

        cookies_delta = {
            name: value
            for name, value in after.cookies.items()
            if before.cookies.get(name) != value
        }

        return EvidenceBundle(
            action=action,
            url_before=before.url,
            url_after=after.url,
            http=http,
            console=console,
            dom_outline_after=after.dom_outline,
            content_after=after.content,
            focus_after=after.focus,
            cookies_delta=cookies_delta,
            page_errors=page_errors,
            screenshot=screenshot,
            target_present=target_present,
            opened=opened or [],
            gate=gate,
        )
