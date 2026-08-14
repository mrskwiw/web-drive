"""EvidenceBundler delta logic — pure, no browser required."""

from engine.evidence import EvidenceBundler
from engine.models import (
    Action,
    ActionType,
    ConsoleMessage,
    NetworkCall,
    PageState,
)


def _state(
    url,
    *,
    console=None,
    network=None,
    page_errors=None,
    cookies=None,
    focus=None,
    content="",
):
    return PageState(
        url=url,
        title="t",
        ready_state="complete",
        console=console or [],
        network=network or [],
        page_errors=page_errors or [],
        focus=focus,
        cookies=cookies or {},
        dom_outline='nav "Home" {#home}',
        content=content,
    )


def test_deltas_are_only_what_appeared_after_the_action():
    before = _state(
        "https://e.com",
        console=[ConsoleMessage("log", "pre")],
        network=[NetworkCall("GET", "https://e.com", 200)],
        cookies={"sid": "1"},
    )
    after = _state(
        "https://e.com/done",
        console=[
            ConsoleMessage("log", "pre"),
            ConsoleMessage("error", "kaboom"),
            ConsoleMessage("warning", "careful"),
        ],
        network=[
            NetworkCall("GET", "https://e.com", 200),
            NetworkCall("POST", "https://e.com/api", 500),
        ],
        page_errors=["Uncaught TypeError"],
        cookies={"sid": "2", "new": "x"},
        focus="#done",
        content="Order confirmed. Total: $42.",
    )

    bundle = EvidenceBundler().build(
        Action(type=ActionType.CLICK, selector="button"), before, after
    )

    assert bundle.url_before == "https://e.com"
    assert bundle.url_after == "https://e.com/done"
    # only the post-action console lines, split by level
    assert bundle.console.errors == ["kaboom"]
    assert bundle.console.warnings == ["careful"]
    # only the new network call
    assert [c.url for c in bundle.http] == ["https://e.com/api"]
    assert bundle.page_errors == ["Uncaught TypeError"]
    # changed + added cookies, not unchanged ones
    assert bundle.cookies_delta == {"sid": "2", "new": "x"}
    assert bundle.focus_after == "#done"
    # readable content passes through for outcome verification
    assert bundle.content_after == "Order confirmed. Total: $42."
    assert bundle.gate is None  # Phase B


def test_no_change_yields_empty_deltas():
    s = _state("https://e.com", cookies={"sid": "1"})
    other = _state("https://e.com", cookies={"sid": "1"})
    bundle = EvidenceBundler().build(
        Action(type=ActionType.WAIT_FOR, selector="x"), s, other
    )
    assert bundle.console.errors == []
    assert bundle.http == []
    assert bundle.cookies_delta == {}
