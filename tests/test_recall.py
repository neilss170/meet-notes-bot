"""Recall: questions across every meeting, and the citations behind an answer.

Every model call is faked. What is under test is everything around the model:
which transcript lines it is shown, and that whatever it cites resolves to a
real line in the right meeting - with the lines around it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pytest

from meetbot.analysis.llm import LLMError
from meetbot.analysis.recall import (
    HEADER_OVERHEAD,
    HISTORY_TURNS,
    SEGMENT_MAX_CHARS,
    MeetingSource,
    RecallError,
    SearchPlan,
    answer_about_meeting,
    answer_across_meetings,
    plan_search,
    rank_windows,
    resolve_citations,
    segment_utterances,
    strip_citations,
    tokenize,
)
from meetbot.transcript.store import Utterance
from tests.conftest import FakeLLMClient


def _day(month: int, day: int, hour: int = 10) -> float:
    return datetime(2026, month, day, hour).timestamp()


def _meeting(
    meeting_id: str,
    title: str,
    lines: list[tuple[str, str]],
    *,
    when: float | None = None,
    alias: dict[str, str] | None = None,
) -> MeetingSource:
    """A meeting from ``(speaker, text)`` pairs, spaced so they never merge."""
    utterances = [
        Utterance(speaker, text, i * 20.0, i * 20.0 + 5.0)
        for i, (speaker, text) in enumerate(lines)
    ]
    return MeetingSource(
        id=meeting_id,
        title=title,
        started_at=_day(9, 1) if when is None else when,
        utterances=utterances,
        speaker_alias=alias or {},
    )


def _cost(*sources: MeetingSource) -> int:
    """What sending these meetings whole would cost against a budget."""
    return sum(s.cost(range(len(s.segments))) + HEADER_OVERHEAD for s in sources)


class CitingLLM(FakeLLMClient):
    """Answers by citing whichever line contains ``phrase``, as a model would."""

    def __init__(self, phrase: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.phrase = phrase

    def complete_text(self, **kwargs: Any) -> str:
        self.text_calls.append(kwargs)
        found = re.search(r"\[(S\d+)\][^\n]*" + re.escape(self.phrase), kwargs["user"])
        return f"It came up here [{found.group(1)}]." if found else "I couldn't find that."


@pytest.fixture
def archive() -> list[MeetingSource]:
    return [
        _meeting(
            "standup",
            "Standup",
            [
                ("Speaker 0", "Morning, quick round of updates."),
                ("Speaker 1", "The API work is two weeks behind."),
                ("Speaker 0", "Priya will write the migration script by Friday."),
            ],
            when=_day(9, 8),
        ),
        _meeting(
            "budget",
            "Finance sync",
            [
                ("Speaker 0", "Let's look at the numbers for next quarter."),
                ("Speaker 1", "The marketing budget is capped at forty thousand."),
                ("Speaker 0", "Anything above that needs sign-off from Dana."),
            ],
            when=_day(9, 3),
        ),
        _meeting(
            "yolo",
            "YOLOv11 notes",
            [
                ("Speaker 0", "The backbone downsamples the image with strided convolutions."),
                ("Speaker 0", "The SPPF block aggregates features from several scales."),
            ],
            when=_day(9, 10),
        ),
    ]


# --- segments ----------------------------------------------------------------


class TestSegments:
    def test_fragments_join_into_a_line_worth_quoting(self) -> None:
        """Deepgram finalises a lone "This" as an utterance of its own."""
        segments = segment_utterances([
            Utterance("Speaker 0", "This", 0.0, 0.4),
            Utterance("Speaker 0", "is followed by", 0.6, 1.5),
            Utterance("Speaker 0", "repeated C3K2 modules.", 1.7, 3.0),
        ])
        assert [s.text for s in segments] == ["This is followed by repeated C3K2 modules."]
        assert (segments[0].start, segments[0].end) == (0.0, 3.0)

    def test_a_new_speaker_starts_a_new_line(self) -> None:
        segments = segment_utterances([
            Utterance("You", "Are we on track?", 0.0, 1.0),
            Utterance("Speaker 0", "Two weeks behind.", 1.2, 2.0),
        ])
        assert [s.speaker for s in segments] == ["You", "Speaker 0"]

    def test_a_long_pause_starts_a_new_line(self) -> None:
        segments = segment_utterances([
            Utterance("Speaker 0", "First thought.", 0.0, 1.0),
            Utterance("Speaker 0", "Much later thought.", 30.0, 31.0),
        ])
        assert len(segments) == 2

    def test_a_monologue_is_still_split_into_citable_lines(self) -> None:
        """Ten minutes of one speaker is one turn, and citing it points nowhere."""
        utterances = [
            Utterance("Speaker 0", f"Sentence {i} about the model.", i * 2.0, i * 2.0 + 1.5)
            for i in range(200)
        ]
        segments = segment_utterances(utterances)
        assert len(segments) > 10
        assert all(len(s.text) <= SEGMENT_MAX_CHARS for s in segments)
        assert " ".join(s.text for s in segments) == " ".join(u.text for u in utterances)

    def test_blank_fragments_are_dropped(self) -> None:
        assert segment_utterances([Utterance("Speaker 0", "   ", 0.0, 1.0)]) == []


# --- search ------------------------------------------------------------------


class TestTokenize:
    @pytest.mark.parametrize(
        "one, other",
        [
            ("deadline", "deadlines"),
            ("planned", "planning"),
            ("processes", "processed"),
            ("release", "released"),
            ("budget", "budgets"),
        ],
    )
    def test_forms_of_a_word_find_each_other(self, one: str, other: str) -> None:
        assert tokenize(one) == tokenize(other)

    def test_the_way_a_question_is_asked_is_not_searched_for(self) -> None:
        assert tokenize("When was the budget mentioned in a meeting?") == ["budget"]
        assert tokenize("discusses talking mentions") == [], "in any form"

    def test_short_names_are_not_mistaken_for_stopwords(self) -> None:
        """"same" stems to "sam"; that must not make Sam unsearchable."""
        assert tokenize("Did Sam own the rollout?") == ["sam", "own", "rollout"]

    def test_identifiers_survive(self) -> None:
        assert "c3k2" in tokenize("repeated C3K2 modules")


class TestRanking:
    def test_the_meeting_that_said_it_ranks_first(self, archive) -> None:
        ranked = rank_windows(archive, SearchPlan("what is the marketing budget?"))
        assert ranked, "a word that was said must match something"
        assert archive[ranked[0][1]].id == "budget"

    def test_search_terms_find_a_paraphrase(self, archive) -> None:
        """Nobody said "spend". The model's search terms bridge the gap."""
        question = "how much are we allowed to spend?"
        assert rank_windows(archive, SearchPlan(question)) == []
        ranked = rank_windows(
            archive, SearchPlan(question, terms=("budget", "spend", "cost"))
        )
        assert archive[ranked[0][1]].id == "budget"

    def test_naming_the_meeting_finds_it(self, archive) -> None:
        ranked = rank_windows(archive, SearchPlan("what happened in the finance sync?"))
        assert archive[ranked[0][1]].id == "budget"


class TestPlanSearch:
    def test_terms_and_a_period_come_back_parsed(self) -> None:
        client = FakeLLMClient(
            json_response={
                "terms": ["budget", "  spend "],
                "from_date": "2026-09-01",
                "to_date": "2026-09-07",
            }
        )
        plan = plan_search("spending last week?", client, today=date(2026, 9, 10))
        assert plan.terms == ("budget", "spend")
        assert (plan.from_day, plan.to_day) == (date(2026, 9, 1), date(2026, 9, 7))
        assert "2026-09-10" in client.json_calls[0]["user"], "the model needs today"

    def test_a_backwards_period_is_put_right(self) -> None:
        client = FakeLLMClient(
            json_response={"terms": [], "from_date": "2026-09-07", "to_date": "2026-09-01"}
        )
        plan = plan_search("q", client)
        assert plan.from_day < plan.to_day

    def test_a_failure_searches_the_question_as_typed(self) -> None:
        client = FakeLLMClient()
        client.json_error = LLMError("down")
        assert plan_search("budget?", client) == SearchPlan("budget?")

    def test_a_malformed_reply_is_survivable(self) -> None:
        client = FakeLLMClient(
            json_response={"terms": "budget", "from_date": "last week", "to_date": 7}
        )
        plan = plan_search("budget?", client)
        assert plan.terms == ()
        assert not plan.has_period


# --- citations ---------------------------------------------------------------


class TestCitations:
    @pytest.fixture
    def source(self) -> MeetingSource:
        return _meeting(
            "m1",
            "Standup",
            [
                ("Speaker 0", "Where are we on the API?"),
                ("Speaker 1", "Two weeks behind, sadly."),
                ("Speaker 0", "Then Priya takes the migration script."),
                ("Speaker 1", "Agreed, by Friday."),
            ],
        )

    @staticmethod
    def _ids(count: int) -> dict[str, tuple[int, int]]:
        return {f"S{i + 1}": (0, i) for i in range(count)}

    def test_ids_become_numbers_in_order_of_appearance(self, source) -> None:
        text, citations = resolve_citations(
            "Behind [S2]. Priya owns it [S3].", self._ids(4), [source]
        )
        assert text == "Behind[^1]. Priya owns it[^2]."
        assert [c.n for c in citations] == [1, 2]
        assert [c.text for c in citations] == [
            "Two weeks behind, sadly.",
            "Then Priya takes the migration script.",
        ]

    def test_a_line_cited_twice_keeps_its_number(self, source) -> None:
        text, citations = resolve_citations("A [S2]. B [S3]. C [S2].", self._ids(4), [source])
        assert text == "A[^1]. B[^2]. C[^1]."
        assert len(citations) == 2

    def test_an_id_the_model_invented_is_dropped(self, source) -> None:
        """A citation must resolve to a real line, or it is not shown at all."""
        text, citations = resolve_citations("Confident claim [S99].", self._ids(4), [source])
        assert text == "Confident claim."
        assert citations == []

    @pytest.mark.parametrize(
        "written", ["[S2, S3]", "[S2][S3]", "[S2-S3]", "【S2】【S3】", "[S2; S3]"]
    )
    def test_the_ways_models_write_citations_are_understood(
        self, source, written: str
    ) -> None:
        text, citations = resolve_citations(f"Both {written}.", self._ids(4), [source])
        assert text == "Both[^1][^2]."
        assert [c.speaker for c in citations] == ["Speaker 1", "Speaker 0"]

    def test_a_citation_carries_what_led_up_to_it_and_what_came_after(
        self, source
    ) -> None:
        """Why something was said is usually the line before it."""
        _, [citation] = resolve_citations("x [S3]", self._ids(4), [source])
        assert [s.text for s in citation.before] == [
            "Where are we on the API?",
            "Two weeks behind, sadly.",
        ]
        assert [s.text for s in citation.after] == ["Agreed, by Friday."]
        assert citation.meeting_id == "m1"
        assert citation.start == 40.0

    def test_a_citation_does_not_swallow_a_line_break(self, source) -> None:
        text, _ = resolve_citations("- first\n[S1] second", self._ids(4), [source])
        assert text == "- first\n[^1] second"

    def test_markers_can_be_stripped_for_a_clean_answer(self) -> None:
        assert strip_citations("Behind[^1]. Priya[^2][^3].") == "Behind. Priya."


# --- asking every meeting ----------------------------------------------------


class TestAskingEveryMeeting:
    def test_a_small_archive_is_read_whole_with_no_search_step(self, archive) -> None:
        """Ranking a handful of short recordings only adds a way to miss."""
        client = CitingLLM("capped at forty thousand")
        result = answer_across_meetings(
            archive, "What's the marketing budget?", client, today=date(2026, 9, 10)
        )
        assert client.json_calls == [], "no search terms should have been requested"
        prompt = client.text_calls[0]["user"]
        for source in archive:
            for segment in source.segments:
                assert segment.text in prompt
        assert result.searched == 3
        [citation] = result.citations
        assert citation.meeting_id == "budget"
        assert citation.meeting_title == "Finance sync"
        assert "[^1]" in result.answer

    def test_meetings_are_shown_oldest_first_with_their_dates(self, archive) -> None:
        client = CitingLLM("nothing")
        answer_across_meetings(archive, "anything?", client)
        prompt = client.text_calls[0]["user"]
        assert (
            prompt.index("Finance sync")
            < prompt.index("## Standup")
            < prompt.index("YOLOv11 notes")
        )
        assert "03 Sep 2026" in prompt

    def test_a_large_archive_is_searched_and_only_the_hits_are_read(
        self, archive
    ) -> None:
        filler = [
            _meeting(
                f"filler-{i}",
                f"Weekly sync {i}",
                [
                    ("Speaker 0", f"Routine update {i} about the office plants."),
                    ("Speaker 1", "Nothing else to report this week, thanks everyone."),
                ]
                * 3,
                when=_day(8, 1 + i),
            )
            for i in range(12)
        ]
        client = CitingLLM("capped at forty thousand")
        client.json_response = {
            "terms": ["budget", "marketing"],
            "from_date": "",
            "to_date": "",
        }
        result = answer_across_meetings(
            [*filler, *archive],
            "What's the marketing budget?",
            client,
            budget=_cost(*archive),
        )
        assert len(client.json_calls) == 1, "a large archive should ask for search terms"
        prompt = client.text_calls[0]["user"]
        assert "capped at forty thousand" in prompt
        assert "office plants" not in prompt
        assert result.citations[0].meeting_id == "budget"

    def test_with_nothing_to_match_the_most_recent_meetings_are_read(
        self, archive
    ) -> None:
        client = CitingLLM("nothing")
        answer_across_meetings(
            archive, "what did I do?", client, budget=_cost(*archive) - 1
        )
        prompt = client.text_calls[0]["user"]
        assert "openings of the most recent meetings" in prompt
        assert "strided convolutions" in prompt, "the newest meeting should be read"

    def test_a_period_in_the_question_limits_the_search(self, archive) -> None:
        client = CitingLLM("two weeks behind")
        client.json_response = {
            "terms": ["behind"],
            "from_date": "2026-09-07",
            "to_date": "2026-09-09",
        }
        result = answer_across_meetings(
            archive,
            "What slipped last week?",
            client,
            budget=_cost(*archive) - 1,
            today=date(2026, 9, 10),
        )
        prompt = client.text_calls[0]["user"]
        assert result.searched == 1
        assert "Finance sync" not in prompt
        assert "YOLOv11" not in prompt
        assert "07 Sep 2026 to 09 Sep 2026" in prompt
        assert result.citations[0].meeting_id == "standup"

    def test_a_period_with_no_meetings_searches_everything_and_says_so(
        self, archive
    ) -> None:
        client = CitingLLM("capped at forty thousand")
        client.json_response = {
            "terms": ["budget"],
            "from_date": "2025-01-01",
            "to_date": "2025-01-31",
        }
        result = answer_across_meetings(
            archive, "budget in January?", client, budget=_cost(*archive) - 1
        )
        assert result.searched == 3
        assert "every meeting was searched instead" in client.text_calls[0]["user"]

    def test_anonymised_names_stay_off_the_prompt_but_not_off_the_citation(
        self,
    ) -> None:
        source = _meeting(
            "m",
            "One-to-one",
            [
                ("Priya Raman", "I'll own the migration script."),
                ("Neil", "Great, thanks."),
            ],
            alias={"Priya Raman": "Speaker A", "Neil": "Speaker B"},
        )
        client = CitingLLM("own the migration script")
        result = answer_across_meetings([source], "who owns the script?", client)
        prompt = client.text_calls[0]["user"]
        assert "Priya" not in prompt
        assert "Speaker A" in prompt
        assert result.citations[0].speaker == "Priya Raman"

    def test_an_empty_question_is_rejected(self, archive) -> None:
        with pytest.raises(RecallError, match="Ask a question"):
            answer_across_meetings(archive, "  ", FakeLLMClient())

    def test_no_transcripts_is_an_error_not_an_empty_answer(self) -> None:
        client = FakeLLMClient()
        with pytest.raises(RecallError, match="nothing to search"):
            answer_across_meetings([_meeting("m", "Silent", [])], "anything?", client)
        assert not client.text_calls, "no quota should have been spent"

    def test_earlier_answers_are_replayed_without_their_markers(self, archive) -> None:
        client = CitingLLM("two weeks behind")
        answer_across_meetings(
            archive,
            "and who owns it?",
            client,
            history=[{"question": "what slipped?", "answer": "The API work[^1]."}],
        )
        prompt = client.text_calls[0]["user"]
        assert "what slipped?" in prompt
        assert "The API work." in prompt
        assert "[^1]" not in prompt

    def test_history_is_capped(self, archive) -> None:
        history = [
            {"question": f"question {i}", "answer": f"answer {i}"}
            for i in range(HISTORY_TURNS * 3)
        ]
        client = CitingLLM("x")
        answer_across_meetings(archive, "latest?", client, history=history)
        prompt = client.text_calls[0]["user"]
        assert "question 0\n" not in prompt, "the oldest turns should have fallen off"
        assert f"question {len(history) - 1}" in prompt

    def test_a_provider_failure_is_not_disguised(self, archive) -> None:
        client = FakeLLMClient()
        client.text_error = LLMError("rate limit hit")
        with pytest.raises(LLMError, match="rate limit"):
            answer_across_meetings(archive, "anything?", client)

    def test_an_empty_answer_is_an_error(self, archive) -> None:
        with pytest.raises(RecallError, match="empty answer"):
            answer_across_meetings(
                archive, "anything?", FakeLLMClient(text_responses=[""])
            )


# --- asking one meeting ------------------------------------------------------


class TestAskingOneMeeting:
    def test_answers_cite_the_transcript(self, utterances) -> None:
        client = CitingLLM("two weeks behind")
        result = answer_about_meeting(
            MeetingSource("m", "Standup", 0.0, utterances), "How is the API work?", client
        )
        assert "How is the API work?" in client.text_calls[0]["user"]
        [citation] = result.citations
        assert citation.text == "We're two weeks behind on the API work."
        assert result.searched == 1

    def test_the_notes_are_included_as_context(self, utterances) -> None:
        """They carry real names and corrected spellings the transcript lacks."""
        client = FakeLLMClient(text_responses=["ok"])
        answer_about_meeting(
            MeetingSource("m", "t", 0.0, utterances),
            "q",
            client,
            notes="Priya owns the migration",
        )
        assert "Priya owns the migration" in client.text_calls[0]["user"]

    def test_history_is_replayed_for_follow_ups(self, utterances) -> None:
        client = FakeLLMClient(text_responses=["ok"])
        answer_about_meeting(
            MeetingSource("m", "t", 0.0, utterances),
            "and who owns it?",
            client,
            history=[{"question": "what slipped?", "answer": "the API work"}],
        )
        prompt = client.text_calls[0]["user"]
        assert "what slipped?" in prompt
        assert "the API work" in prompt

    def test_an_empty_question_is_rejected(self, utterances) -> None:
        with pytest.raises(RecallError, match="Ask a question"):
            answer_about_meeting(
                MeetingSource("m", "t", 0.0, utterances), "   ", FakeLLMClient()
            )

    def test_nothing_to_search_is_an_error(self) -> None:
        with pytest.raises(RecallError, match="nothing to search"):
            answer_about_meeting(
                MeetingSource("m", "t", 0.0, []), "anything?", FakeLLMClient()
            )

    def test_notes_alone_are_enough_to_search(self) -> None:
        client = FakeLLMClient(text_responses=["yes"])
        result = answer_about_meeting(
            MeetingSource("m", "t", 0.0, []), "q", client, notes="some notes"
        )
        assert result.answer == "yes"
        assert result.citations == ()

    def test_a_long_meeting_keeps_its_opening_and_its_close(self) -> None:
        utterances = [
            Utterance(
                "Speaker 0" if i % 2 else "Speaker 1",
                f"line {i} " + "words " * 30,
                i * 20.0,
                i * 20.0 + 5.0,
            )
            for i in range(400)
        ]
        client = FakeLLMClient(text_responses=["ok"])
        answer_about_meeting(
            MeetingSource("m", "t", 0.0, utterances), "q", client, budget=5_000
        )
        prompt = client.text_calls[0]["user"]
        assert "line 0 " in prompt
        assert "line 399 " in prompt
        assert "line 200 " not in prompt
        assert "middle of the transcript is left out" in prompt
        assert len(prompt) < 8_000
