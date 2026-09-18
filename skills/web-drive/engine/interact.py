"""``interact`` — a persistent, agent-driven browser session spanning
SEPARATE CLI invocations, for exploring a gated/stateful flow one action at a
time.

Motivating case: content-jumpstart.com's Project Wizard, whose step-tab
buttons stay disabled until prior page state (a selected client) is
provided. `map`'s `gated_controls`/`state_changing_controls` (v1.6) NAME
that a control is a lead; this command is how an agent actually FOLLOWS one
up — by clicking, watching what really happened, and deciding the next
click, against a live page instead of guessing a whole step sequence from
static markup and betting on `verify` passing in one shot.

Deliberately three raw primitives only (click / fill / read) and nothing that
guesses intent: no auto-fill of "plausible" values, no auto-chaining through
a flow, no semantic understanding of what a combobox or a wizard even is.
Every action is one explicit call the AGENT chooses to make. No AI/API key
here or anywhere else in this engine — the "agent" driving each call IS the
Claude Code session invoking this CLI, per the whole family's architecture.

Mechanics: ``start`` launches Playwright's OWN managed Chromium binary
directly (bypassing ``playwright.chromium.launch()``, which ties the
browser's life to the launching Python process) as a DETACHED OS process
with an OS-assigned remote-debugging port, recording its PID/port/profile
dir to a small JSON state file. Every later call is a separate, short-lived
process that connects over CDP (``connect_over_cdp``), finds the browser's
one context/page, performs ONE action, and lets the connection drop — which
never closes a CDP-attached browser (only ``stop``, killing the OS process,
does) — so page state (cookies, DOM, wherever a flow keeps its client-side
progress) survives between calls exactly like a human's own open tab would.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright

_STATE_SCHEMA = 1


class InteractError(Exception):
    """A user-facing interact failure (bad state file, port never came up, ...)."""


def _read_state(state_path: str) -> Dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        raise InteractError(
            f"no interact session at {state_path!r} -- run `interact start` first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


async def _fingerprint(page: Any) -> str:
    """Same cheap same-page change signal as sitemap.py's crawler, against a
    raw Playwright Page instead of a BrowserController (interact never owns
    a BrowserController -- its whole point is a browser that outlives the
    Python process that launched it)."""
    try:
        return await page.evaluate(
            "() => document.title + '|' + "
            "((document.body && document.body.innerText) || '').slice(0, 800)"
        )
    except Exception:  # noqa: BLE001 — a page mid-navigation has no stable content to read
        return ""


def _devtools_port(profile_dir: Path, timeout_s: float = 10.0) -> int:
    """Chrome launched with ``--remote-debugging-port=0`` picks a free port
    itself and writes it as the first line of ``DevToolsActivePort`` inside
    its user-data-dir -- the documented, race-free way to learn which port a
    freshly spawned instance actually bound, instead of guessing one
    ourselves and risking a collision with something else on the machine."""
    port_file = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_file.exists():
            lines = port_file.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip().isdigit():
                return int(lines[0].strip())
        time.sleep(0.1)
    raise InteractError(
        f"chromium did not report a DevTools port within {timeout_s}s "
        f"(profile dir: {profile_dir})"
    )


async def _chromium_executable() -> str:
    async with async_playwright() as p:
        return p.chromium.executable_path


def start_session(
    state_path: str,
    url: str,
    session: Optional[str] = None,
    user_agent: Optional[str] = None,
    headless: bool = True,
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    if Path(state_path).exists():
        raise InteractError(
            f"{state_path!r} already names a session -- `interact stop` it first "
            f"(starting over it would leak the old chromium process)"
        )

    storage_state = None
    if session:
        bundle = json.loads(Path(session).read_text(encoding="utf-8"))
        storage_state = bundle.get("storage_state")
        user_agent = user_agent or bundle.get("user_agent")

    executable = asyncio.run(_chromium_executable())
    profile_dir = Path(tempfile.mkdtemp(prefix="wd-interact-"))
    args = [
        executable,
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
    if user_agent:
        args.append(f"--user-agent={user_agent}")

    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except OSError as exc:
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise InteractError(
            f"could not launch chromium at {executable!r}: {exc}. "
            f"Executable doesn't exist -- run `playwright install chromium`."
        ) from exc
    try:
        port = _devtools_port(profile_dir, timeout_s)
    except InteractError:
        proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise

    async def _setup() -> Dict[str, Any]:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            # Chrome's OWN default context (contexts[0], never explicitly
            # CDP-created) rather than `browser.new_context()`: a context this
            # client creates over CDP is torn down the moment THIS connection
            # disconnects (confirmed live -- a `read` right after `start`
            # found no page at all), which defeats the entire point of a
            # session meant to outlive each individual CLI call. The default
            # context has no such lifecycle tie to any one client.
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            if storage_state:
                cookies = storage_state.get("cookies", [])
                if cookies:
                    await context.add_cookies(cookies)
                for origin_entry in storage_state.get("origins", []):
                    local_storage = origin_entry.get("localStorage", [])
                    origin = origin_entry.get("origin")
                    if origin and local_storage:
                        await page.goto(origin, wait_until="domcontentloaded")
                        for item in local_storage:
                            await page.evaluate(
                                "([k, v]) => localStorage.setItem(k, v)",
                                [item["name"], item["value"]],
                            )
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(300)
            content = await _fingerprint(page)
            return {"url": page.url, "title": await page.title(), "content_preview": content[:300]}

    try:
        result = asyncio.run(_setup())
    except Exception:
        proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise

    Path(state_path).write_text(
        json.dumps(
            {
                "schema": _STATE_SCHEMA,
                "pid": proc.pid,
                "port": port,
                "profile_dir": str(profile_dir),
                "entry_url": url,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"state": state_path, **result}


def _pick_page(browser: Any) -> Any:
    """Find the page `start_session` navigated, not Chrome's own internal one.

    Reusing `browser.contexts[0]` (see `start_session`) means there is
    normally exactly one context and one page. Filtering OUT chrome's own
    internal pages (`chrome://`, devtools) is the one signal that stays
    reliable regardless of position -- this module never navigates to one of
    those on purpose.

    Refuses on more than one candidate rather than guessing (post-commit
    review, 2026-09-18): a popup/new tab (`target=_blank`, `window.open`)
    leaves a SECOND non-internal page, and silently picking one -- by
    position or any other heuristic -- risks every later call reading or
    mutating the wrong surface, including a real action landing on a page the
    caller never intended. No stable page identity is tracked across the
    separate processes this module is built around, so ambiguity here has no
    safe automatic resolution; failing loudly matches this codebase's
    conservative-by-construction rule elsewhere (see probe.py)."""
    candidates = [
        page
        for context in browser.contexts
        for page in context.pages
        if not page.url.startswith(("chrome://", "chrome-extension://", "devtools://"))
    ]
    if not candidates:
        raise InteractError(
            "no interactable page found on this session's browser -- was "
            "`stop` already called against this state file, or its process "
            "killed some other way?"
        )
    if len(candidates) > 1:
        urls = ", ".join(p.url for p in candidates)
        raise InteractError(
            f"{len(candidates)} non-internal pages are open ({urls}) -- a click "
            "likely opened a popup/new tab. interact has no way to know which "
            "one you mean and will not guess; close the extra page yourself or "
            "avoid the control that opened it."
        )
    return candidates[0]


def click(state_path: str, text: Optional[str] = None, selector: Optional[str] = None) -> Dict[str, Any]:
    """``changed: false`` is not proof nothing happened -- confirmed live
    against content-jumpstart.com's own wizard: opening its client combobox
    reported `changed: false` (the 400ms settle below wasn't enough for that
    particular dropdown's render), yet a SEPARATE `read` immediately after
    clearly showed it open. Treat `changed`/`navigated` as a fast hint only;
    call `read` when the answer actually matters."""
    if not text and not selector:
        raise InteractError("pass --text or --selector")
    state = _read_state(state_path)

    async def _do() -> Dict[str, Any]:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{state['port']}")
            page = _pick_page(browser)
            before_url = page.url
            before = await _fingerprint(page)
            target = selector if selector else f"text={text}"
            await page.click(target, timeout=5000)
            await page.wait_for_timeout(400)
            after_url = page.url
            after = await _fingerprint(page)
            return {
                "clicked": target,
                "url": after_url,
                "navigated": after_url != before_url,
                "changed": after != before,
                "content_preview": after[:300],
            }

    return asyncio.run(_do())


def fill(state_path: str, selector: str, value: str) -> Dict[str, Any]:
    state = _read_state(state_path)

    async def _do() -> Dict[str, Any]:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{state['port']}")
            page = _pick_page(browser)
            await page.fill(selector, value, timeout=5000)
            return {"filled": selector, "value_len": len(value), "url": page.url}

    return asyncio.run(_do())


def read(state_path: str) -> Dict[str, Any]:
    state = _read_state(state_path)

    async def _do() -> Dict[str, Any]:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{state['port']}")
            page = _pick_page(browser)
            content = await _fingerprint(page)
            return {"url": page.url, "title": await page.title(), "content_preview": content[:800]}

    return asyncio.run(_do())


def _process_cmdline(pid: int) -> Optional[str]:
    """The running process's own command line, or:
    - ``""`` when the check genuinely ran and found no such process (it
      already exited) -- a confident negative.
    - ``None`` when the check itself could not run (platform tool missing,
      denied, or errored) -- unknown, not negative.

    Uses PowerShell's `Get-CimInstance` on Windows, not `wmic`: confirmed
    live on a current Windows 11 build that `wmic` itself returns a non-zero
    exit code and no output for an ordinary, real, currently-running PID --
    `wmic` is deprecated and unreliable-to-absent on modern Windows, not a
    rare corner case worth a graceful fallback for. `Get-CimInstance`
    confirmed working the same way for both a real PID and a nonexistent one
    (empty output, exit 0 -- a clean negative, not an error) before this was
    trusted for `stop`'s kill decision."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
                    "| Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if out.returncode != 0:
                return None
            return out.stdout or ""
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            return cmdline_path.read_text(encoding="utf-8", errors="replace").replace("\x00", " ")
        return ""
    except Exception:  # noqa: BLE001 — the check itself failed, not a negative result
        return None


def stop(state_path: str) -> Dict[str, Any]:
    """Kill the chromium `start_session` launched and remove its profile dir.

    Verifies the PID still names a process running from OUR OWN profile dir
    before killing it, and only ever removes a directory this module itself
    created (post-commit review, 2026-09-18): trusting a bare PID out of a
    JSON state file is unsafe on two counts -- the process can have already
    exited and the PID been reused by something unrelated by the time `stop`
    runs, and the file could be corrupted or hand-edited. Matching the
    profile dir against the live process's own command line, and restricting
    `rmtree` to a path this module's own `tempfile.mkdtemp(prefix="wd-interact-")`
    would have produced, means a stale or tampered state file can fail to
    clean up but can never kill an unrelated process or delete an arbitrary
    directory.

Refuses to kill whenever verification does not come back as a CONFIRMED
    match -- an unavailable check (`_process_cmdline` returns `None`) is
    treated the same as a confirmed non-match, not as "assume yes and kill
    anyway." A second review argued the opposite default (kill when
    unverifiable) to avoid leaking the chromium process; that was tried and
    reverted after it turned out `wmic` returns a non-zero exit for an
    ordinary, real, currently-running process on a real, current Windows
    build tested live -- "verification unavailable" is not the rare corner
    case that tradeoff assumed, it was effectively the ALWAYS case with
    `wmic`, which would have made the kill-anyway fallback fire on every
    single `stop` call and defeat the PID check entirely. Switching to
    PowerShell's `Get-CimInstance` (see `_process_cmdline`) fixed the
    underlying reliability problem instead of papering over it with a
    weaker default; an occasional leaked profile dir when a check tool is
    genuinely absent is still the correct failure mode to prefer over ever
    killing a process this session didn't launch.

    Two further hardenings a second review raised are DELIBERATELY NOT
    applied, and won't be without a concrete report of them mattering in
    practice: closing the microsecond TOCTOU window between this check and
    the kill call with an OS-level process handle, and replacing the
    profile-dir substring/prefix check with a cryptographic ownership token.
    Both add real complexity to a LOCAL, single-operator CLI helper with no
    adversarial input path -- the process this launches is on the same
    machine, under the same user, for the same short-lived session as the
    caller. The failure mode being defended against (a PID happens to be
    reused by something else in the instant between check and kill, or a
    hand-crafted state file's path happens to collide with an unrelated
    directory) is a nuisance for a dev tool, not a security compromise, and
    chasing it further is exactly the reviewer ping-pong this project's own
    convention says to stop and make a call on instead."""
    state = _read_state(state_path)
    profile_dir = state.get("profile_dir", "")

    # `pid` reaches a PowerShell command STRING in `_process_cmdline` (and a
    # taskkill argv) -- a state file is exactly the "could be tampered" input
    # this function's own docstring already reasons about, so a non-integer
    # value here must never survive to be interpolated into either (a third
    # post-commit review round correctly caught this: a hand-edited "pid" of
    # e.g. "0; Remove-Item C:\\" would otherwise execute as PowerShell).
    # str/int's own conversion rules are the whole check: a genuine int (or a
    # clean digit string) survives, anything else raises here, well before
    # either subprocess call.
    try:
        pid = int(state["pid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InteractError(
            f"{state_path!r} has a non-integer pid ({state.get('pid')!r}) -- "
            "refusing to use it in a process-management command"
        ) from exc

    cmdline = _process_cmdline(pid) if profile_dir else ""
    if cmdline is not None and profile_dir in cmdline:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):  # noqa: BLE001 — already gone is fine
            pass

    profile_path = Path(profile_dir) if profile_dir else None
    if (
        profile_path is not None
        and profile_path.name.startswith("wd-interact-")
        and profile_path.parent == Path(tempfile.gettempdir())
    ):
        shutil.rmtree(profile_path, ignore_errors=True)
    Path(state_path).unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


__all__ = ["InteractError", "start_session", "click", "fill", "read", "stop"]
