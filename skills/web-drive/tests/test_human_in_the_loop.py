"""Ported from web-qa (2026-09-16): `fill` self-verification, `pause`, and
`wait_for_human`.

These three existed in web-qa's `browser.py` but were never carried into
web-drive's Phase A copy (spec §4.3 requires `browser.py` to stay byte-identical
— this had silently drifted). Found while building Phase C's `probe`, which
needs reliable form-filling to test `optional_but_required` reconciliation
against a controlled input. `ActionType.PAUSE` was also missing from
`models.py` entirely, so `flow.py`'s `ActionType(step["type"])` would have
raised on any `pause` step the moment `flow` is wired to the CLI (Phase E+).

web-drive has no `flow` CLI command yet, so these drive `BrowserController`
directly rather than through `CliRunner` (web-qa's equivalent test uses
`flow --steps`, unavailable here).
"""

from __future__ import annotations

import http.server
import threading
from contextlib import contextmanager

import pytest

from engine.browser import BrowserController
from engine.models import Action, ActionType, BrowserEngine

# A controlled input that behaves like React Native Web's TextInput: the
# component owns the value, so any `input` event that did not come from a
# real keystroke gets stomped back to component state on the next render.
PAGE = b"""<!doctype html>
<title>controlled-input fixture</title>
<input id="plain" placeholder="plain">
<input id="controlled" placeholder="controlled">
<script>
  var state = '';
  var keys = 0;
  var el = document.getElementById('controlled');
  el.addEventListener('keydown', function (e) {
    keys++;
    if (e.key === 'Backspace') { state = state.slice(0, -1); }
    else if (e.key === 'Delete') { state = ''; }
    else if (e.key.length === 1) { state += e.key; }
  });
  el.addEventListener('input', function () { el.value = state; });
  window.__keys = function () { return keys; };
</script>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — stdlib callback name
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *_args):
        return


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


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


async def _launch():
    controller = BrowserController(engine=BrowserEngine.CHROMIUM, headless=True)
    try:
        await controller.launch()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip("Chromium not installed for Playwright")
        raise
    return controller


@pytest.mark.asyncio
async def test_fill_lands_on_a_controlled_input_and_stays_fast_on_a_plain_one():
    controller = await _launch()
    try:
        with _server() as base:
            await controller.navigate(base)
            await controller.perform(
                Action(type=ActionType.FILL, selector="#plain", value="PlainValue")
            )
            await controller.perform(
                Action(type=ActionType.FILL, selector="#controlled", value="QATester")
            )
            plain = await controller.page.input_value("#plain")
            controlled = await controller.page.input_value("#controlled")
            keys = await controller.page.evaluate("() => window.__keys()")
    finally:
        await controller.close()

    assert plain == "PlainValue"
    # The controlled field actually holds the value — the defect this fixes.
    assert controlled == "QATester"
    # Non-zero proves the keystroke fallback actually fired for #controlled.
    assert keys > 0, f"expected the keystroke fallback to fire, saw KEYS:{keys}"


@pytest.mark.asyncio
async def test_plain_fill_does_not_type_character_by_character():
    controller = await _launch()
    try:
        with _server() as base:
            await controller.navigate(base)
            await controller.perform(
                Action(type=ActionType.FILL, selector="#plain", value="PlainValue")
            )
            plain = await controller.page.input_value("#plain")
            keys = await controller.page.evaluate("() => window.__keys()")
    finally:
        await controller.close()

    assert plain == "PlainValue"
    assert keys == 0  # the fast path never touched the controlled field's counter


@pytest.mark.asyncio
async def test_pause_refuses_to_run_headless():
    controller = await _launch()
    try:
        with _server() as base:
            await controller.navigate(base)
            with pytest.raises(RuntimeError, match="needs a browser the human can see"):
                await controller.perform(
                    Action(type=ActionType.PAUSE, text="solve it", value="5")
                )
    finally:
        await controller.close()
