"""`--low-memory` passes Chromium-only switches, so they must never reach a
firefox/webkit launch (post-commit review, 2026-09-22). Pure unit test of the
guard helper -- no browser needed."""

from __future__ import annotations

from engine.browser import _LOW_MEMORY_ARGS, _low_memory_args
from engine.models import BrowserEngine


def test_low_memory_args_apply_only_to_chromium():
    assert _low_memory_args(BrowserEngine.CHROMIUM, True) == _LOW_MEMORY_ARGS
    # Chromium switches would choke firefox/webkit -- correctly a no-op there.
    assert _low_memory_args(BrowserEngine.FIREFOX, True) == []
    assert _low_memory_args(BrowserEngine.WEBKIT, True) == []


def test_low_memory_args_empty_when_flag_off():
    assert _low_memory_args(BrowserEngine.CHROMIUM, False) == []
