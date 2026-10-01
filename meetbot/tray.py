"""Scribe in the notification area.

The server runs windowless, which is what makes it something you can forget
about - and also what leaves nothing to click. Minimise the page and Scribe is
gone: no window in the taskbar, no way to tell whether it is listening, and
stopping it means finding the right ``pythonw.exe`` in Task Manager and hoping.

So while Scribe runs, the icon *is* Scribe. Hovering it says what is
happening, clicking it brings the page back, and its menu holds the things
worth doing without the page open: start or stop a recording, jump to the
notes that were just written, and a quit that lets the recording finish
instead of killing it mid-sentence.

The icon and the server share one process on purpose. A separate tray process
would have to ask the server what it is doing over HTTP, and every route worth
asking needs an account - the icon would need a password of its own, or a hole
in the accounts, just to draw a tooltip. In one process it reads the job list
directly and there is nothing to authenticate.

One machine-level side effect is worth knowing about: Windows 11 hides a
notification icon it has not seen before, and the only way to change that is a
registry value or a drag with the mouse, so :func:`promote` writes the value
for our own entry. An icon behind the chevron cannot tell you anything at a
glance, which is the whole point of having one.

Two consequences are handled here. The server's event loop belongs to another
thread, so anything that touches a job is handed to that thread rather than
called from the menu (:meth:`ScribeTray._on_server`). And Windows keeps the
menu it was last given, so what an action does is decided from the live state
rather than from the label that was clicked.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from meetbot import browser
from meetbot.desktop import spark_image

logger = logging.getLogger(__name__)

#: How often the icon re-reads what the server is doing. The tooltip carries a
#: running clock, so this is also how stale that clock can be - two seconds is
#: below noticing, and the cost is a dictionary scan.
POLL_S = 2.0

#: Windows truncates a notification-area tooltip at 128 characters including
#: the terminator, and silently cuts anything longer mid-word.
TOOLTIP_LIMIT = 127

#: Drawn at 32 and left to Windows to scale down: the notification area asks
#: for 16 or 24 depending on the display, and a downscale reads better than
#: whatever it would do with a 256-pixel mark.
ICON_SIZE = 32

#: Longest meeting name shown in the menu or the tooltip. Names come from
#: whoever typed them, and a tooltip is a single line.
NAME_LIMIT = 40

#: What a recording started from the icon is called. Nobody is at a keyboard
#: when the menu is used, so the time it started is the most useful thing the
#: name can carry until it is renamed.
TITLE_FORMAT = "Recording %H:%M"

#: How long quitting waits for the server to unwind. The service gives notes
#: that are still being written 20 seconds of grace on shutdown; this has to
#: outlast that, or quitting would abandon the write-up it just triggered.
QUIT_TIMEOUT_S = 45.0

#: Where Windows 11 remembers which notification icons it will actually show.
#: An icon it has not seen before is filed here and hidden behind the chevron,
#: which for this one amounts to not existing: an icon you have to go looking
#: for cannot tell you at a glance whether Scribe is listening.
NOTIFY_ICON_KEY = r"Control Panel\NotifyIconSettings"

#: How many polls to keep looking for our entry before letting it be. Windows
#: writes it when the icon is first handed over, which is usually immediate
#: and occasionally a second later.
PROMOTE_TRIES = 5

#: How long a menu click waits for the server's loop to answer. Starting or
#: stopping a job is bookkeeping - the recording itself unwinds in the
#: background - so anything slower than this means the loop is wedged, and
#: saying so beats a menu that hangs.
ACT_TIMEOUT_S = 15.0


class TrayError(Exception):
    """Something a menu click should say out loud rather than log and forget."""


def available() -> tuple[bool, str]:
    """Whether an icon can be shown here, and what to say when it cannot."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False, (
            "The notification-area icon needs: pip install pystray pillow. "
            "Running without it - Scribe is only reachable at its address."
        )
    except Exception as exc:  # noqa: BLE001
        # pystray chooses a backend while being imported, and the X11 one
        # opens the display as it does so - so on a machine without one this
        # raises rather than failing to import. `tray` is documented to fall
        # back to serving without an icon, and an Xlib traceback where a
        # server should be is not that.
        return False, (
            f"No notification area here ({type(exc).__name__}: {exc}). "
            "Running without it - Scribe is only reachable at its address."
        )
    return True, "An icon in the notification area."


def promote(
    tooltip: str = "Scribe", executable: str | None = None, key: str = NOTIFY_ICON_KEY
) -> bool:
    """Ask Windows to keep our icon beside the clock rather than in the flyout.

    Windows 11 gives every icon it has ever seen a key of its own and shows
    only the ones marked promoted. There is no API for it - the setting is
    this registry value or a drag with the mouse - and it is the difference
    between a status you can glance at and one nobody will ever find.

    Nothing else is touched: an entry has to carry both our tooltip and this
    interpreter's own path before anything is written to it, and the only
    value written is the one that decides where the icon sits. Dragging the
    icon back into the flyout undoes it.

    Returns:
        Whether an entry was found and marked. ``False`` means Windows has not
        filed the icon yet, or this is not Windows.
    """
    if sys.platform != "win32":
        return False
    import winreg

    wanted = (executable or sys.executable).casefold()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, index)
                access = winreg.KEY_READ | winreg.KEY_SET_VALUE
                with winreg.OpenKey(root, name, 0, access) as entry:
                    try:
                        tip = str(winreg.QueryValueEx(entry, "InitialTooltip")[0])
                        path = str(winreg.QueryValueEx(entry, "ExecutablePath")[0])
                    except OSError:
                        continue
                    if not tip.startswith(tooltip) or path.casefold() != wanted:
                        continue
                    winreg.SetValueEx(entry, "IsPromoted", 0, winreg.REG_DWORD, 1)
                    logger.info("Keeping the Scribe icon beside the clock.")
                    return True
    except OSError:
        logger.debug("Could not read %s", key, exc_info=True)
    return False


def clock(seconds: float) -> str:
    """``4:07``, or ``1:04:07`` once a meeting has run over the hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def job_name(job: Any) -> str:
    """What to call a meeting in a single line of menu."""
    raw = getattr(job, "title", "") or getattr(job, "meet_url", "") or "a meeting"
    return _clip(str(raw), NAME_LIMIT)


@dataclass(frozen=True)
class Status:
    """What the icon has to say about the server behind it.

    Attributes:
        label: The menu's first line. Deliberately free of the running clock:
            Windows only re-reads the menu when it is handed a new one, and
            rebuilding it every two seconds risks doing so while it is open.
        recording: A meeting is being captured right now.
        writing: A write-up is in flight for a meeting that has just ended.
        ready: Some way of recording works. ``False`` means the preflight
            checks found a blocker, and the page is where it is explained.
        can_start: This machine can be recorded right now - the gate on the
            menu's own start item, which only ever records locally.
        elapsed_s: How long the live meeting has been running.
        live_id: The job to stop. Empty when nothing is running.
        latest_id: The newest finished meeting, for "Latest notes".
        detail: The blockers, named, for the tooltip.
    """

    label: str
    recording: bool = False
    writing: bool = False
    ready: bool = True
    can_start: bool = True
    elapsed_s: float = 0.0
    live_id: str = ""
    latest_id: str = ""
    detail: str = ""


def _writing_job(state: Any, jobs: list[Any]) -> Any | None:
    """The meeting whose notes are being written, if any.

    The service tracks LLM calls in flight as ``"<job id>:enhance"`` so a
    double-click cannot spend the quota twice; the same set answers "is Scribe
    still busy with the call I just ended?", which is the one thing worth
    waiting for before quitting.
    """
    flight = getattr(state, "in_flight", None) or ()
    ids = {str(key).split(":", 1)[0] for key in flight}
    if not ids:
        return None
    return next((job for job in jobs if getattr(job, "id", "") in ids), None)


def read_status(app: Any) -> Status:
    """Ask the running service what it is doing.

    Reads the job list and the last preflight report straight off the
    application, which is the whole reason the icon lives in the server's
    process. Nothing here mutates anything, so it is safe from the icon's
    thread even though the jobs belong to the loop's.
    """
    state = getattr(app, "state", None)
    manager = getattr(state, "manager", None)
    if manager is None:
        # The icon can be up before the loop has started, and during that
        # second there is genuinely nothing to report.
        return Status(label="Starting…", ready=False, can_start=False)

    jobs = list(manager.list())
    live = next((job for job in jobs if not job.status.is_terminal), None)
    writing = _writing_job(state, jobs)
    latest = next((job for job in jobs if job.status.is_terminal), None)

    health = getattr(state, "health", None)
    # No report yet means the checks have not finished, not that they failed:
    # an icon that cries "setup needed" for the first few seconds of every
    # sign-in teaches people to ignore it.
    ready = True if health is None else bool(health.can_record)
    can_start = True if health is None else bool(health.can_record_locally)
    detail = ""
    if health is not None and not ready:
        detail = ", ".join(check.name for check in health.blockers)

    if live is not None:
        label = f"Recording · {job_name(live)}"
    elif writing is not None:
        label = f"Writing up · {job_name(writing)}"
    elif not ready:
        label = f"Setup needed · {detail}" if detail else "Setup needed"
    else:
        label = "Not recording"

    return Status(
        label=label,
        recording=live is not None,
        writing=writing is not None,
        ready=ready,
        can_start=can_start,
        elapsed_s=live.elapsed_s if live is not None else 0.0,
        live_id=getattr(live, "id", "") if live is not None else "",
        latest_id=getattr(latest, "id", "") if latest is not None else "",
        detail=detail,
    )


def tooltip(status: Status) -> str:
    """The line Windows shows on hover.

    This is the only place the clock appears, and the reason the tooltip is
    worth anything: "is it actually listening, and for how long" is the
    question a hidden recorder has to answer without being opened.
    """
    text = f"Scribe — {status.label}"
    if status.recording:
        text = f"{text} · {clock(status.elapsed_s)}"
    return _clip(text, TOOLTIP_LIMIT)


@dataclass(frozen=True)
class Item:
    """One line of menu, described without reference to any tray library.

    Keeping the description separate from pystray is what makes the menu
    testable: what it says, what is greyed out and what each line does are
    decided here, and the binding below only translates.
    """

    label: str
    action: str | None = None
    default: bool = False
    enabled: bool = True
    separator: bool = False


#: A divider. Its label is never shown.
SEPARATOR = Item("-", separator=True)


def menu_for(status: Status, *, log: bool = False, folder: bool = True) -> list[Item]:
    """The menu as it should look for this state.

    The first line is the status itself rather than a title: a menu that opens
    with "Scribe" wastes the one glance somebody gives it, and what they want
    to know is whether it is recording.
    """
    items = [
        Item(status.label, enabled=False),
        SEPARATOR,
        Item("Open Scribe", action="open", default=True),
    ]
    if status.recording:
        items.append(Item("Stop recording", action="toggle"))
    else:
        # Greyed out rather than hidden when the machine cannot be recorded:
        # a missing item reads as a missing feature, and the tooltip says why.
        items.append(Item("Start recording", action="toggle", enabled=status.can_start))
    items.append(Item("Latest notes", action="latest", enabled=bool(status.latest_id)))

    # The group of things to open on disk. Skipped entirely when there is
    # nothing in it, or its two separators would meet and print as one line
    # of nothing.
    places = []
    if folder:
        places.append(Item("Recordings folder", action="folder"))
    if log:
        places.append(Item("Server log", action="log"))
    if places:
        items.append(SEPARATOR)
        items.extend(places)

    items.append(SEPARATOR)
    items.append(
        Item(
            "Stop recording and quit" if status.recording else "Quit Scribe",
            action="quit",
        )
    )
    return items


def open_path(path: Path) -> None:
    """Show a file or folder in whatever the desktop uses to show them."""
    if not path.exists():
        raise TrayError(f"There is no {path.name} yet.")
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - a path this process chose
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    with contextlib.suppress(OSError):
        subprocess.Popen([opener, str(path)])  # noqa: S603 - fixed command


def background_server(
    app: Any, *, host: str = "127.0.0.1", port: int = 8080, log_level: str = "info"
) -> tuple[Any, threading.Thread]:
    """Run the service on a thread, leaving this one for the icon.

    The icon has to own the main thread - its message loop is what makes a
    notification-area icon work - so uvicorn goes to a worker. It copes: it
    only installs signal handlers when it finds itself on the main thread, and
    Ctrl+C is not how this process is meant to be stopped anyway.
    """
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            # uvicorn otherwise replaces the handlers configure_logging set up.
            log_config=None,
        )
    )
    thread = threading.Thread(target=server.run, name="scribe-server", daemon=True)
    thread.start()
    return server, thread


def stop_server(
    server: Any, thread: threading.Thread, timeout: float = QUIT_TIMEOUT_S
) -> bool:
    """Ask the service to shut down, and wait for it to mean it.

    Returns:
        Whether it finished within *timeout*. A ``False`` here is worth
        logging: it means a recording or a write-up was abandoned.
    """
    server.should_exit = True
    thread.join(timeout)
    return not thread.is_alive()


class ScribeTray:
    """The icon, its menu, and the thread that keeps them current."""

    def __init__(
        self,
        app: Any,
        *,
        url: str,
        on_quit: Callable[[], None],
        log_path: Path | None = None,
        output_dir: Path | None = None,
    ) -> None:
        """
        Args:
            app: The running FastAPI application, read for its job list.
            url: Where the page is served, for the links this opens.
            on_quit: Shuts the server down. Called from the icon's thread and
                allowed to block while a recording finishes.
            log_path: The windowless server's log, offered if it exists.
            output_dir: Where recordings land, offered as a folder to open.
        """
        self._app = app
        self.url = url.rstrip("/")
        self._on_quit = on_quit
        self._log_path = Path(log_path) if log_path else None
        self._output_dir = Path(output_dir) if output_dir else None
        self._icon: Any | None = None
        self._stopping = threading.Event()
        self._shown = ""
        self._tip = ""
        self._recording_icon = False
        self._promote_tries = PROMOTE_TRIES

    # -- what it is saying -------------------------------------------------

    def status(self) -> Status:
        return read_status(self._app)

    def items(self) -> list[Item]:
        return menu_for(
            self.status(),
            log=bool(self._log_path and self._log_path.exists()),
            folder=self._output_dir is not None,
        )

    def notify(self, message: str) -> None:
        """Say something where somebody will see it.

        There is no window to put an error in, so a failed menu click has to
        arrive as a notification or not at all.
        """
        logger.info("Tray: %s", message)
        icon = self._icon
        if icon is None:
            return
        with contextlib.suppress(Exception):
            icon.notify(message, "Scribe")

    # -- what its menu does ------------------------------------------------

    def invoke(self, action: str) -> None:
        """Run one menu action.

        Everything is caught: a click that raises would otherwise take the
        message loop with it, and an icon that vanishes when pressed is worse
        than one that says the thing did not work.
        """
        handlers: dict[str, Callable[[], Any]] = {
            "open": self.open_page,
            "toggle": self.toggle_recording,
            "latest": self.open_latest,
            "folder": self.open_folder,
            "log": self.open_log,
            "quit": self.quit,
        }
        handler = handlers.get(action)
        if handler is None:
            logger.warning("Tray: no such action %r", action)
            return
        try:
            handler()
        except TrayError as exc:
            self.notify(str(exc))
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("Tray: %s failed", action)
            self.notify("That did not work. The details are in the log.")

    def open_page(self, path: str = "") -> None:
        """Bring Scribe's window up, opening one if there is not one already.

        A window already open is raised rather than duplicated: clicking the
        icon twice should not leave two copies of the same page, and the one
        that is open may have half a sentence typed into its notepad.
        """
        if not path and browser.focus_window():
            return
        if browser.open_window(f"{self.url}{path}"):
            return
        # No Chromium browser here. The page still opens; it is simply a tab
        # this cannot close later, which the page itself handles by saying so.
        logger.info("Opening Scribe in the default browser instead.")
        webbrowser.open(f"{self.url}{path}")

    def open_latest(self) -> None:
        """Show the meeting that finished most recently.

        With a window already open this asks *that page* to move, through the
        service both this and it are talking to, rather than opening a second
        window onto the same Scribe.
        """
        latest = self.status().latest_id
        if not latest:
            raise TrayError("Nothing has been recorded yet.")
        if browser.app_windows():
            self._ask_page_for(latest)
            browser.focus_window()
            return
        self.open_page(f"/?meeting={latest}")

    def _ask_page_for(self, job_id: str) -> None:
        """Leave the meeting id where the page's next poll will collect it."""
        state = getattr(self._app, "state", None)
        if state is not None:
            state.focus = (job_id, time.time())

    def open_folder(self) -> None:
        if self._output_dir is None:
            raise TrayError("There is no recordings folder configured.")
        open_path(self._output_dir)

    def open_log(self) -> None:
        if self._log_path is None:
            raise TrayError("This Scribe is not logging to a file.")
        open_path(self._log_path)

    def toggle_recording(self) -> None:
        """Start recording this machine, or stop what is already running.

        Decided from the live state, not from the label that was clicked:
        Windows holds on to the menu it was last handed, so a stale "Start
        recording" must not start a second recording over the top of one
        already going.
        """
        status = self.status()
        if status.live_id:
            self.stop_recording(status.live_id)
        else:
            self.start_recording()

    def start_recording(self, title: str = "") -> Any:
        """Record this machine, with nothing joining the call."""
        from meetbot.capture.local import capture_available

        ready, detail = capture_available()
        if not ready:
            # Rejected here rather than started and abandoned: a job that dies
            # two seconds in looks like a recording that happened.
            raise TrayError(detail)
        manager = self._manager()
        name = title or time.strftime(TITLE_FORMAT)
        owner = self._owner()
        job = self._on_server(
            lambda: manager.start_local(title=name, owner=owner)
        )
        logger.info("Tray: recording %r as job %s", name, getattr(job, "id", "?"))
        self.refresh()
        return job

    def stop_recording(self, job_id: str) -> None:
        """Stop a running meeting and let its write-up start."""
        manager = self._manager()
        if not self._on_server(lambda: manager.stop(job_id)):
            raise TrayError("That recording had already finished.")
        logger.info("Tray: stop requested for job %s", job_id)
        self.refresh()

    def quit(self) -> None:
        """Let go of the machine properly.

        Three things, in order. The window Scribe opened is closed, because a
        page left behind by a server that has gone is worse than no page. A
        recording in progress is stopped, and its notes are given the same
        grace the service gives them on shutdown - the meeting somebody has
        just ended is the one they are waiting to read. Only then does the
        process go.
        """
        if self._stopping.is_set():
            return
        if self.status().recording:
            self.notify("Finishing the recording, then closing.")
        self._stopping.set()
        # The window goes first: waiting for a write-up with a dead-looking
        # page still on screen is worse than the page simply being gone, and
        # this is the one browser window Scribe is entitled to close.
        with contextlib.suppress(Exception):
            browser.close_windows()
        try:
            self._on_quit()
        finally:
            icon = self._icon
            if icon is not None:
                with contextlib.suppress(Exception):
                    icon.visible = False
                with contextlib.suppress(Exception):
                    icon.stop()

    # -- keeping it current ------------------------------------------------

    def refresh(self) -> None:
        """Bring the tooltip, the mark and the menu up to date.

        The menu is only rebuilt when its text actually changes. Windows keeps
        whatever menu it was last given, and handing it a new one while it is
        open is not something to do every two seconds for a clock that is not
        in it.
        """
        icon = self._icon
        if icon is None:
            return
        status = self.status()

        tip = tooltip(status)
        if tip != self._tip:
            self._tip = tip
            with contextlib.suppress(Exception):
                icon.title = tip

        if status.recording != self._recording_icon:
            image = spark_image(ICON_SIZE, recording=status.recording)
            self._recording_icon = status.recording
            if image is not None:
                with contextlib.suppress(Exception):
                    icon.icon = image

        if status.label != self._shown:
            self._shown = status.label
            with contextlib.suppress(Exception):
                icon.update_menu()

    def _watch(self) -> None:
        while not self._stopping.wait(POLL_S):
            if self._promote_tries > 0:
                # The entry exists only once Windows has been handed the icon,
                # which is usually immediate and occasionally a moment later.
                self._promote_tries = 0 if promote() else self._promote_tries - 1
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - a bad poll must not end polling
                logger.debug("Tray: could not refresh", exc_info=True)

    # -- reaching into the server ------------------------------------------

    def _manager(self) -> Any:
        manager = getattr(getattr(self._app, "state", None), "manager", None)
        if manager is None:
            raise TrayError("Scribe is still starting up.")
        return manager

    def _owner(self) -> str:
        """Whose meeting a recording started from the icon is.

        Nobody signs in at a menu, and an unowned recording is visible to
        admins only - which would hide it from the very account whose machine
        it is. The first admin owns it: the icon only exists in the process
        serving the page, on the machine its owner is sitting at.
        """
        users = getattr(getattr(self._app, "state", None), "users", None)
        if users is None:
            return ""
        with contextlib.suppress(Exception):
            for user in users.list():
                if user.is_admin:
                    return str(user.username)
        return ""

    def _on_server(self, work: Callable[[], Any], timeout: float = ACT_TIMEOUT_S) -> Any:
        """Run *work* on the service's event loop and wait for the answer.

        Jobs are asyncio tasks on that loop; touching them from the icon's
        thread would be starting a task on a loop that is not running here and
        cancelling one from underneath it. Coroutines returned by *work* are
        awaited there too, so ``manager.stop`` can be called as it is written.
        """
        loop = getattr(getattr(self._app, "state", None), "loop", None)
        if loop is None:
            raise TrayError("Scribe is still starting up.")

        async def run() -> Any:
            result = work()
            if inspect.isawaitable(result):
                return await result
            return result

        future = asyncio.run_coroutine_threadsafe(run(), loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            future.cancel()
            raise TrayError("Scribe did not answer. The log has the details.") from exc

    # -- the icon itself ---------------------------------------------------

    def _clicked(self, action: str, _icon: Any, _item: Any) -> None:
        """What pystray calls. Bound per item with :func:`functools.partial`,
        because pystray inspects the argument count of anything with a
        ``__code__`` and refuses a lambda that carries the action as a third.
        """
        self.invoke(action)

    def _entries(self) -> Iterator[Any]:
        """Translate the described menu into pystray's, once per rebuild."""
        from pystray import Menu, MenuItem

        for item in self.items():
            if item.separator:
                yield Menu.SEPARATOR
                continue
            yield MenuItem(
                item.label,
                functools.partial(self._clicked, item.action) if item.action else None,
                default=item.default,
                enabled=item.enabled,
            )

    def build(self) -> Any:
        """The pystray icon, menu and all."""
        from pystray import Icon, Menu

        image = spark_image(ICON_SIZE)
        if image is None:
            raise TrayError("Drawing the icon needs Pillow: pip install pillow")
        self._tip = tooltip(self.status())
        self._shown = self.status().label
        # A callable menu is re-read on every rebuild, which is how the status
        # line and the start/stop label follow what the server is doing.
        return Icon("Scribe", image, self._tip, menu=Menu(self._entries))

    def _ready(self, icon: Any) -> None:
        icon.visible = True
        if promote():
            self._promote_tries = 0
        threading.Thread(target=self._watch, name="scribe-tray", daemon=True).start()
        logger.info("Scribe is in the notification area, by the clock.")
        logger.info(
            "If it is not there, Windows has hidden it: click the '^' beside "
            "the clock and drag Scribe out."
        )

    def run(self) -> None:
        """Show the icon and stay until Quit. Blocks the calling thread."""
        self._icon = self.build()
        self._icon.run(setup=self._ready)


__all__ = [
    "ACT_TIMEOUT_S",
    "ICON_SIZE",
    "Item",
    "NAME_LIMIT",
    "NOTIFY_ICON_KEY",
    "POLL_S",
    "PROMOTE_TRIES",
    "QUIT_TIMEOUT_S",
    "SEPARATOR",
    "ScribeTray",
    "Status",
    "TITLE_FORMAT",
    "TOOLTIP_LIMIT",
    "TrayError",
    "available",
    "background_server",
    "clock",
    "job_name",
    "menu_for",
    "open_path",
    "promote",
    "read_status",
    "stop_server",
    "tooltip",
]
