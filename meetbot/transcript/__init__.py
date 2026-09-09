"""Transcript persistence and rendering."""

from __future__ import annotations

from meetbot.transcript.format import (
    format_timestamp,
    group_turns,
    render_for_llm,
    render_markdown,
    render_text,
)
from meetbot.transcript.notes import Notepad
from meetbot.transcript.store import (
    MeetingMeta,
    TranscriptStore,
    Utterance,
    iter_records,
    read_meta,
    read_utterances,
    speakers,
)

__all__ = [
    "MeetingMeta",
    "Notepad",
    "TranscriptStore",
    "Utterance",
    "format_timestamp",
    "group_turns",
    "iter_records",
    "read_meta",
    "read_utterances",
    "render_for_llm",
    "render_markdown",
    "render_text",
    "speakers",
]
