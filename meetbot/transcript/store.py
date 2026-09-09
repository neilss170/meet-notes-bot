"""Crash-durable JSONL transcript storage.

The store is append-only and flushed after every record, so a transcript
survives the bot being killed mid-meeting. Each line is one JSON object with a
``type`` discriminator:

``meta``
    A single header record written when the meeting starts.
``utterance``
    One finalised transcribed utterance.
``event``
    A lifecycle marker (joined, participant roster change, stream error, ...).

Readers tolerate truncated or corrupt trailing lines - the common failure mode
after a hard kill is a half-written final line, and losing that line should
never cost you the rest of the meeting.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

logger = logging.getLogger(__name__)

RecordType = Literal["meta", "utterance", "event"]

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Utterance:
    """One finalised, speaker-attributed chunk of speech.

    Attributes:
        speaker: Display label, e.g. ``"Speaker 0"`` from Deepgram diarization
            or a real participant name under per-participant capture.
        text: The transcribed text, already punctuated by Deepgram.
        start: Seconds from the start of capture.
        end: Seconds from the start of capture.
        channel: Audio channel the utterance came from - ``"mixed"`` for the
            single mixed stream, otherwise a per-participant channel id.
        confidence: Deepgram's confidence in ``[0, 1]``, when reported.
        wall_clock: Absolute UTC timestamp the utterance was finalised.
    """

    speaker: str
    text: str
    start: float
    end: float
    channel: str = "mixed"
    confidence: float | None = None
    wall_clock: str = field(default_factory=_utc_now_iso)

    @property
    def duration(self) -> float:
        """Length of the utterance in seconds (never negative)."""
        return max(0.0, self.end - self.start)

    def to_record(self) -> dict[str, Any]:
        """Serialise to the on-disk JSONL shape."""
        return {"type": "utterance", **asdict(self)}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Utterance":
        """Rebuild from a JSONL record, ignoring unknown future fields."""
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in record.items() if k in known}
        missing = {"speaker", "text", "start", "end"} - set(payload)
        if missing:
            raise ValueError(f"utterance record missing field(s): {sorted(missing)}")
        return cls(**payload)


@dataclass(frozen=True)
class MeetingMeta:
    """Header record describing the meeting this transcript belongs to."""

    meet_url: str
    bot_name: str
    started_at: str = field(default_factory=_utc_now_iso)
    schema_version: int = SCHEMA_VERSION

    def to_record(self) -> dict[str, Any]:
        """Serialise to the on-disk JSONL shape."""
        return {"type": "meta", **asdict(self)}


class TranscriptStore:
    """Append-only JSONL writer/reader for a single meeting.

    The writer is thread-safe: the Deepgram reader task and the lifecycle
    watchdog both append, potentially from different threads.

    Usage::

        with TranscriptStore(Path("out/transcript.jsonl")) as store:
            store.write_meta(MeetingMeta(url, name))
            store.append(utterance)
    """

    def __init__(self, path: Path, *, append: bool = False) -> None:
        """Open ``path`` for writing.

        Args:
            path: Destination JSONL file. Parent directories are created.
            append: Append to an existing file instead of truncating it.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._count = 0
        self._handle = self.path.open("a" if append else "w", encoding="utf-8")

    # -- writing -----------------------------------------------------------

    def _write_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            if self._closed:
                raise ValueError(f"TranscriptStore for {self.path} is closed")
            self._handle.write(line + "\n")
            # Flush on every record: a meeting bot that loses the transcript
            # when it crashes is worse than useless.
            self._handle.flush()
            self._count += 1

    def write_meta(self, meta: MeetingMeta) -> None:
        """Write the meeting header record."""
        self._write_record(meta.to_record())

    def append(self, utterance: Utterance) -> None:
        """Append one finalised utterance."""
        self._write_record(utterance.to_record())

    def append_event(self, kind: str, **details: Any) -> None:
        """Append a lifecycle event record.

        Args:
            kind: Short machine-readable event name, e.g. ``"joined"``.
            **details: JSON-serialisable extra context.
        """
        self._write_record(
            {
                "type": "event",
                "kind": kind,
                "wall_clock": _utc_now_iso(),
                "details": details,
            }
        )

    @property
    def record_count(self) -> int:
        """Number of records written by this instance."""
        return self._count

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Flush and close the underlying file. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handle.flush()
            finally:
                self._handle.close()
        logger.info("Transcript closed: %s (%d records)", self.path, self._count)

    def __enter__(self) -> "TranscriptStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield every well-formed record from a transcript file.

    Malformed lines are logged at WARNING and skipped rather than aborting the
    read: a truncated final line after a hard kill must not cost the rest of
    the transcript.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No transcript at {path}")
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping malformed line %d of %s: %s", lineno, path, exc
                )
                continue
            if not isinstance(record, dict) or "type" not in record:
                logger.warning(
                    "Skipping line %d of %s: not a typed record", lineno, path
                )
                continue
            yield record


def read_utterances(path: Path) -> list[Utterance]:
    """Load every utterance from a transcript file, oldest first.

    The file is in *arrival* order, which is not the same as chronological
    order once more than one channel is being transcribed. Local capture
    sends the microphone and the speakers as separate channels and Deepgram
    finalises them independently, so an utterance that starts at 00:43 can be
    written after one that starts at 00:45. Read back unsorted, the
    transcript reads jumbled and the summariser is handed the conversation
    out of sequence.

    Sorting happens here rather than at write time because the store is
    append-only on purpose: that is what makes a transcript survive the
    process being killed mid-meeting. The sort is stable, so utterances
    sharing a start time keep the order they arrived in, and a single-channel
    transcript - already chronological - is unchanged by it.
    """
    utterances: list[Utterance] = []
    for record in iter_records(path):
        if record.get("type") != "utterance":
            continue
        try:
            utterances.append(Utterance.from_record(record))
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping unreadable utterance in %s: %s", path, exc)
    return sorted(utterances, key=lambda u: u.start)


#: Event kind carrying a learned diarization-id -> real-name mapping.
SPEAKER_NAMES_EVENT = "speaker_names"


def read_speaker_names(path: Path) -> dict[str, str]:
    """Learned diarization-id to real-name mappings, latest winning.

    The transcript is append-only, so an utterance written before its
    speaker was identified still carries the generic label. Rather than
    rewrite history - which would cost the crash-durability the whole format
    exists for - the mapping is recorded as an event and applied when the
    transcript is read.
    """
    mapping: dict[str, str] = {}
    for record in iter_records(path):
        if record.get("type") != "event" or record.get("kind") != SPEAKER_NAMES_EVENT:
            continue
        # append_event nests its keyword arguments under "details".
        details = record.get("details")
        names = details.get("names") if isinstance(details, dict) else None
        if isinstance(names, dict):
            mapping.update({str(k): str(v) for k, v in names.items() if k and v})
    return mapping


def apply_speaker_names(
    utterances: list[Utterance], mapping: dict[str, str]
) -> list[Utterance]:
    """Relabel utterances still carrying a diarization id.

    Only generic labels are replaced. Real names never collide with the
    ``"Speaker 0"``-shaped keys, so an utterance named at capture time is
    left exactly as it was recorded.
    """
    if not mapping:
        return utterances
    return [
        replace(u, speaker=mapping[u.speaker]) if u.speaker in mapping else u
        for u in utterances
    ]


def read_meta(path: Path) -> dict[str, Any] | None:
    """Return the meeting header record, or ``None`` if the file has none."""
    for record in iter_records(path):
        if record.get("type") == "meta":
            return record
    return None


def speakers(utterances: Iterable[Utterance]) -> list[str]:
    """Distinct speaker labels, in order of first appearance."""
    seen: dict[str, None] = {}
    for utterance in utterances:
        seen.setdefault(utterance.speaker, None)
    return list(seen)
