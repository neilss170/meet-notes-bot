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
from typing import Any
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


def start_detached(port: int, *, command: str = "tray") -> Path:
    """Start the server and let go of it, so it outlives this process.

    Args:
        port: Port to serve on.
        command: The subcommand to run. ``tray`` by default, so a Scribe
            started this way has an icon in the notification area to stop it
            by; ``tray`` falls back to serving without one where the icon
            cannot be shown.

    Returns:
        The log file its output is going to.
    """
    directory = project_dir()
    log_path = directory / SERVER_LOG
    command_line = [
        str(background_python()), "-m", "meetbot", command, "--port", str(port)
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
            command_line,
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


#: The mark, measured off the spiral the page draws: an Archimedean curve in
#: a 24-unit box, ``r = 1.15 + 0.5384 * phi``, leaving the centre straight up
#: and turning for two and a third turns. Held as fractions of the icon's
#: side so one drawing serves 16 pixels and 256, and kept here in numbers
#: rather than as a copied path so a test can check it against the page.
SPIRAL_R0 = 1.15 / 24
SPIRAL_GROWTH = 0.5384 / 24
SPIRAL_STROKE = 2.5 / 24
SPIRAL_TURNS = 2.35

#: How wide the mark sits in its tile, edge to edge of the curve including
#: the stroke. The mark this replaced reached 0.80, and matching it keeps the
#: two the same weight beside other icons on a shelf.
SPIRAL_REACH = 0.80

#: Below this many pixels the gap between neighbouring turns is thinner than
#: one pixel, and the whole mark greys into a smudge. The outer turn is
#: dropped instead: the same curve, cut short. Carrying different artwork at
#: different sizes is what an .ico's separate entries are for.
SPIRAL_SMALL_PX = 32
SPIRAL_SMALL_TURNS = 1.35

#: Pillow draws hard pixels, and a one-pixel stroke with a sub-pixel gap
#: beside it either closes or does not - which is how the first attempt at
#: this produced an orange blob. Drawing eight times over and shrinking gives
#: the anti-aliasing the browser does for free.
_SUPERSAMPLE = 8


def spiral_fit(turns: float) -> float:
    """Scale that puts *turns* turns' outer edge at :data:`SPIRAL_REACH`.

    Fewer turns is a shorter curve, so one fixed scale would draw the small
    sizes' mark *smaller* inside the same tile - which is backwards, because
    a 16-pixel icon needs to fill more of its tile than a 256-pixel one, not
    less. Scaling by the turns actually drawn keeps the mark one size at every
    size and lets only the amount of detail change.
    """
    extent = SPIRAL_R0 + SPIRAL_GROWTH * 2 * math.pi * turns + SPIRAL_STROKE / 2
    return (SPIRAL_REACH / 2) / extent


def spiral_turns(size: int) -> float:
    """How much of the curve there is room to draw at *size* pixels."""
    return SPIRAL_TURNS if size >= SPIRAL_SMALL_PX else SPIRAL_SMALL_TURNS


def spiral_points(size: float, turns: float) -> list[tuple[float, float]]:
    """The spiral, sampled densely enough to draw as a smooth stroke."""
    centre = size / 2
    fit = spiral_fit(turns)
    sweep = turns * 2 * math.pi
    steps = max(256, int(size))
    points = []
    for step in range(steps + 1):
        phi = sweep * step / steps
        radius = (SPIRAL_R0 + SPIRAL_GROWTH * phi) * size * fit
        # Straight up out of the centre, then round, matching the page.
        angle = phi - math.pi / 2
        points.append(
            (centre + math.cos(angle) * radius, centre + math.sin(angle) * radius)
        )
    return points


def spark_image(size: int = 256, *, recording: bool = False) -> Any | None:
    """Draw the mark at *size* pixels. ``None`` if Pillow is not installed.

    The shape is the one the page wears: a spiral, which is Scribe's logo and
    also the thing that turns while Scribe is working. A shortcut carrying
    the stock Python icon looks like somebody's script rather than a thing you
    use, and a shortcut carrying a *different* mark from the window it opens
    is worse - it looks like two products.

    Args:
        size: Side length in pixels. Every measurement is a fraction of it,
            so the same drawing serves the notification area and a 256-pixel
            shortcut - though below :data:`SPIRAL_SMALL_PX` the curve gives up
            its outer turn, which there is no room to draw.
        recording: Draw it as a red tile with the mark cut out of it. The
            notification area gets 16 pixels to work with, which is too few
            for a badge in a corner and too few to tell one dark tile from
            another by the colour of a thin stroke - so the whole tile
            changes. Beside a row of monochrome system glyphs it is the only
            red thing on the taskbar, which is what "you are being recorded"
            should look like.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    ink = (18, 19, 26, 255)
    red = (229, 72, 77, 255)
    spark, back = (ink, red) if recording else ((226, 112, 58, 255), ink)

    big = size * _SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=max(2, round(big * 0.227)), fill=back
    )

    turns = spiral_turns(size)
    points = spiral_points(big, turns)
    width = max(1, round(big * SPIRAL_STROKE * spiral_fit(turns)))
    # A round brush dragged along the curve. draw.line's own joints leave
    # nicks along the outside of a tight bend at this scale; overlapping
    # circles cannot, and they give the round ends for nothing.
    radius = width / 2
    step = max(1.0, width / 6)
    carried = step
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        span = math.hypot(x1 - x0, y1 - y0)
        draw.line([(x0, y0), (x1, y1)], fill=spark, width=width)
        carried += span
        if carried >= step:
            carried = 0.0
            draw.ellipse([x0 - radius, y0 - radius, x0 + radius, y0 + radius], fill=spark)
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=spark)
    return image.resize((size, size), Image.LANCZOS)


#: What goes in the .ico, largest first. 256 is the one File Explorer shows
#: at its biggest zoom, 48 is a Desktop icon, 32 a taskbar button and 16 a
#: list row.
ICO_SIZES = (256, 48, 32, 16)


def write_icon(path: Path) -> bool:
    """Save the mark as a Windows ``.ico``. ``False`` without Pillow.

    Every size is drawn at that size rather than shrunk from the big one.
    Handing Pillow a single 256-pixel image and a list of sizes looks like it
    does the same thing and does not: it resamples, so the 16-pixel entry
    came out as a squashed two-and-a-third-turn spiral - exactly the smudge
    that :data:`SPIRAL_SMALL_PX` exists to avoid, quietly reintroduced at the
    last step. An .ico holds independent images for this reason.
    """
    drawn = [spark_image(size) for size in ICO_SIZES]
    if any(image is None for image in drawn):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    biggest, *rest = drawn
    biggest.save(
        path,
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=rest,
    )
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
            # Signing in should not throw a browser window at you, so this
            # one opens no page - but it does put the icon in the notification
            # area, which is the only thing that says Scribe is running at all
            # when nothing asked for it.
            link = folder / f"{SHORTCUT_NAME}.lnk"
            if _make_shortcut(link, python, f"-m meetbot tray --port {port}", icon):
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
    "spark_image",
    "start_detached",
    "wait_until_running",
    "windows_folder",
    "ICO_SIZES",
    "write_icon",
]
