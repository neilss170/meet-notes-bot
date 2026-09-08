"""Private per-run copies of the signed-in Chromium profile.

Chromium will not share one profile directory between two live browsers, and
it does not fail loudly when asked to: the second instance silently gets a
*blank* profile. A bot sent to a second concurrent meeting therefore joins
signed-out, and Meet increasingly refuses anonymous guests outright - so the
symptom is "can't join this video call", which points at the join selectors
rather than at the real cause.

Each run gets its own copy instead. The master profile - the one
``meetbot login`` signs in - is then only ever read, never opened by a
meeting, which has a second benefit: a run that crashes or is killed mid-call
cannot corrupt the signed-in session every future meeting depends on.

The cost is a directory copy per join. Almost all of a Chrome profile is
regenerable cache (49 MB of 61 MB on the profile this was written against),
so the caches are skipped and what remains copies in well under a second.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: Profile subdirectories holding only regenerable cache. Chromium rebuilds
#: them on demand, and skipping them is most of why the copy is fast enough
#: to sit in the join path.
SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GPUPersistentCache",
        "GrShaderCache",
        "ShaderCache",
        "DawnWebGPUCache",
        "DawnGraphiteCache",
        "Service Worker",
        "Crashpad",
        "Safe Browsing",
    }
)

#: Lock files Chromium uses to claim a profile. Copying them would carry a
#: dead process's claim into the fresh directory.
_LOCK_PREFIX: Final[str] = "Singleton"

#: Prefix every clone directory carries, so stale ones can be recognised.
_CLONE_PREFIX: Final[str] = "meetbot-profile-"

#: Pause between delete attempts, for handles Windows has not released yet.
_RETRY_DELAY_S: Final[float] = 0.25


def _ignored(_directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in SKIP_DIRS or n.startswith(_LOCK_PREFIX)}


def clone_profile(master: Path, parent: Path | None = None) -> Path:
    """Copy ``master`` into a fresh directory this run can own outright.

    Args:
        master: The signed-in profile written by ``meetbot login``.
        parent: Where to place the copy. Defaults to the system temp area.

    Returns:
        Path to the copy, to be handed to ``launch_persistent_context`` and
        passed to :func:`discard_profile` when the run ends.

    Raises:
        FileNotFoundError: If ``master`` does not exist. Callers should treat
            this as "not signed in yet" rather than a crash.
    """
    master = master.expanduser()
    if not master.is_dir():
        raise FileNotFoundError(
            f"No signed-in Chromium profile at {master}. "
            "Run 'python -m meetbot login' first."
        )
    destination = Path(tempfile.mkdtemp(prefix=_CLONE_PREFIX, dir=parent))
    shutil.copytree(master, destination, ignore=_ignored, dirs_exist_ok=True)
    logger.debug("Cloned the signed-in profile %s -> %s", master, destination)
    return destination


def discard_profile(path: Path | None, attempts: int = 4) -> None:
    """Delete a clone made by :func:`clone_profile`.

    Never raises: a meeting that has already produced its transcript must not
    fail during cleanup.

    Windows keeps profile files open for a moment after the browser exits, so
    a single attempt usually leaves most of the directory behind - 63 clones
    and 37 MB accumulated over one afternoon of testing. Retrying briefly
    clears them, and :func:`sweep_stale_profiles` catches whatever a crash
    skipped entirely.
    """
    if path is None:
        return
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        if attempt < attempts - 1:
            time.sleep(_RETRY_DELAY_S)
    logger.debug("Could not remove the profile clone at %s", path)


def sweep_stale_profiles(older_than_s: float = 3600.0, parent: Path | None = None) -> int:
    """Remove clones left behind by runs that never cleaned up after themselves.

    A crash, a kill, or a machine losing power skips :func:`discard_profile`
    entirely, so its retries never happen. Age-based rather than
    unconditional, so a clone belonging to a meeting running right now is
    left alone.

    Returns:
        How many directories were removed.
    """
    root = parent or Path(tempfile.gettempdir())
    cutoff = time.time() - older_than_s
    removed = 0
    for candidate in root.glob(f"{_CLONE_PREFIX}*"):
        if not candidate.is_dir():
            continue
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed += not candidate.exists()
    if removed:
        logger.info("Removed %d stale profile clone(s) from %s", removed, root)
    return removed


__all__ = [
    "SKIP_DIRS",
    "clone_profile",
    "discard_profile",
    "sweep_stale_profiles",
]
