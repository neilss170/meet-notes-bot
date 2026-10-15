"""The server, as the desktop app runs it.

``scribe-server.exe`` is this module frozen. The Electron shell starts it with
a port and a data directory, waits for ``/health``, and talks to it over HTTP
from then on; it never imports anything here, which is what lets the shell
stay a shell. From a checkout the same thing runs as::

    python -m meetbot.server_main --port 8765 --data-dir %APPDATA%\\Scribe

Three things differ from ``meetbot serve``, and each is a consequence of
having no terminal and a parent process:

**Everything user-specific comes from the data directory** - see
:mod:`meetbot.appdata`. Settings are read from its ``.env`` and nowhere else,
and the recordings and account paths are fixed to it, so a stray ``.env`` in
whatever directory Windows happened to start the process in cannot redirect
somebody's meetings.

**It refuses nothing at startup.** A fresh install has no API keys, and a
server that exited over that would be an app whose only window is an error.
It starts, and the page's readiness banner says what is missing and where.

**It does not outlive its parent.** If the shell dies without asking - killed
from Task Manager, a crash - the server notices within a couple of seconds
and shuts down the way Quit would, finishing any recording on the way out.
An orphaned recorder holding the microphone with no window and no icon is
the worst state this program could be left in.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

#: The environment variable the shell passes its control token in. Not an
#: argument: a command line is visible to anything that lists processes.
TOKEN_ENV = "SCRIBE_CONTROL_TOKEN"

#: How often the parent is checked on.
PARENT_POLL_S = 2.0

#: Notes still being written get this long at shutdown. Longer than the
#: terminal's: quitting from the icon mid-call is exactly when they are wanted,
#: and the shell waits for the process rather than for a fixed time.
DESKTOP_NOTES_GRACE_S = 120.0

logger = logging.getLogger("meetbot.server")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scribe-server", description="Scribe's local server, for the desktop app."
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="Where settings, accounts and recordings live (default: %%APPDATA%%\\Scribe).",
    )
    parser.add_argument(
        "--parent-pid", type=int, default=0,
        help="Shut down gracefully when this process exits.",
    )
    parser.add_argument(
        "--mic-shortcut", default="",
        help="The shortcut the shell registered for muting, to show in the page.",
    )
    parser.add_argument(
        "--shell-notifications", action="store_true",
        help="The shell raises call notifications; do not raise them here.",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def _resource(name: str) -> Path:
    """A file shipped alongside the code, frozen or not."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / name


def _quiet_streams() -> None:
    """A windowless executable has no stdout or stderr at all.

    Anything that writes to them - a stray print, a library's warning -
    would raise instead. They go nowhere; the log file is where output lives.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def _alive(pid: int) -> bool:
    """Whether a process is still running. Windows and POSIX both."""
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            # WAIT_TIMEOUT (0x102) means it has not exited.
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    # A child of ours that has exited stays in the process table as a zombie
    # until it is collected, and os.kill() reports a zombie as alive. Collect
    # it if it is ours; the shell is not, and is asked the ordinary way.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(pid: int, on_gone: Callable[[], None], *, poll_s: float = PARENT_POLL_S) -> threading.Thread:
    """Call ``on_gone`` once the parent process has exited."""

    def run() -> None:
        while _alive(pid):
            time.sleep(poll_s)
        logger.warning("The desktop app (pid %d) has gone; shutting down.", pid)
        on_gone()

    thread = threading.Thread(target=run, name="parent-watch", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    _quiet_streams()
    args = _parse(argv)

    from meetbot.appdata import DataDir, default_data_dir, migrate
    from meetbot.logging_setup import configure_logging

    data = DataDir(args.data_dir or default_data_dir()).create()
    configure_logging(args.log_level.upper(), data.log_file)
    logger.info("Scribe server starting: data in %s, port %d", data.root, args.port)

    try:
        migrate(data)
    except Exception:  # noqa: BLE001 - a failed copy must not stop the app
        logger.exception("Could not copy the earlier Scribe's data; starting fresh")
    data.seed_env(_resource(".env.example"))

    from meetbot.config import ConfigError, load_config

    try:
        config = load_config(env_file=data.env_file)
    except ConfigError as exc:
        # Unreadable rather than incomplete - a provider name that does not
        # exist, say. Still worth starting: the page is where it gets fixed.
        logger.error("Settings in %s could not be read: %s", data.env_file, exc)
        from meetbot.config import Config

        config = Config()
    config = config.override(output_dir=data.recordings, service_state_dir=data.state)
    try:
        config.validate(require_meeting=False)
    except ConfigError as exc:
        logger.warning("Scribe is not fully set up yet (%s): %s", data.env_file, exc)

    import uvicorn

    from meetbot.service.app import create_app

    url = f"http://127.0.0.1:{args.port}"
    app = create_app(
        config,
        public_url=url,
        control_token=os.environ.pop(TOKEN_ENV, ""),
        shell_notifications=args.shell_notifications,
        notes_grace_s=DESKTOP_NOTES_GRACE_S,
    )
    app.state.mic_shortcut = args.mic_shortcut
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level.lower(),
            log_config=None,
        )
    )

    def request_exit() -> None:
        server.should_exit = True

    app.state.request_exit = request_exit
    if args.parent_pid:
        watch_parent(args.parent_pid, request_exit)

    server.run()
    if not getattr(server, "started", False):
        logger.error("Scribe could not listen on %s:%d.", args.host, args.port)
        return 1
    logger.info("Scribe server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
