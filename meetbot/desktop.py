"""Starting Scribe without a terminal.

A notetaker you have to remember to start from a command prompt is a notetaker
that is not running when the call begins, which is the only moment it is worth
anything. So there is an icon, and there is an option to have it running
already when you sign in.

Three small Windows details make this less trivial than it sounds:

**The Desktop is not always ``%USERPROFILE%\\Desktop``.** A machine signed
into OneDrive has its Desktop redirected, and a shortcut written to the
un-redirected path simply never appears. Windows is asked where the folder
actually is rather than told.

**A shortcut needs an explicit working directory.** Started without one, the
server runs in ``system32``: the ``.env`` search upwards finds nothing, and
any recording lands somewhere nobody would think to look.

**A console window is not part of the product.** ``pythonw.exe`` runs the same
interpreter with no window attached, so the server's output is redirected to a
log file beside the recordings instead - which is also where the admin
password printed on the very first run can be read back.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

logger = logging.getLogger(__name__)

#: What the shortcuts are called, on the Desktop and in the Startup folder.
SHORTCUT_NAME = "Scribe"

#: Where a windowless server's output goes, in the project directory.
SERVER_LOG = "scribe-server.log"

#: Generated beside it, and pointed at by both shortcuts.
ICON_FILE = "scribe.ico"

#: How long :func:`wait_until_running` gives a server it has just started.
#: Generous: the first start of the day loads Python, FastAPI and Playwright's
#: bindings on a cold file cache.
START_TIMEOUT_S = 30.0

#: How long a single "is it up yet?" probe waits.
PROBE_TIMEOUT_S = 1.5

#: Asked of Windows rather than assembled from environment variables.
DESKTOP_FOLDER = "DesktopDirectory"
STARTUP_FOLDER = "Startup"

#: Written with the values in environment variables rather than interpolated
#: into the script, so a path with a quote or an ampersand in it cannot end
#: the argument early.
_SHORTCUT_SCRIPT = """
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:SCRIBE_LINK)
$link.TargetPath = $env:SCRIBE_TARGET
$link.Arguments = $env:SCRIBE_ARGS
$link.WorkingDirectory = $env:SCRIBE_CWD
$link.Description = 'Scribe - meeting notes'
if ($env:SCRIBE_ICON) { $link.IconLocation = $env:SCRIBE_ICON }
$link.Save()
"""


def available() -> tuple[bool, str]:
    """Whether shortcuts can be made here.

    Windows only so far, and it says so rather than raising: every other
    platform still has ``meetbot serve``, which is what the shortcut runs.
    """
    if sys.platform != "win32":
        return False, (
            "Shortcuts are Windows-only for now. Run 'meetbot serve --open', "
            "or add it to your session's startup applications."
        )
    return True, "A Desktop icon, and optionally a Startup entry."


def project_dir() -> Path:
    """The directory Scribe should run in: the one holding ``.env``."""
    root = Path(__file__).resolve().parent.parent
    if (root / ".env").exists() or (root / "pyproject.toml").exists():
        return root
    return Path.cwd()


def is_running(port: int, *, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Whether *Scribe* is answering on this port.

    The sign-in page is fetched rather than the port merely connected to: a
    bare TCP check would mistake anything else listening on 8080 for Scribe
    and then hand somebody their browser pointed at it.
    """
    try:
        with urlopen(f"http://127.0.0.1:{port}/login", timeout=timeout) as response:
            head = response.read(4096).decode("utf-8", "replace")
    except (URLError, OSError, ValueError):
        return False
    return "Scribe" in head


def wait_until_running(port: int, timeout: float = START_TIMEOUT_S) -> bool:
    """Block until the server answers, or the patience runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(port):
            return True
        time.sleep(0.4)
    return False


def background_python() -> Path:
    """``pythonw.exe`` where it exists, so a launch leaves no console behind."""
    executable = Path(sys.executable)
    if sys.platform == "win32":
        windowless = executable.with_name("pythonw.exe")
        if windowless.exists():
            return windowless
    return executable


def start_detached(port: int) -> Path:
    """Start the server and let go of it, so it outlives this process.

    Returns:
        The log file its output is going to.
    """
    directory = project_dir()
    log_path = directory / SERVER_LOG
    command = [
        str(background_python()), "-m", "meetbot", "serve", "--port", str(port)
    ]
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    try:
        extra: dict[str, object] = {}
        if sys.platform == "win32":
            # Detached, and in its own process group: closing the window that
            # started it - or Ctrl+C in that window - must not take the
            # server, and a meeting, down with it.
            extra["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            extra["start_new_session"] = True
        subprocess.Popen(  # noqa: S603 - fixed command, no shell
            command,
            cwd=str(directory),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **extra,  # type: ignore[arg-type]
        )
    finally:
        # The child holds its own duplicate of the handle.
        handle.close()
    return log_path


def write_icon(path: Path) -> bool:
    """Draw the spark as an icon file. ``False`` if Pillow is not installed.

    The mark is the one the page uses - four strokes through a centre, in the
    same orange - because a shortcut carrying the stock Python icon looks like
    somebody's script rather than a thing you use.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    size = 256
    spark = (226, 112, 58, 255)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=58, fill=(18, 19, 26, 255))

    centre = size / 2
    reach = size * 0.30
    for degrees in (0, 45, 90, 135):
        angle = math.radians(degrees)
        dx, dy = math.cos(angle) * reach, math.sin(angle) * reach
        draw.line(
            [(centre - dx, centre - dy), (centre + dx, centre + dy)],
            fill=spark,
            width=15,
        )
    draw.ellipse(
        [centre - 21, centre - 21, centre + 21, centre + 21], fill=(18, 19, 26, 255)
    )
    draw.ellipse([centre - 11, centre - 11, centre + 11, centre + 11], fill=spark)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
    return True


def windows_folder(name: str) -> Path | None:
    """Where Windows says a known folder is, or ``None``.

    Asked rather than assembled: this machine's Desktop lives under OneDrive,
    and a shortcut written to ``%USERPROFILE%\\Desktop`` would be filed
    somewhere the person never looks.
    """
    try:
        finished = subprocess.run(  # noqa: S603 - fixed command, no shell
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"[Environment]::GetFolderPath('{name}')",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not ask Windows for %s", name, exc_info=True)
        return None
    path = finished.stdout.strip()
    return Path(path) if path else None


def _make_shortcut(link: Path, target: Path, arguments: str, icon: Path | None) -> bool:
    environment = {
        **os.environ,
        "SCRIBE_LINK": str(link),
        "SCRIBE_TARGET": str(target),
        "SCRIBE_ARGS": arguments,
        "SCRIBE_CWD": str(project_dir()),
        "SCRIBE_ICON": str(icon) if icon else "",
    }
    try:
        finished = subprocess.run(  # noqa: S603 - fixed command, no shell
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", _SHORTCUT_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not create %s: %s", link.name, exc)
        return False
    if finished.returncode != 0:
        logger.warning(
            "Could not create %s: %s", link.name, finished.stderr.strip()[:200]
        )
        return False
    return link.exists()


def install(*, port: int = 8080, startup: bool = False) -> list[Path]:
    """Put Scribe on the Desktop, and optionally into Startup.

    Args:
        port: The port both shortcuts use.
        startup: Also start the server when this account signs in.

    Returns:
        The shortcut files that now exist.
    """
    icon_path = project_dir() / ICON_FILE
    icon = icon_path if write_icon(icon_path) else None
    python = background_python()
    made: list[Path] = []

    desktop = windows_folder(DESKTOP_FOLDER)
    if desktop is not None:
        # The icon runs `launch`, not `serve`: clicking it while Scribe is
        # already running should open the page, not fail to bind the port.
        link = desktop / f"{SHORTCUT_NAME}.lnk"
        if _make_shortcut(link, python, f"-m meetbot launch --port {port}", icon):
            made.append(link)

    if startup:
        folder = windows_folder(STARTUP_FOLDER)
        if folder is not None:
            # Signing in should not throw a browser window at you, so this one
            # serves and says nothing.
            link = folder / f"{SHORTCUT_NAME}.lnk"
            if _make_shortcut(link, python, f"-m meetbot serve --port {port}", icon):
                made.append(link)
    return made


def installed() -> list[Path]:
    """The shortcuts that currently exist."""
    found: list[Path] = []
    for name in (DESKTOP_FOLDER, STARTUP_FOLDER):
        folder = windows_folder(name)
        if folder is None:
            continue
        link = folder / f"{SHORTCUT_NAME}.lnk"
        if link.exists():
            found.append(link)
    return found


def remove() -> list[Path]:
    """Take both shortcuts away. Returns the ones that were there."""
    gone: list[Path] = []
    for link in installed():
        try:
            link.unlink()
        except OSError as exc:
            logger.warning("Could not remove %s: %s", link, exc)
            continue
        gone.append(link)
    return gone


__all__ = [
    "ICON_FILE",
    "PROBE_TIMEOUT_S",
    "SERVER_LOG",
    "SHORTCUT_NAME",
    "START_TIMEOUT_S",
    "available",
    "background_python",
    "install",
    "installed",
    "is_running",
    "project_dir",
    "remove",
    "start_detached",
    "wait_until_running",
    "windows_folder",
    "write_icon",
]
