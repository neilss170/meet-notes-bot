"""Where an installed Scribe keeps everything that belongs to its user.

Run from a checkout, Scribe keeps its things wherever the checkout put them:
``.env`` beside the code, recordings in ``./recordings``, accounts and the
session key in ``~/.meetbot``. That is fine for a developer and wrong for an
installed program, whose install folder is replaced on every upgrade and may
not even be writable. So the desktop app runs the server with a data
directory - ``%APPDATA%\\Scribe`` on Windows - and everything user-specific
lives under it:

``.env``
    Settings and API keys. Created from the bundled template on first run,
    so there is always one file to open and fill in.
``state\\``
    The account database, the session-signing key, call-alert choices and
    the cross-meeting chat history - what ``~/.meetbot`` held.
``recordings\\``
    One directory per meeting, exactly as before.
``logs\\``
    The server's log. Where to look when the app says it could not start.

**Moving in.** The first time a data directory is used, whatever an earlier
from-source Scribe left behind is *copied* into it - never moved, and the
originals are never deleted. A copy can be checked and thrown away; a move
that went wrong halfway through is somebody's meetings gone. A marker file
records what was copied from where, so it happens once.

Finding the earlier Scribe: ``~/.meetbot`` is fixed, but the checkout - and
so the ``.env`` and the recordings - could be anywhere. The shortcuts
``meetbot shortcut`` made know, because a shortcut has to name its working
directory. By the time the server first runs, the installer has replaced the
Desktop one and the shell has moved the Startup one into ``retired\`` here,
so that is looked in too; failing all of them, ``SCRIBE_LEGACY_DIR`` can say.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Written once the first-run copy has been done, and what it copied.
MIGRATION_MARKER = "migration.json"

#: The from-source state directory, and the parts of it not worth carrying:
#: the bot's browser profile is large, holds a Google session tied to that
#: path, and stays where it is - ``.env`` still points at it - and the
#: per-run profile clones are scratch.
LEGACY_STATE_DIR = Path("~/.meetbot")
LEGACY_STATE_SKIP = frozenset({"chrome-profile", "profiles"})

#: Where the shell moves the from-source Startup shortcut it retires.
RETIRED_DIR = "retired"

#: The log file inside ``logs``.
SERVER_LOG_NAME = "scribe-server.log"

_SHORTCUT_DIR_SCRIPT = (
    "$s = New-Object -ComObject WScript.Shell; "
    "$s.CreateShortcut($env:SCRIBE_LINK).WorkingDirectory"
)


def default_data_dir() -> Path:
    """The per-user data directory the desktop app uses.

    ``%APPDATA%\\Scribe`` on Windows - roaming, because accounts and settings
    are the user's rather than the machine's. The equivalent elsewhere, so a
    macOS shell can pass the same flag without anything here changing.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Scribe"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Scribe"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "scribe"


@dataclass(frozen=True)
class DataDir:
    """The layout of one data directory. Nothing is created until :meth:`create`."""

    root: Path

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def recordings(self) -> Path:
        return self.root / "recordings"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / SERVER_LOG_NAME

    @property
    def marker(self) -> Path:
        return self.root / MIGRATION_MARKER

    def create(self) -> "DataDir":
        for path in (self.root, self.state, self.recordings, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def seed_env(self, template: Path | None) -> bool:
        """Give a new data directory a settings file to fill in.

        Returns whether one was written. An existing ``.env`` is never
        touched - it is the one file here somebody has typed into.
        """
        if self.env_file.exists() or template is None or not template.exists():
            return False
        shutil.copy2(template, self.env_file)
        logger.info("Wrote a settings file to fill in: %s", self.env_file)
        return True


# -- finding an earlier Scribe -------------------------------------------------


def _shortcut_dirs(data_root: Path | None = None) -> list[Path]:
    """Working directories of the shortcuts ``meetbot shortcut`` made."""
    if sys.platform != "win32":
        return []
    from meetbot.desktop import (
        DESKTOP_FOLDER,
        SHORTCUT_NAME,
        STARTUP_FOLDER,
        windows_folder,
    )

    links: list[Path] = []
    if data_root is not None:
        links.extend(sorted((data_root / RETIRED_DIR).glob("*.lnk")))
    for folder in (DESKTOP_FOLDER, STARTUP_FOLDER):
        where = windows_folder(folder)
        if where is not None:
            links.append(where / f"{SHORTCUT_NAME}.lnk")

    found: list[Path] = []
    for link in links:
        if not link.exists():
            continue
        try:
            finished = subprocess.run(  # noqa: S603 - fixed command, no shell
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 _SHORTCUT_DIR_SCRIPT],
                env={**os.environ, "SCRIBE_LINK": str(link)},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        directory = finished.stdout.strip()
        if directory:
            found.append(Path(directory))
    return found


def legacy_project_dirs(
    extra: Iterable[Path] = (), *, data_root: Path | None = None
) -> list[Path]:
    """Checkouts an earlier Scribe ran from, best guess first.

    Only directories that look like one - holding a ``.env`` or a
    ``recordings`` folder - are returned.
    """
    candidates: list[Path] = list(extra)
    named = os.environ.get("SCRIBE_LEGACY_DIR", "").strip()
    if named:
        candidates.insert(0, Path(named))
    candidates.extend(_shortcut_dirs(data_root))
    seen: set[str] = set()
    found: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if (resolved / ".env").is_file() or (resolved / "recordings").is_dir():
            found.append(resolved)
    return found


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


# -- copying it in -------------------------------------------------------------


def _copy_tree(source: Path, target: Path, *, skip: frozenset[str] = frozenset()) -> int:
    """Copy what ``target`` does not already have. Returns files copied."""
    copied = 0
    for entry in source.iterdir():
        if entry.name in skip:
            continue
        destination = target / entry.name
        try:
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                copied += _copy_tree(entry, destination)
                # After the contents, or copying them would bump it again. A
                # meeting restored from disk is dated by its folder's time, so
                # a fresh one would file every old meeting under today.
                shutil.copystat(entry, destination)
            elif not destination.exists():
                shutil.copy2(entry, destination)
                copied += 1
        except OSError as exc:
            # One unreadable file must not cost the rest; it stays where it
            # was, and the log says which.
            logger.warning("Could not copy %s: %s", entry, exc)
    return copied


def migrate(
    data: DataDir,
    *,
    project_dirs: Iterable[Path] | None = None,
    legacy_state: Path | None = None,
) -> dict[str, Any] | None:
    """Copy an earlier from-source Scribe into ``data``, once.

    Args:
        data: The (created) data directory to fill.
        project_dirs: Checkouts to look in. Found automatically when omitted.
        legacy_state: The old state directory. ``~/.meetbot`` - or whatever
            the old ``.env`` set ``SERVICE_STATE_DIR`` to - when omitted.

    Returns:
        What was copied, as written to the marker, or ``None`` when the
        directory had already been through this.
    """
    if data.marker.exists():
        return None
    projects = (
        list(project_dirs)
        if project_dirs is not None
        else legacy_project_dirs(data_root=data.root)
    )
    project = projects[0] if projects else None
    old_env = _read_env(project / ".env") if project else {}

    record: dict[str, Any] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project": str(project) if project else None,
        "copied": {},
        "note": "Copies. The originals were left exactly where they were.",
    }

    if project is not None and (project / ".env").is_file() and not data.env_file.exists():
        shutil.copy2(project / ".env", data.env_file)
        record["copied"]["settings"] = str(project / ".env")

    if legacy_state is None:
        legacy_state = Path(old_env.get("SERVICE_STATE_DIR") or LEGACY_STATE_DIR)
        if not legacy_state.is_absolute() and project is not None and not str(
            legacy_state
        ).startswith("~"):
            legacy_state = project / legacy_state
    legacy_state = legacy_state.expanduser()
    if legacy_state.is_dir() and legacy_state.resolve() != data.state.resolve():
        count = _copy_tree(legacy_state, data.state, skip=LEGACY_STATE_SKIP)
        record["copied"]["state"] = {"from": str(legacy_state), "files": count}

    if project is not None:
        recordings = Path(old_env.get("OUTPUT_DIR") or "recordings")
        if not recordings.is_absolute():
            recordings = project / recordings
        if recordings.is_dir() and recordings.resolve() != data.recordings.resolve():
            count = _copy_tree(recordings, data.recordings)
            record["copied"]["recordings"] = {"from": str(recordings), "files": count}

    data.marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if record["copied"]:
        logger.info("Copied an earlier Scribe into %s: %s", data.root, record["copied"])
    return record


__all__ = [
    "DataDir",
    "LEGACY_STATE_DIR",
    "MIGRATION_MARKER",
    "default_data_dir",
    "legacy_project_dirs",
    "migrate",
]
