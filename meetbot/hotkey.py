"""A keyboard shortcut that works whichever window has focus.

Muting yourself mid-call is a reflex, and it happens in the call app, not in
Scribe. A button on a page you would have to bring forward first is too slow
for the moment somebody leans over to ask you something, so Ctrl+Alt+M mutes
Scribe's hearing of your microphone from anywhere.

**Why RegisterHotKey.** Windows delivers a registered hotkey as a message to
the thread that registered it, whatever has focus. No keyboard hook, nothing
that sees any other keystroke, and no dependency: it is two calls through
ctypes and a message loop on a thread of its own.

**When it cannot be had.** Only one program may hold a combination. If
another already has Ctrl+Alt+M, registering fails, and that is reported
rather than raised - the button and the voice command still work, and a
recorder that refuses to start over a shortcut would be absurd.

Windows only. Elsewhere :meth:`GlobalHotkey.start` declines and says so.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Modifier flags and messages, from WinUser.h.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
#: Holding the keys down fires once, not once per auto-repeat - a held
#: shortcut that toggled mute ten times a second would land on a coin toss.
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

#: The combination that toggles the microphone, as people read it.
MIC_SHORTCUT = "Ctrl+Alt+M"

#: How long :meth:`GlobalHotkey.start` waits to hear whether Windows agreed.
START_TIMEOUT_S = 5.0


class GlobalHotkey:
    """One system-wide shortcut, calling back on a thread of its own.

    The callback runs on the hotkey's thread, so anything that has to happen
    on an event loop must be handed to it - the same rule as the voice
    listener's.
    """

    def __init__(
        self,
        on_press: Callable[[], None],
        *,
        key: str = "M",
        modifiers: int = MOD_CONTROL | MOD_ALT,
        name: str = MIC_SHORTCUT,
    ) -> None:
        self._on_press = on_press
        self._key = key.upper()
        self._modifiers = modifiers
        self.name = name
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered = threading.Event()
        self._answered = threading.Event()
        self.detail = "Not registered."

    @property
    def active(self) -> bool:
        return self._registered.is_set()

    def start(self) -> bool:
        """Claim the shortcut. Returns whether Windows gave it to us."""
        if sys.platform != "win32":
            self.detail = f"{self.name} is only available on Windows."
            return False
        if self._thread is not None:
            return self.active
        self._thread = threading.Thread(
            target=self._run, name="scribe-hotkey", daemon=True
        )
        self._thread.start()
        self._answered.wait(START_TIMEOUT_S)
        return self.active

    def stop(self) -> None:
        """Give the shortcut back. Idempotent."""
        thread, self._thread = self._thread, None
        if thread is None or not self._thread_id:
            return
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(2.0)

    def _run(self) -> None:  # pragma: no cover - needs a Windows desktop
        from ctypes import wintypes

        user32: Any = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        ok = user32.RegisterHotKey(
            None, 1, self._modifiers | MOD_NOREPEAT, ord(self._key)
        )
        if not ok:
            self.detail = (
                f"{self.name} is already taken by another program, so it "
                "cannot mute Scribe. The button and voice commands still do."
            )
            logger.warning("Shortcut: %s", self.detail)
            self._answered.set()
            return
        self._registered.set()
        self.detail = f"{self.name} mutes your microphone in Scribe."
        logger.info("Shortcut: %s", self.detail)
        self._answered.set()

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message != WM_HOTKEY:
                    continue
                try:
                    self._on_press()
                except Exception:  # noqa: BLE001 - a bad press must not end the loop
                    logger.exception("Shortcut handler failed")
        finally:
            user32.UnregisterHotKey(None, 1)
            self._registered.clear()


__all__ = ["MIC_SHORTCUT", "GlobalHotkey"]
