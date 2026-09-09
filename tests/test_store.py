"""Tests for the JSONL transcript store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meetbot.transcript.store import (
    MeetingMeta,
    SPEAKER_NAMES_EVENT,
    TranscriptStore,
    Utterance,
    apply_speaker_names,
    iter_records,
    read_meta,
    read_speaker_names,
    read_utterances,
    speakers,
)


def test_roundtrip_preserves_utterances(tmp_path: Path, utterances) -> None:
    path = tmp_path / "transcript.jsonl"
    with TranscriptStore(path) as store:
        store.write_meta(
            MeetingMeta(meet_url="https://meet.google.com/abc-defg-hij", bot_name="Bot")
        )
        for utterance in utterances:
            store.append(utterance)

    loaded = read_utterances(path)
    assert len(loaded) == len(utterances)
    for original, restored in zip(utterances, loaded):
        assert restored.speaker == original.speaker
        assert restored.text == original.text
        assert restored.start == pytest.approx(original.start)
        assert restored.end == pytest.approx(original.end)


def test_meta_is_readable(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with TranscriptStore(path) as store:
        store.write_meta(
            MeetingMeta(meet_url="https://meet.google.com/abc-defg-hij", bot_name="Bot")
        )
        store.append(Utterance("Speaker 0", "hello", 0.0, 1.0))

    meta = read_meta(path)
    assert meta is not None
    assert meta["meet_url"] == "https://meet.google.com/abc-defg-hij"
    assert meta["bot_name"] == "Bot"
    assert meta["schema_version"] == 1


def test_records_are_flushed_immediately(tmp_path: Path) -> None:
    """A crash mid-meeting must not lose already-transcribed speech."""
    path = tmp_path / "t.jsonl"
    store = TranscriptStore(path)
    store.append(Utterance("Speaker 0", "first", 0.0, 1.0))
    store.append(Utterance("Speaker 1", "second", 1.0, 2.0))

    # Read the file without closing the writer, simulating an external reader
    # after a hard kill.
    assert len(read_utterances(path)) == 2
    store.close()


def test_truncated_final_line_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with TranscriptStore(path) as store:
        store.append(Utterance("Speaker 0", "complete", 0.0, 1.0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "utterance", "speaker": "Spea')

    loaded = read_utterances(path)
    assert [u.text for u in loaded] == ["complete"]


def test_records_missing_required_fields_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"type": "utterance", "speaker": "Speaker 0"})
        + "\n"
        + json.dumps(
            {
                "type": "utterance",
                "speaker": "Speaker 1",
                "text": "kept",
                "start": 0.0,
                "end": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert [u.text for u in read_utterances(path)] == ["kept"]


def test_unknown_future_fields_are_ignored(tmp_path: Path) -> None:
    """A newer writer's extra fields must not break an older reader."""
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "utterance",
                "speaker": "Speaker 0",
                "text": "hello",
                "start": 0.0,
                "end": 1.0,
                "sentiment_v2": "cheerful",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = read_utterances(path)
    assert len(loaded) == 1
    assert loaded[0].text == "hello"


def test_events_do_not_appear_as_utterances(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with TranscriptStore(path) as store:
        store.append_event("joined", display_name="Bot")
        store.append(Utterance("Speaker 0", "hi", 0.0, 1.0))

    assert len(read_utterances(path)) == 1
    kinds = [r.get("kind") for r in iter_records(path) if r["type"] == "event"]
    assert kinds == ["joined"]


def test_append_after_close_raises(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "t.jsonl")
    store.close()
    with pytest.raises(ValueError, match="closed"):
        store.append(Utterance("Speaker 0", "too late", 0.0, 1.0))


def test_close_is_idempotent(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "t.jsonl")
    store.close()
    store.close()


def test_append_mode_preserves_existing_records(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with TranscriptStore(path) as store:
        store.append(Utterance("Speaker 0", "first", 0.0, 1.0))
    with TranscriptStore(path, append=True) as store:
        store.append(Utterance("Speaker 0", "second", 1.0, 2.0))

    assert [u.text for u in read_utterances(path)] == ["first", "second"]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_utterances(tmp_path / "nope.jsonl")


def test_speakers_are_ordered_by_first_appearance(utterances) -> None:
    assert speakers(utterances) == ["Speaker 0", "Speaker 1"]


def test_utterance_duration_is_never_negative() -> None:
    assert Utterance("Speaker 0", "x", 5.0, 4.0).duration == 0.0
    assert Utterance("Speaker 0", "x", 1.0, 3.5).duration == pytest.approx(2.5)


def test_non_ascii_text_survives_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with TranscriptStore(path) as store:
        store.append(Utterance("Renée", "Café — naïve résumé 🎧", 0.0, 2.0))

    loaded = read_utterances(path)
    assert loaded[0].speaker == "Renée"
    assert loaded[0].text == "Café — naïve résumé 🎧"


class TestRetroactiveSpeakerNames:
    """Utterances written before their speaker was identified.

    The log is append-only for crash durability, so it is never rewritten.
    The mapping is recorded as an event and applied when the transcript is
    read back.
    """

    def test_generic_labels_are_replaced(self, tmp_path) -> None:
        utterances = [
            Utterance("Speaker 0", "Morning.", 0.0, 1.0),
            Utterance("Speaker 1", "Hello.", 1.0, 2.0),
        ]
        named = apply_speaker_names(
            utterances, {"Speaker 0": "Neil Sharma", "Speaker 1": "Priya"}
        )
        assert [u.speaker for u in named] == ["Neil Sharma", "Priya"]

    def test_names_recorded_at_capture_time_are_left_alone(self) -> None:
        utterances = [Utterance("Neil Sharma", "Morning.", 0.0, 1.0)]
        named = apply_speaker_names(utterances, {"Speaker 0": "Someone Else"})
        assert named[0].speaker == "Neil Sharma"

    def test_an_empty_mapping_changes_nothing(self) -> None:
        utterances = [Utterance("Speaker 0", "Morning.", 0.0, 1.0)]
        assert apply_speaker_names(utterances, {}) == utterances

    def test_the_mapping_round_trips_through_the_log(self, tmp_path) -> None:
        path = tmp_path / "t.jsonl"
        store = TranscriptStore(path)
        store.write_meta(MeetingMeta(meet_url="https://meet.google.com/a-b-c", bot_name="bot"))
        store.append(Utterance("Speaker 0", "Morning.", 0.0, 1.0))
        store.append_event(SPEAKER_NAMES_EVENT, names={"Speaker 0": "Neil Sharma"})
        store.close()

        assert read_speaker_names(path) == {"Speaker 0": "Neil Sharma"}
        named = apply_speaker_names(read_utterances(path), read_speaker_names(path))
        assert named[0].speaker == "Neil Sharma"

    def test_a_transcript_without_the_event_is_unaffected(self, tmp_path) -> None:
        path = tmp_path / "t.jsonl"
        store = TranscriptStore(path)
        store.append(Utterance("Speaker 0", "Morning.", 0.0, 1.0))
        store.close()
        assert read_speaker_names(path) == {}


class TestChronologicalReads:
    """Arrival order is not chronological order with two channels in flight.

    Found on the first real local recording: the microphone and the speakers
    are separate Deepgram channels, finalised independently, so the file
    contained a turn starting at 00:45 written before one starting at 00:43.
    The transcript read jumbled and the summariser was handed the
    conversation out of sequence.
    """

    def test_utterances_come_back_oldest_first(self, tmp_path) -> None:
        from meetbot.transcript.store import (
            TranscriptStore,
            Utterance,
            read_utterances,
        )

        path = tmp_path / "transcript.jsonl"
        with TranscriptStore(path) as store:
            store.append(Utterance("Speaker 0", "exposed to nature", 40.7, 43.0))
            # Written next, but starts *later* than the one after it.
            store.append(Utterance("You", "P I.", 45.5, 46.0))
            store.append(Utterance("Speaker 0", "outdoor activities", 43.9, 45.0))

        starts = [u.start for u in read_utterances(path)]
        assert starts == sorted(starts)
        assert starts == [40.7, 43.9, 45.5]

    def test_the_file_itself_stays_in_arrival_order(self, tmp_path) -> None:
        """The store is append-only on purpose - that is what survives a kill.

        Sorting belongs to the reader; rewriting the file to keep it ordered
        would cost the crash durability the format exists for.
        """
        import json

        from meetbot.transcript.store import TranscriptStore, Utterance

        path = tmp_path / "transcript.jsonl"
        with TranscriptStore(path) as store:
            store.append(Utterance("You", "second", 45.5, 46.0))
            store.append(Utterance("Speaker 0", "first", 43.9, 45.0))

        written = [
            json.loads(line)["start"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert written == [45.5, 43.9], "the file was rewritten"

    def test_the_sort_is_stable_for_equal_starts(self, tmp_path) -> None:
        from meetbot.transcript.store import (
            TranscriptStore,
            Utterance,
            read_utterances,
        )

        path = tmp_path / "transcript.jsonl"
        with TranscriptStore(path) as store:
            store.append(Utterance("You", "mine", 10.0, 11.0))
            store.append(Utterance("Speaker 0", "theirs", 10.0, 11.0))

        assert [u.text for u in read_utterances(path)] == ["mine", "theirs"]

    def test_a_single_channel_transcript_is_unchanged(self, tmp_path) -> None:
        """The bot path is already chronological; sorting must be a no-op."""
        from meetbot.transcript.store import (
            TranscriptStore,
            Utterance,
            read_utterances,
        )

        path = tmp_path / "transcript.jsonl"
        texts = ["one", "two", "three"]
        with TranscriptStore(path) as store:
            for i, text in enumerate(texts):
                store.append(Utterance("Speaker 0", text, float(i), float(i) + 0.5))

        assert [u.text for u in read_utterances(path)] == texts
