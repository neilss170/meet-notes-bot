"""Tests for transcript rendering."""

from __future__ import annotations

from meetbot.transcript.format import (
    TURN_GAP_S,
    format_timestamp,
    group_turns,
    render_for_llm,
    render_markdown,
    render_text,
)
from meetbot.transcript.store import Utterance


def test_format_timestamp() -> None:
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(59.9) == "00:00:59"
    assert format_timestamp(3725.4) == "01:02:05"
    assert format_timestamp(-5) == "00:00:00"


def test_group_turns_merges_same_speaker_within_gap(utterances) -> None:
    turns = group_turns(utterances)
    # 0.0-4.8 merge, 6.0 is Speaker 1, 40.0 is a new Speaker 0 turn (gap > 8s).
    assert [t.speaker for t in turns] == [
        "Speaker 0",
        "Speaker 1",
        "Speaker 0",
        "Speaker 1",
    ]
    assert turns[0].text == "Morning everyone, let's start with the roadmap. I'll go first."
    assert turns[0].end == 4.8


def test_group_turns_splits_on_long_silence() -> None:
    utterances = [
        Utterance("Speaker 0", "before", 0.0, 1.0),
        Utterance("Speaker 0", "after", 1.0 + TURN_GAP_S + 1, 2.0 + TURN_GAP_S + 1),
    ]
    turns = group_turns(utterances)
    assert len(turns) == 2


def test_group_turns_drops_empty_text() -> None:
    utterances = [
        Utterance("Speaker 0", "   ", 0.0, 1.0),
        Utterance("Speaker 0", "real", 1.0, 2.0),
    ]
    turns = group_turns(utterances)
    assert len(turns) == 1
    assert turns[0].text == "real"


def test_group_turns_on_empty_input() -> None:
    assert group_turns([]) == []


def test_render_text_has_one_line_per_turn(utterances) -> None:
    lines = render_text(utterances).splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("[00:00:00] Speaker 0: ")


def test_render_text_without_timestamps(utterances) -> None:
    lines = render_text(utterances, timestamps=False).splitlines()
    assert lines[0].startswith("Speaker 0: ")
    assert "[" not in lines[0]


def test_render_markdown_includes_header_and_speakers(utterances) -> None:
    rendered = render_markdown(
        utterances,
        title="Standup",
        meta={"meeting_url": "https://meet.google.com/abc-defg-hij"},
    )
    assert rendered.startswith("# Standup")
    assert "https://meet.google.com/abc-defg-hij" in rendered
    assert "**Speakers detected:** 2" in rendered
    assert "**[00:00:40] Speaker 0**" in rendered
    assert rendered.endswith("\n")


def test_render_markdown_on_empty_transcript() -> None:
    rendered = render_markdown([])
    assert "_No speech was captured during this meeting._" in rendered


def test_render_for_llm_returns_full_text_under_budget(utterances) -> None:
    rendered = render_for_llm(utterances, max_chars=10_000)
    assert rendered == render_text(utterances)


def test_render_for_llm_truncates_the_middle() -> None:
    long_meeting = [
        Utterance("Speaker 0", f"Discussion item number {i} for the quarter.",
                  i * 20.0, i * 20.0 + 5.0)
        for i in range(60)
    ]
    full = render_text(long_meeting)
    budget = len(full) // 2
    rendered = render_for_llm(long_meeting, max_chars=budget)

    assert len(rendered) <= budget
    assert "transcript truncated in the middle" in rendered
    # Both ends of the meeting survive; the middle is what goes.
    assert rendered.startswith("[00:00:00] Speaker 0: Discussion item number 0")
    assert rendered.rstrip().endswith("Discussion item number 59 for the quarter.")
    # Whole turns only - never half a line with no speaker attached.
    for line in rendered.splitlines():
        assert line == "" or line.startswith("[") or line.startswith("[...")


def test_render_for_llm_with_a_budget_smaller_than_one_turn(utterances) -> None:
    """A degenerate budget still returns both ends, not just the marker."""
    rendered = render_for_llm(utterances, max_chars=80)

    assert len(rendered) <= 80
    assert "truncated" in rendered
    assert rendered.startswith("[00:00:00]")


def test_render_for_llm_without_budget_is_untruncated(utterances) -> None:
    assert "truncated" not in render_for_llm(utterances)
