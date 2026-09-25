"""Notice a call starting, so recording it is one click rather than a memory test.

Every calling app - Meet in a browser tab, WhatsApp, Zoom, Teams, Slack,
Discord, a softphone - has exactly one thing in common: it opens the
microphone. So rather than recognising apps one at a time, which would miss
whichever one somebody uses next, Scribe watches for the microphone being
taken.

Windows already tracks that per app, for the microphone indicator in the
taskbar. Under ``CapabilityAccessManager\\ConsentStore\\microphone`` every app
that has used the microphone has a key, and its ``LastUsedTimeStop`` reads zero
for as long as it is still using it. Reading that is a registry query: no
audio is opened, nothing is injected into any app, and no permission prompt
appears.

Two consequences shape the rest of this module.

**Anything that uses the microphone counts,** including things that are not
calls - a game's voice chat, dictation, a voice recorder. Each prompt can be
dismissed for that call or muted for good per app; guessing which apps are
"really" calls would be the per-app list this approach exists to avoid.

**Scribe uses the microphone itself** while it records. Its own interpreter is
never reported, and nothing is offered while a recording is already running.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from meetbot.transcript.notes import atomic_write

logger = logging.getLogger(__name__)

_CONSENT_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone"
)

#: FILETIME counts 100-nanosecond ticks from 1601; this is 1970 in those units.
_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000

#: How long an app must hold the microphone before it is worth a prompt. A
#: voice search or a sound check lets go within a second or two; a call does
#: not.
GRACE_S = 4.0

#: Sessions older than this are ignored. An app that crashed while holding the
#: microphone never writes its stop time, and would otherwise be "in a call"
#: forever.
MAX_SESSION_AGE_S = 6 * 3600.0

#: How often the service reads the microphone state.
POLL_INTERVAL_S = 2.0

#: Apps worth naming properly, matched against the executable name or the
#: Store package family. Anything else is named after its executable - which
#: is the point: an app missing from this list is still detected.
_KNOWN_APPS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), name)
    for pattern, name in (
        (r"whatsapp", "WhatsApp"),
        (r"zoom", "Zoom"),
        (r"teams", "Microsoft Teams"),
        (r"slack", "Slack"),
        (r"discord", "Discord"),
        (r"ciscocollabhost|webex", "Webex"),
        (r"skype", "Skype"),
        (r"telegram", "Telegram"),
        (r"^signal", "Signal"),
        (r"yourphone|phonelink", "Phone Link"),
        (r"granola", "Granola"),
        (r"^chrome$", "Chrome"),
        (r"^msedge$", "Edge"),
        (r"^brave$", "Brave"),
        (r"^firefox$", "Firefox"),
        (r"^opera", "Opera"),
        (r"^vivaldi$", "Vivaldi"),
        (r"^arc$", "Arc"),
    )
)

#: Apps that host other people's calls in a tab, so the app name alone says
#: nothing about which service the call is on.
BROWSERS = frozenset({"Chrome", "Edge", "Brave", "Firefox", "Opera", "Vivaldi", "Arc"})

#: Window titles that name a call service running in a browser. Consulted only
#: when a browser holds the microphone, and never sent anywhere.
_WEB_CALLS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bMeet\b|meet\.google\.com|\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b"), "Google Meet"),
    (re.compile(r"whatsapp", re.IGNORECASE), "WhatsApp"),
    (re.compile(r"microsoft teams", re.IGNORECASE), "Microsoft Teams"),
    (re.compile(r"\bzoom\b", re.IGNORECASE), "Zoom"),
    (re.compile(r"\bslack\b", re.IGNORECASE), "Slack"),
    (re.compile(r"\bdiscord\b", re.IGNORECASE), "Discord"),
    (re.compile(r"\bwebex\b", re.IGNORECASE), "Webex"),
    (re.compile(r"\bmessenger\b", re.IGNORECASE), "Messenger"),
    (re.compile(r"\bjitsi\b", re.IGNORECASE), "Jitsi"),
    (re.compile(r"\bskype\b", re.IGNORECASE), "Skype"),
    (re.compile(r"\btelegram\b", re.IGNORECASE), "Telegram"),
)
_MEET_CODE_RE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b")


def detection_available() -> tuple[bool, str]:
    """Whether calls can be noticed on this machine, and a line saying so."""
    if sys.platform != "win32":
        return False, "Call alerts need Windows - the same place Scribe records from."
    return True, "Watching for any app that starts using the microphone."


@dataclass(frozen=True)
class MicSession:
    """One app currently holding the microphone."""

    #: The registry key: a Store package family name, or an executable path
    #: with ``#`` in place of each backslash.
    app_id: str
    #: ``LastUsedTimeStart`` as a FILETIME. A new value means a new session.
    started: int
    packaged: bool

    @property
    def session_id(self) -> str:
        digest = hashlib.sha1(f"{self.app_id}|{self.started}".encode("utf-8"))
        return digest.hexdigest()[:12]

    @property
    def started_at(self) -> float:
        return max(0.0, (self.started - _FILETIME_UNIX_EPOCH) / 10_000_000)

    @property
    def executable(self) -> str:
        return "" if self.packaged else self.app_id.replace("#", "\\")

    @property
    def app_name(self) -> str:
        return app_display_name(self.app_id, packaged=self.packaged)


def app_display_name(app_id: str, *, packaged: bool) -> str:
    """A person-readable name for a microphone key."""
    if packaged:
        lookup = app_id.split("_", 1)[0]
        fallback = lookup.rsplit(".", 1)[-1]
    else:
        base = re.split(r"[#\\/]", app_id)[-1]
        lookup = fallback = base[:-4] if base.lower().endswith(".exe") else base
    for pattern, name in _KNOWN_APPS:
        if pattern.search(lookup):
            return name
    return fallback or app_id


def read_mic_sessions() -> list[MicSession]:
    """Every app holding the microphone right now. Empty off Windows."""
    if sys.platform != "win32":
        return []
    import winreg

    sessions: list[MicSession] = []
    for path, packaged in ((_CONSENT_KEY, True), (_CONSENT_KEY + r"\NonPackaged", False)):
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(root, name) as key:
                        started = int(winreg.QueryValueEx(key, "LastUsedTimeStart")[0])
                        stopped = int(winreg.QueryValueEx(key, "LastUsedTimeStop")[0])
                except (OSError, TypeError, ValueError):
                    continue
                if started and not stopped:
                    sessions.append(MicSession(name, started, packaged))
    return sessions


def visible_window_titles() -> list[str]:
    """Titles of the visible top-level windows. Empty off Windows."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd: Any, _lparam: Any) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value.strip():
                    titles.append(buffer.value)
        return True

    user32.EnumWindows(collect, 0)
    return titles


def web_call_in(titles: Iterable[str]) -> tuple[str, str]:
    """The call service a window title names, and its Meet code if it has one."""
    for title in titles:
        for pattern, service in _WEB_CALLS:
            if pattern.search(title):
                code = _MEET_CODE_RE.search(title) if service == "Google Meet" else None
                return service, code.group(1) if code else ""
    return "", ""


@dataclass(frozen=True)
class CallPrompt:
    """An offer to record a call that has just started."""

    id: str
    app_id: str
    app: str
    #: The call service when it differs from the app - Google Meet running
    #: in Chrome - otherwise empty.
    service: str
    #: What the recording will be called if the offer is taken.
    title: str
    since: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app_id": self.app_id,
            "app": self.app,
            "service": self.service,
            "title": self.title,
            "since": self.since,
        }


def _prompt_for(session: MicSession, titles: Sequence[str], now: float) -> CallPrompt:
    app = session.app_name
    service, code = web_call_in(titles) if app in BROWSERS else ("", "")
    title = f"Google Meet {code}" if code else f"{service or app} call"
    return CallPrompt(
        id=session.session_id,
        app_id=session.app_id,
        app=app,
        service=service,
        title=title,
        since=session.started_at or now,
    )


def _normalise_exe(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


class CallWatcher:
    """Turns microphone sessions into prompts, and remembers the answers.

    :meth:`poll` does the blocking work - a registry read and, when a browser
    holds the microphone, a walk of the window list - so the service runs it
    in a worker thread while routes answer prompts on the event loop. A lock
    keeps the two apart.

    Attributes:
        reader: Returns the current microphone sessions.
        titles: Returns the visible window titles.
        clock: Returns the time, in epoch seconds.
    """

    def __init__(
        self,
        mute_file: Path,
        *,
        reader: Callable[[], list[MicSession]] | None = None,
        titles: Callable[[], list[str]] | None = None,
        clock: Callable[[], float] | None = None,
        own_executables: Iterable[str] | None = None,
    ) -> None:
        self._mute_file = Path(mute_file)
        # Resolved here rather than as default arguments, so a test can
        # replace the module's reader before the service builds its watcher.
        self.reader = reader or read_mic_sessions
        self.titles = titles or visible_window_titles
        self.clock = clock or time.time
        own = (
            own_executables
            if own_executables is not None
            # A venv's python.exe is a launcher that starts the base
            # interpreter as a child, and the child is what opens the device.
            else (sys.executable, getattr(sys, "_base_executable", ""))
        )
        self._own = {_normalise_exe(path) for path in own if path}
        self._lock = threading.Lock()
        self._prompts: dict[str, CallPrompt] = {}
        self._first_seen: dict[str, float] = {}
        self._dismissed: set[str] = set()
        self._handled: set[str] = set()
        self._announced: set[str] = set()
        self._muted: dict[str, str] = self._load_muted()

    # -- reading -----------------------------------------------------------

    def poll(self, *, recording: bool = False) -> list[CallPrompt]:
        """Read the microphone once.

        Args:
            recording: Whether Scribe is already recording. Every call live
                at that moment counts as handled, so stopping a recording
                mid-call does not immediately offer to start another.

        Returns:
            Prompts that became worth announcing since the last poll.
        """
        sessions = [s for s in self.reader() if not self._is_own(s)]
        titles = (
            self._safe_titles()
            if any(s.app_name in BROWSERS for s in sessions)
            else []
        )
        now = self.clock()
        with self._lock:
            live: set[str] = set()
            for session in sessions:
                if session.started_at and now - session.started_at > MAX_SESSION_AGE_S:
                    continue
                sid = session.session_id
                live.add(sid)
                if recording:
                    self._handled.add(sid)
                existing = self._prompts.get(sid)
                if existing is None:
                    self._prompts[sid] = _prompt_for(session, titles, now)
                    self._first_seen[sid] = now
                elif existing.app in BROWSERS and not existing.service:
                    # A tab's title changes as the call page loads, so keep
                    # looking until it names a service.
                    labelled = _prompt_for(session, titles, now)
                    if labelled.service:
                        self._prompts[sid] = replace(
                            existing, service=labelled.service, title=labelled.title
                        )
            for sid in list(self._prompts):
                if sid not in live:
                    del self._prompts[sid]
                    self._first_seen.pop(sid, None)
            self._dismissed &= live
            self._handled &= live
            self._announced &= live
            fresh = [p for p in self._pending(now) if p.id not in self._announced]
            self._announced.update(p.id for p in fresh)
        return fresh

    def pending(self) -> list[CallPrompt]:
        """Calls still waiting for an answer, oldest first."""
        with self._lock:
            return self._pending(self.clock())

    def _pending(self, now: float) -> list[CallPrompt]:
        waiting = [
            prompt
            for sid, prompt in self._prompts.items()
            if sid not in self._dismissed
            and sid not in self._handled
            and prompt.app_id not in self._muted
            and now - self._first_seen.get(sid, now) >= GRACE_S
        ]
        return sorted(waiting, key=lambda p: p.since)

    def _is_own(self, session: MicSession) -> bool:
        return bool(session.executable) and _normalise_exe(session.executable) in self._own

    def _safe_titles(self) -> list[str]:
        try:
            return list(self.titles())
        except Exception:  # noqa: BLE001 - a label is never worth failing a poll
            logger.debug("Could not read window titles", exc_info=True)
            return []

    # -- answers -----------------------------------------------------------

    def find(self, prompt_id: str) -> CallPrompt | None:
        with self._lock:
            return self._prompts.get(prompt_id)

    def dismiss(self, prompt_id: str) -> bool:
        """Not this call. The next call from the same app still asks."""
        with self._lock:
            if prompt_id not in self._prompts:
                return False
            self._dismissed.add(prompt_id)
            return True

    def mark_handled(self, prompt_id: str) -> None:
        """This call is being recorded, so stop offering it."""
        with self._lock:
            if prompt_id in self._prompts:
                self._handled.add(prompt_id)

    def mute(self, app_id: str, name: str = "") -> None:
        """Never offer to record calls from this app."""
        with self._lock:
            known = next((p.app for p in self._prompts.values() if p.app_id == app_id), "")
            self._muted[app_id] = name or known or app_id
            snapshot = dict(self._muted)
        self._save_muted(snapshot)

    def unmute(self, app_id: str) -> bool:
        with self._lock:
            removed = self._muted.pop(app_id, None) is not None
            snapshot = dict(self._muted)
        if removed:
            self._save_muted(snapshot)
        return removed

    def muted(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"app_id": app_id, "app": name}
                for app_id, name in sorted(self._muted.items(), key=lambda kv: kv[1].lower())
            ]

    # -- persistence -------------------------------------------------------

    def _load_muted(self) -> dict[str, str]:
        try:
            payload = json.loads(self._mute_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        muted = payload.get("muted") if isinstance(payload, dict) else None
        if not isinstance(muted, dict):
            return {}
        return {str(k): str(v) for k, v in muted.items() if k}

    def _save_muted(self, muted: dict[str, str]) -> None:
        try:
            atomic_write(self._mute_file, json.dumps({"muted": muted}, indent=2))
        except OSError:
            logger.warning("Could not save the muted call apps", exc_info=True)


#: Shows a toast under PowerShell's registered app identity. The XML arrives in
#: an environment variable rather than on the command line, so nothing in a
#: title or a URL can break the quoting.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:SCRIBE_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$toast.Tag = $env:SCRIBE_TOAST_TAG
$toast.Group = 'scribe-calls'
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def toast_xml(prompt: CallPrompt, base_url: str) -> str:
    """The notification for ``prompt``, as Windows toast XML.

    "Start recording" opens Scribe with the prompt's id in the address. The
    page starts recording only when that id matches a call still waiting on
    the signed-in account. The id is random and appears nowhere except here
    and behind the login, so a link planted on another site cannot start a
    recording.
    """
    base = base_url.rstrip("/")
    heading = f"{prompt.service or prompt.app} call detected"
    body = (
        f"{prompt.app} is using your microphone. Take notes with Scribe? "
        "Nothing joins the call."
    )
    open_url = escape(f"{base}/?call={prompt.id}")
    record_url = escape(f"{base}/?record={prompt.id}")
    return (
        f'<toast activationType="protocol" launch="{open_url}" duration="long">'
        '<visual><binding template="ToastGeneric">'
        f"<text>{escape(heading)}</text><text>{escape(body)}</text>"
        "</binding></visual>"
        "<actions>"
        f'<action content="Start recording" activationType="protocol" arguments="{record_url}"/>'
        '<action content="Not now" activationType="system" arguments="dismiss"/>'
        "</actions></toast>"
    )


def show_toast(prompt: CallPrompt, base_url: str) -> bool:
    """Raise a Windows notification for ``prompt``.

    The card in the page only helps when Scribe's tab is in front of you,
    which during a call it usually is not. Returns whether Windows took it.
    """
    if sys.platform != "win32":
        return False
    env = {
        **os.environ,
        "SCRIBE_TOAST_XML": toast_xml(prompt, base_url),
        "SCRIBE_TOAST_TAG": prompt.id[:12],
    }
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _TOAST_SCRIPT,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not show the call notification: %s", exc)
        return False
    if completed.returncode != 0:
        logger.warning(
            "Windows refused the call notification: %s",
            (completed.stderr or completed.stdout).strip()[:300],
        )
        return False
    return True


__all__ = [
    "BROWSERS",
    "GRACE_S",
    "POLL_INTERVAL_S",
    "CallPrompt",
    "CallWatcher",
    "MicSession",
    "app_display_name",
    "detection_available",
    "read_mic_sessions",
    "show_toast",
    "toast_xml",
    "visible_window_titles",
    "web_call_in",
]
