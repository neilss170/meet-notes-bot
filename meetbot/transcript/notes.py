"""On-disk storage for a meeting's notepad.

The transcript is machine-produced and append-only, so :mod:`store` optimises
for never losing a record. Notes are the opposite: a human types them, the
same file is rewritten every few seconds by an autosave, and the last write
wins. Mixing the two in one module would mean two very different durability
stories under one name, so the notepad gets its own.

Everything lives beside the transcript, in the meeting's own directory:

``notes.md``
    Exactly what the person typed. Never rewritten by the model - if
    enhancement produces something worse, the original is still here.
``enhanced.md``
    The model's output. Regenerating overwrites it; the notes it came from do
    not change.
``notepad.json``
    Which template was used and when things were last written.
``chat.jsonl``
    One record per question asked about the meeting, oldest first.

Writes are atomic - a temporary file in the same directory, then
:func:`os.replace`. An autosave that is interrupted half-way would otherwise
leave a truncated ``notes.md``, which is a good way to lose the notes somebody
took during an hour-long meeting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NOTES_FILE = "notes.md"
ENHANCED_FILE = "enhanced.md"
META_FILE = "notepad.json"
CHAT_FILE = "chat.jsonl"

#: Cap on stored note length. Generous for typing, small enough that a runaway
#: client cannot fill the disk with a single PUT.
MAX_NOTES_CHARS = 200_000

#: Questions kept per conversation. Older turns fall off the front.
MAX_CHAT_TURNS = 200

#: A task line in a write-up: "- [ ] Write the script - Priya (Friday)".
_TASK_LINE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(.*\S)\s*$")

#: Windows will not replace a file that another handle has open - a save
#: landing at the same moment, a request reading the notes, an indexer or a
#: virus scanner - and says "Access is denied". It clears within milliseconds,
#: so a replace that hits it is retried rather than failed. Two threads saving
#: together hit it about once in forty writes.
_REPLACE_ATTEMPTS = 10
_REPLACE_BACKOFF_S = 0.02


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step.

    The temporary file is created in the destination directory so the final
    :func:`os.replace` stays on one filesystem, where it is atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(handle.name, path)
                break
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    except BaseException:
        # Leaving .notes.md.xxxx.tmp files behind would accumulate in a
        # directory the user browses, so clean up on any failure path.
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


_LOCKS_GUARD = threading.Lock()
_DIRECTORY_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True)
class Task:
    """One action item in a write-up, and the line it is on."""

    line: int
    text: str
    done: bool


def tasks_in(markdown: str) -> list[Task]:
    """Every task line in ``markdown``, in order, ticked or not."""
    found = []
    for index, line in enumerate(markdown.splitlines()):
        match = _TASK_LINE.match(line)
        if match:
            found.append(Task(index, match.group(4).strip(), match.group(2) != " "))
    return found


def _lock_for(directory: Path) -> threading.RLock:
    """The one lock for a meeting directory, however many Notepads point at it.

    The service makes a fresh :class:`Notepad` per request, so the lock cannot
    live on the instance. Without it an autosave and the save that "Enhance"
    sends first both rewrite ``notepad.json`` from the same stale read, and
    one of the two updates is lost.
    """
    key = os.path.normcase(os.path.abspath(directory))
    with _LOCKS_GUARD:
        return _DIRECTORY_LOCKS.setdefault(key, threading.RLock())


class ChatLog:
    """Questions and their answers, one JSON record per line, oldest first.

    The same shape serves the questions asked about one meeting and the ones
    asked across all of them. Appends rather than rewriting, and compacts
    only once the history has grown past its cap.
    """

    def __init__(self, path: Path, *, max_turns: int | None = None) -> None:
        self.path = Path(path)
        self._max_turns = max_turns

    @property
    def max_turns(self) -> int:
        return self._max_turns if self._max_turns is not None else MAX_CHAT_TURNS

    def read(self) -> list[dict[str, Any]]:
        """Every turn, oldest first.

        Malformed lines are skipped rather than aborting the read, for the
        same reason the transcript reader does it.
        """
        if not self.path.exists():
            return []
        turns: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and "question" in record:
                        turns.append(record)
        except OSError:
            logger.debug("Could not read chat at %s", self.path, exc_info=True)
            return []
        return turns

    def append(self, question: str, answer: str, **extra: Any) -> dict[str, Any]:
        """Record one question, its answer, and anything kept alongside them.

        Args:
            question: What was asked.
            answer: What came back.
            **extra: JSON-serialisable fields stored with the turn - the
                citations behind an answer, for instance.
        """
        record = {
            "question": question,
            "answer": answer,
            **extra,
            "asked_at": _utc_now_iso(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(self.path.parent):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()

            turns = self.read()
            if len(turns) > self.max_turns:
                kept = turns[-self.max_turns :]
                atomic_write(
                    self.path,
                    "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in kept),
                )
        return record

    def clear(self) -> None:
        """Forget every turn."""
        with contextlib.suppress(OSError):
            self.path.unlink()


@dataclass(frozen=True)
class Notepad:
    """The notepad files for one meeting directory.

    Reads never raise for a missing or unreadable file: a meeting recorded
    before the notepad existed simply has no notes, and that is not an error
    the UI should have to handle.
    """

    directory: Path

    # -- raw notes ---------------------------------------------------------

    @property
    def notes_path(self) -> Path:
        return self.directory / NOTES_FILE

    @property
    def enhanced_path(self) -> Path:
        return self.directory / ENHANCED_FILE

    def read_notes(self) -> str:
        """Whatever the person typed, or ``""``."""
        return self._read_text(self.notes_path)

    def write_notes(self, text: str) -> None:
        """Replace the typed notes.

        Args:
            text: The full note body. Truncated to :data:`MAX_NOTES_CHARS`.
        """
        with _lock_for(self.directory):
            atomic_write(self.notes_path, text[:MAX_NOTES_CHARS])
            self.update_meta(notes_updated_at=_utc_now_iso())

    def read_enhanced(self) -> str:
        """The model's notes, or ``""`` if enhancement has not been run."""
        return self._read_text(self.enhanced_path)

    def write_enhanced(self, text: str, *, template: str = "") -> None:
        """Replace the enhanced notes and record how they were produced."""
        with _lock_for(self.directory):
            atomic_write(self.enhanced_path, text)
            # A fresh write-up is all the model's again, so the lines edited
            # in the last one no longer describe anything in it.
            fields: dict[str, Any] = {"enhanced_at": _utc_now_iso(), "edited_lines": []}
            if template:
                fields["template"] = template
            self.update_meta(**fields)

    def write_edited_enhanced(self, text: str, edited_lines: list[str]) -> None:
        """Keep the person's edits to the enhanced notes.

        Args:
            text: The whole document as it now reads.
            edited_lines: The lines the person wrote or rewrote, which the
                page shows in their ink rather than the model's. Only lines
                still in ``text`` are kept.
        """
        present = {line.strip() for line in text.splitlines()}
        kept = [line for line in dict.fromkeys(edited_lines) if line.strip() in present]
        with _lock_for(self.directory):
            atomic_write(self.enhanced_path, text[:MAX_NOTES_CHARS * 4])
            self.update_meta(edited_lines=kept, enhanced_edited_at=_utc_now_iso())

    def set_task(self, line: int, text: str, done: bool) -> bool:
        """Tick or untick the task on ``line`` of the write-up.

        ``text`` is what the caller saw on that line. The home page lists
        tasks from many meetings and may be minutes old; if the write-up has
        been edited since and the line now says something else, nothing is
        changed and ``False`` comes back, rather than ticking whatever task
        moved into that place.
        """
        with _lock_for(self.directory):
            lines = self.read_enhanced().splitlines(keepends=True)
            if not 0 <= line < len(lines):
                return False
            match = _TASK_LINE.match(lines[line].rstrip("\r\n"))
            if not match or match.group(4).strip() != text.strip():
                return False
            was = lines[line].rstrip("\r\n")
            now = f"{match.group(1)}{'x' if done else ' '}{match.group(3)}{match.group(4)}"
            lines[line] = now + lines[line][len(was):]
            # A line the person wrote stays theirs after it is ticked.
            mine = [now if kept.strip() == was.strip() else kept for kept in self.edited_lines]
            self.write_edited_enhanced("".join(lines), mine)
            return True

    @property
    def edited_lines(self) -> list[str]:
        """Lines of the enhanced notes the person wrote themselves."""
        value = self.read_meta().get("edited_lines")
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def has_notes(self) -> bool:
        """Whether anything has been typed.

        Tests the file size rather than the content, so the meetings list can
        show a badge per meeting without reading every notes file.
        """
        try:
            return self.notes_path.stat().st_size > 0
        except OSError:
            return False

    @property
    def has_enhanced(self) -> bool:
        return self.enhanced_path.exists()

    # -- metadata ----------------------------------------------------------

    def read_meta(self) -> dict[str, Any]:
        """Notepad metadata, or ``{}``."""
        raw = self._read_text(self.directory / META_FILE)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring unreadable %s in %s", META_FILE, self.directory)
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def update_meta(self, **fields: Any) -> dict[str, Any]:
        """Merge ``fields`` into the metadata and persist it."""
        with _lock_for(self.directory):
            meta = self.read_meta()
            meta.update(fields)
            atomic_write(self.directory / META_FILE, json.dumps(meta, indent=2))
            return meta

    @property
    def template(self) -> str:
        """The template last used, or ``""`` if none has been chosen."""
        value = self.read_meta().get("template")
        return value if isinstance(value, str) else ""

    # -- chat --------------------------------------------------------------

    @property
    def chat(self) -> ChatLog:
        return ChatLog(self.directory / CHAT_FILE)

    def read_chat(self) -> list[dict[str, Any]]:
        """Every question asked about this meeting, oldest first."""
        return self.chat.read()

    def append_chat(self, question: str, answer: str, **extra: Any) -> dict[str, Any]:
        """Record one question and its answer. See :meth:`ChatLog.append`."""
        return self.chat.append(question, answer, **extra)

    def clear_chat(self) -> None:
        """Forget every question asked about this meeting."""
        self.chat.clear()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""


__all__ = [
    "CHAT_FILE",
    "ENHANCED_FILE",
    "MAX_CHAT_TURNS",
    "MAX_NOTES_CHARS",
    "META_FILE",
    "NOTES_FILE",
    "ChatLog",
    "Notepad",
    "atomic_write",
]
