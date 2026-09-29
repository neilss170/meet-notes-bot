"""The window Scribe opens, so that Scribe can close it again.

A page you opened yourself cannot be closed by the program that served it. A
tab may only be closed by the script that opened it, which is a browser
security rule and a good one - otherwise any page could shut your work. So
quitting from the notification area left the page sitting there: live-looking,
answering nothing, and yours to tidy up.

The way out is for Scribe to open a window of its own. Chromium browsers take
``--app=<url>``, which gives a window with no tab strip and no address bar,
titled by the page rather than by the browser. Two things follow from that
title: it looks like an application rather than a browser someone left open,
and it is *findable* afterwards. A tabbed window is always titled
``<page> - Google Chrome``; an app window is titled ``<page>`` alone. So the
window carrying Scribe can be picked out from every other window the browser
has open, and closed with a message to that one window - his other tabs never
notice.

Deliberately no separate browser profile. Owning the browser *process* would
be the other way to do this, and it needs ``--user-data-dir``, which means a
profile with no extensions, no bookmarks and no session - he would sign in to
Scribe again, in a window that is not quite his browser. Closing by window
instead keeps the browser he actually uses.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

#: The page's ``<title>``. An app window wears it unchanged, which is how its
#: window is recognised later.
APP_TITLE = "Scribe"

#: What a browser appends to a *tabbed* window's title. A title carrying any
#: of these belongs to a window full of somebody's tabs, and is not ours to
#: close however much of the word "Scribe" is in it.
TABBED_SUFFIXES = (
    " - Google Chrome",
    " - Microsoft​ Edge",
    " - Microsoft Edge",
    " - Brave",
    " - Chromium",
    " - Mozilla Firefox",
)

#: Chromium-based browsers, in the order they are looked for.
BROWSERS = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe")

#: Every Chromium window is this class, app window or not.
WINDOW_CLASS = "Chrome_WidgetWin_1"

#: How long :func:`close_windows` waits for a window to actually go.
CLOSE_TIMEOUT_S = 6.0


def is_app_window(title: str) -> bool:
    """Whether a window with this title is a page Scribe opened.

    Kept apart from any Windows call so the rule that decides what may be
    closed can be read, and tested, on its own.
    """
    title = title.strip()
    if APP_TITLE not in title:
        return False
    return not any(suffix in title for suffix in TABBED_SUFFIXES)


def _registered_browser(name: str) -> Path | None:
    """Where Windows says a browser is installed, if it says at all."""
    if sys.platform != "win32":
        return None
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
        try:
            with winreg.OpenKey(root, key) as handle:
                path = Path(str(winreg.QueryValueEx(handle, "")[0]))
        except OSError:
            continue
        if path.exists():
            return path
    return None


def _guessed_locations(name: str) -> Iterator[Path]:
    """The places an installer would have put it."""
    vendors = {
        "chrome.exe": Path("Google/Chrome/Application"),
        "msedge.exe": Path("Microsoft/Edge/Application"),
        "brave.exe": Path("BraveSoftware/Brave-Browser/Application"),
        "chromium.exe": Path("Chromium/Application"),
    }
    folder = vendors.get(name)
    if folder is None:
        return
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            yield Path(base) / folder / name


def find_browser() -> Path | None:
    """A Chromium browser that can open an app window, or ``None``.

    Asked of Windows first, because a browser can be installed per-user, in
    Program Files, or in neither place on a machine somebody has tidied.
    """
    for name in BROWSERS:
        registered = _registered_browser(name)
        if registered is not None:
            return registered
        for candidate in _guessed_locations(name):
            if candidate.exists():
                return candidate
        found = shutil.which(name) or shutil.which(name.removesuffix(".exe"))
        if found:
            return Path(found)
    return None


def open_window(url: str) -> bool:
    """Open *url* as its own window. ``False`` if there is no browser for it.

    The browser is not waited for and its process is not kept: with the
    ordinary profile it usually hands the request to the browser already
    running and exits immediately. What is kept is the window, which is found
    again by its title.
    """
    browser = find_browser()
    if browser is None:
        logger.info("No Chromium browser found; falling back to a plain tab.")
        return False
    command = [
        str(browser),
        f"--app={url}",
        # A window Scribe opened should be a window, not a first-run tour.
        "--no-first-run",
        "--no-default-browser-check",
    ]
    try:
        extra: dict[str, object] = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.DETACHED_PROCESS
        else:
            extra["start_new_session"] = True
        subprocess.Popen(  # noqa: S603 - a fixed command, no shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **extra,  # type: ignore[arg-type]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not open a window with %s: %s", browser.name, exc)
        return False
    return True


# -- finding and closing the window ---------------------------------------


def process_name(pid: int) -> str:
    """The executable behind a process id, lowercased, or ``""``.

    The window class is not enough on its own: every Electron application uses
    Chromium's, so an editor with a file called Scribe open is a window that
    looks exactly like ours until you ask which program owns it.
    """
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return Path(buffer.value).name.casefold()
    finally:
        kernel32.CloseHandle(handle)


def _windows() -> Iterator[tuple[int, str]]:
    """Every visible top-level browser window: its handle and its title.

    Windows owned by anything that is not one of :data:`BROWSERS` are dropped
    here, so nothing downstream can be tricked into closing an editor.
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value != WINDOW_CLASS:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if process_name(int(pid.value)) not in BROWSERS:
            return True
        found.append((int(hwnd), title.value))
        return True

    user32.EnumWindows(visit, 0)
    yield from found


def app_windows() -> list[int]:
    """Handles of the windows Scribe has open."""
    return [hwnd for hwnd, title in _windows() if is_app_window(title)]


def close_windows(timeout: float = CLOSE_TIMEOUT_S) -> int:
    """Close every window Scribe opened. Returns how many actually went.

    ``WM_CLOSE`` is a request, the same one the X button sends - the window
    runs its own teardown and the browser around it is untouched. Anything
    still standing at the deadline is left alone rather than killed: a browser
    process holds other people's tabs.
    """
    if sys.platform != "win32":
        return 0
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    WM_CLOSE = 0x0010

    handles = app_windows()
    for hwnd in handles:
        with contextlib.suppress(OSError):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    deadline = time.monotonic() + timeout
    remaining = list(handles)
    while remaining and time.monotonic() < deadline:
        remaining = [hwnd for hwnd in remaining if user32.IsWindow(hwnd)]
        if remaining:
            time.sleep(0.1)
    closed = len(handles) - len(remaining)
    if remaining:
        logger.info("%d Scribe window(s) did not close in time.", len(remaining))
    elif closed:
        logger.info("Closed %d Scribe window(s).", closed)
    return closed


def focus_window() -> bool:
    """Bring an open Scribe window to the front. ``False`` if there is none.

    Windows only lets the foreground application hand the foreground on, so
    this can be refused - in which case the window flashes in the taskbar,
    which is Windows saying "it is over here" and is good enough.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    SW_RESTORE = 9

    for hwnd in app_windows():
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if not user32.SetForegroundWindow(hwnd):
            # Undocumented, and the thing that works when the rules say no.
            with contextlib.suppress(Exception):
                user32.SwitchToThisWindow(hwnd, True)
        return True
    return False


__all__ = [
    "APP_TITLE",
    "BROWSERS",
    "CLOSE_TIMEOUT_S",
    "TABBED_SUFFIXES",
    "WINDOW_CLASS",
    "app_windows",
    "close_windows",
    "find_browser",
    "focus_window",
    "is_app_window",
    "open_window",
    "process_name",
]
