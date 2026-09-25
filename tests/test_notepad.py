"""Tests for the notepad: templates, enhancement, ask, and storage.

Every LLM call is faked - these tests never require an API key or a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meetbot.analysis.llm import LLMError
from meetbot.analysis.notepad import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    NotepadError,
    enhance_notes,
    get_template,
)
from meetbot.transcript.notes import (
    CHAT_FILE,
    MAX_CHAT_TURNS,
    MAX_NOTES_CHARS,
    Notepad,
)
from meetbot.transcript.store import Utterance
from tests.conftest import FakeLLMClient


# --- templates -------------------------------------------------------------


class TestTemplates:
    def test_every_template_is_usable(self) -> None:
        assert TEMPLATES, "there must be at least one template"
        for template_id, template in TEMPLATES.items():
            assert template.id == template_id
            assert template.name.strip()
            assert template.blurb.strip()
            assert len(template.guidance) > 40, f"{template_id} has no real guidance"

    def test_the_default_template_exists(self) -> None:
        assert DEFAULT_TEMPLATE in TEMPLATES

    def test_to_dict_does_not_leak_the_prompt(self) -> None:
        """Guidance is prompt internals; the UI has no use for it."""
        payload = TEMPLATES[DEFAULT_TEMPLATE].to_dict()
        assert set(payload) == {"id", "name", "blurb"}

    def test_unknown_ids_fall_back_rather_than_raise(self) -> None:
        """A template id is stored with the meeting. Renaming one in a later
        version must not make old meetings unopenable."""
        assert get_template("retired-template").id == DEFAULT_TEMPLATE
        assert get_template(None).id == DEFAULT_TEMPLATE
        assert get_template("").id == DEFAULT_TEMPLATE

    def test_a_known_id_is_returned_as_is(self) -> None:
        assert get_template("standup").id == "standup"


# --- enhancement -----------------------------------------------------------


class TestEnhanceNotes:
    def test_the_notes_are_sent_as_the_spine(self, utterances) -> None:
        client = FakeLLMClient(text_responses=["## Summary\n\nDone."])
        result = enhance_notes(utterances, "budget approved?", client)
        assert result == "## Summary\n\nDone."
        prompt = client.text_calls[0]["user"]
        assert "budget approved?" in prompt
        assert "THE PERSON'S NOTES" in prompt
        assert "Morning everyone" in prompt, "the transcript must be included"

    def test_the_template_guidance_reaches_the_prompt(self, utterances) -> None:
        client = FakeLLMClient(text_responses=["notes"])
        enhance_notes(utterances, "x", client, template_id="standup")
        prompt = client.text_calls[0]["user"]
        assert "by person" in prompt.lower()

    def test_empty_notes_switch_to_writing_from_the_transcript(
        self, utterances
    ) -> None:
        """The bot often runs unattended, so this is a supported case."""
        client = FakeLLMClient(text_responses=["notes"])
        enhance_notes(utterances, "   \n  ", client)
        prompt = client.text_calls[0]["user"]
        assert "did not type any notes" in prompt
        assert "THE PERSON'S NOTES" not in prompt

    def test_notes_without_a_transcript_are_still_tidied(self) -> None:
        """A meeting that captured no audio should not lose the typed notes."""
        client = FakeLLMClient(text_responses=["## Notes\n\n- something"])
        result = enhance_notes([], "raw thoughts", client)
        assert result.startswith("## Notes")
        assert "Nothing was transcribed" in client.text_calls[0]["user"]

    def test_nothing_at_all_is_an_error(self) -> None:
        with pytest.raises(NotepadError, match="nothing to enhance"):
            enhance_notes([], "", FakeLLMClient())

    def test_speaker_labels_are_flagged_as_not_real_names(self, utterances) -> None:
        client = FakeLLMClient(text_responses=["notes"])
        enhance_notes(utterances, "x", client)
        assert "not real names" in client.text_calls[0]["user"]

    def test_a_long_transcript_is_condensed_first(self) -> None:
        """Map phase, then one enhancement call over the condensed parts."""
        # Speakers alternate, so the rendered transcript is many lines -
        # chunking splits on line boundaries, and one uninterrupted monologue
        # would render as a single unsplittable line.
        long_meeting = [
            Utterance(f"Speaker {i % 3}", "word " * 200, float(i), float(i) + 1)
            for i in range(400)
        ]
        client = FakeLLMClient(text_responses=[])
        enhance_notes(
            long_meeting, "my notes", client, max_single_pass_chars=5_000
        )
        assert len(client.text_calls) > 2, "expected several condense calls"
        final = client.text_calls[-1]["user"]
        assert "condensed in parts" in final
        assert "my notes" in final

    def test_the_condense_phase_is_steered_by_the_notes(self) -> None:
        # Speakers alternate, so the rendered transcript is many lines -
        # chunking splits on line boundaries, and one uninterrupted monologue
        # would render as a single unsplittable line.
        long_meeting = [
            Utterance(f"Speaker {i % 3}", "word " * 200, float(i), float(i) + 1)
            for i in range(400)
        ]
        client = FakeLLMClient(text_responses=[])
        enhance_notes(
            long_meeting, "migration script", client, max_single_pass_chars=5_000
        )
        assert "migration script" in client.text_calls[0]["user"]

    def test_an_empty_model_reply_is_an_error(self, utterances) -> None:
        """Silently writing empty notes over good ones would be worse."""
        client = FakeLLMClient(text_responses=["   "])
        with pytest.raises(NotepadError, match="empty notes"):
            enhance_notes(utterances, "x", client)

    def test_a_provider_failure_becomes_a_notepad_error(self, utterances) -> None:
        client = FakeLLMClient()
        client.text_error = LLMError("rate limit hit")
        with pytest.raises(NotepadError, match="rate limit hit"):
            enhance_notes(utterances, "x", client)

    def test_the_meeting_url_and_duration_are_context(self, utterances) -> None:
        client = FakeLLMClient(text_responses=["notes"])
        enhance_notes(
            utterances, "x", client,
            meeting_url="https://meet.google.com/abc-defg-hij",
            duration="00:42:10",
        )
        prompt = client.text_calls[0]["user"]
        assert "abc-defg-hij" in prompt
        assert "00:42:10" in prompt


# Questions about a meeting are covered in test_recall.py, beside the
# citations they now carry.


# --- storage ---------------------------------------------------------------


class TestNotepadStorage:
    @pytest.fixture
    def pad(self, tmp_path: Path) -> Notepad:
        return Notepad(tmp_path / "meeting")

    def test_reads_are_empty_before_anything_is_written(self, pad: Notepad) -> None:
        """A meeting recorded before the notepad existed is not an error."""
        assert pad.read_notes() == ""
        assert pad.read_enhanced() == ""
        assert pad.read_meta() == {}
        assert pad.read_chat() == []
        assert pad.has_notes is False
        assert pad.has_enhanced is False
        assert pad.template == ""

    def test_notes_round_trip(self, pad: Notepad) -> None:
        pad.write_notes("budget approved?\npriya -> migration")
        assert pad.read_notes() == "budget approved?\npriya -> migration"
        assert pad.has_notes is True

    def test_writing_leaves_no_temporary_files_behind(self, pad: Notepad) -> None:
        pad.write_notes("hello")
        pad.write_enhanced("## Notes")
        leftovers = [p.name for p in pad.directory.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_an_emptied_notepad_does_not_count_as_notes(self, pad: Notepad) -> None:
        pad.write_notes("something")
        pad.write_notes("")
        assert pad.has_notes is False

    def test_notes_are_truncated_rather_than_filling_the_disk(
        self, pad: Notepad
    ) -> None:
        pad.write_notes("x" * (MAX_NOTES_CHARS + 5_000))
        assert len(pad.read_notes()) == MAX_NOTES_CHARS

    def test_enhanced_notes_record_their_template(self, pad: Notepad) -> None:
        pad.write_enhanced("## Standup", template="standup")
        assert pad.read_enhanced() == "## Standup"
        assert pad.template == "standup"
        assert pad.has_enhanced is True
        assert "enhanced_at" in pad.read_meta()

    def test_enhancing_does_not_touch_the_original_notes(self, pad: Notepad) -> None:
        """If enhancement produces something worse, the original must survive."""
        pad.write_notes("my rough notes")
        pad.write_enhanced("## Something the model made up")
        assert pad.read_notes() == "my rough notes"

    def test_unreadable_metadata_is_ignored(self, pad: Notepad) -> None:
        pad.write_notes("x")
        (pad.directory / "notepad.json").write_text("{not json", encoding="utf-8")
        assert pad.read_meta() == {}
        assert pad.template == ""

    def test_chat_round_trips_oldest_first(self, pad: Notepad) -> None:
        pad.append_chat("what slipped?", "the API work")
        pad.append_chat("who owns it?", "Priya")
        turns = pad.read_chat()
        assert [t["question"] for t in turns] == ["what slipped?", "who owns it?"]
        assert turns[0]["answer"] == "the API work"
        assert "asked_at" in turns[0]

    def test_clearing_the_chat_removes_it(self, pad: Notepad) -> None:
        pad.append_chat("q", "a")
        pad.clear_chat()
        assert pad.read_chat() == []
        # Clearing an already-empty chat must not raise.
        pad.clear_chat()

    def test_malformed_chat_lines_are_skipped(self, pad: Notepad) -> None:
        """A truncated final line must not cost the rest of the history."""
        pad.append_chat("good", "answer")
        with (pad.directory / CHAT_FILE).open("a", encoding="utf-8") as handle:
            handle.write('{"question": "truncated"\n')
        assert [t["question"] for t in pad.read_chat()] == ["good"]

    def test_chat_is_compacted_once_it_grows_too_long(self, pad: Notepad) -> None:
        for i in range(MAX_CHAT_TURNS + 10):
            pad.append_chat(f"q{i}", f"a{i}")
        turns = pad.read_chat()
        assert len(turns) == MAX_CHAT_TURNS
        assert turns[-1]["question"] == f"q{MAX_CHAT_TURNS + 9}"
        assert turns[0]["question"] != "q0", "the oldest turns should be gone"

    def test_notes_survive_non_ascii(self, pad: Notepad) -> None:
        pad.write_notes("café — naïve → résumé")
        assert pad.read_notes() == "café — naïve → résumé"
        pad.append_chat("¿qué?", "sí")
        assert pad.read_chat()[0]["answer"] == "sí"

    def test_saves_landing_together_do_not_fail(self, pad: Notepad) -> None:
        """An autosave and the save "Enhance" sends first can arrive together.

        On Windows a replace onto a file another handle has open fails with
        "Access is denied". Two threads saving at once hit it about once in
        forty writes, and it reached the page as a 500 from the notes route.
        """
        import threading

        failures: list[BaseException] = []

        def save_repeatedly(tag: str) -> None:
            for i in range(150):
                try:
                    pad.write_notes(f"{tag}{i}")
                    pad.read_notes()
                except OSError as exc:
                    failures.append(exc)

        threads = [threading.Thread(target=save_repeatedly, args=(t,)) for t in "ab"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert failures == []
        assert pad.read_notes() in {"a149", "b149"}
        assert "notes_updated_at" in pad.read_meta()

    def test_the_chat_file_is_one_json_object_per_line(self, pad: Notepad) -> None:
        pad.append_chat("q", "a")
        lines = (pad.directory / CHAT_FILE).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["question"] == "q"
