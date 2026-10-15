"""Tests for the LLM analysis module.

Every LLM call is mocked - these tests never require an API key or a network.
"""

from __future__ import annotations

import sys
import types

import pytest

from meetbot.analysis.llm import GEMINI_BASE_URL, LLMError, build_client
from meetbot.analysis.summarize import (
    ActionItem,
    AnalysisError,
    MeetingAnalysis,
    analyze_transcript,
    chunk_transcript,
    parse_analysis_payload,
    render_analysis_markdown,
)
from meetbot.transcript.store import Utterance
from tests.conftest import FakeLLMClient


# --- payload parsing -------------------------------------------------------


def test_parse_full_payload(fake_llm: FakeLLMClient) -> None:
    analysis = parse_analysis_payload(
        fake_llm.json_response, model="m", provider="anthropic"
    )
    assert analysis.summary.startswith("The team reviewed")
    assert len(analysis.key_points) == 2
    assert len(analysis.action_items) == 2
    assert analysis.action_items[0].owner == "Speaker 1"
    assert analysis.action_items[1].owner is None
    assert analysis.sentiment == "mixed"
    assert analysis.model == "m"
    assert analysis.provider == "anthropic"


def test_parse_requires_a_summary() -> None:
    with pytest.raises(AnalysisError, match="no summary"):
        parse_analysis_payload({"key_points": ["x"]})


def test_parse_treats_placeholder_owners_as_unassigned() -> None:
    analysis = parse_analysis_payload(
        {
            "summary": "s",
            "action_items": [
                {"description": "a", "owner": "Unassigned"},
                {"description": "b", "owner": "  n/a  "},
                {"description": "c", "owner": "null"},
                {"description": "d", "owner": "Priya"},
            ],
        }
    )
    assert [item.owner for item in analysis.action_items] == [
        None,
        None,
        None,
        "Priya",
    ]


def test_parse_accepts_bare_string_action_items() -> None:
    analysis = parse_analysis_payload(
        {"summary": "s", "action_items": ["Send the deck", "  ", 42]}
    )
    assert [item.description for item in analysis.action_items] == ["Send the deck"]


def test_parse_defaults_missing_optional_fields() -> None:
    analysis = parse_analysis_payload({"summary": "only a summary"})
    assert analysis.key_points == []
    assert analysis.action_items == []
    assert analysis.sentiment == "neutral"
    assert analysis.sentiment_rationale == ""


def test_parse_keeps_unrecognised_sentiment_verbatim() -> None:
    analysis = parse_analysis_payload({"summary": "s", "sentiment": "Euphoric"})
    assert analysis.sentiment == "euphoric"


def test_parse_drops_blank_key_points() -> None:
    analysis = parse_analysis_payload(
        {"summary": "s", "key_points": ["real", "", "   ", None]}
    )
    assert analysis.key_points == ["real"]


# --- chunking --------------------------------------------------------------


def test_chunk_transcript_returns_single_chunk_when_short() -> None:
    assert chunk_transcript("a\nb\nc", chunk_chars=100) == ["a\nb\nc"]


def test_chunk_transcript_splits_on_line_boundaries() -> None:
    text = "\n".join(f"[00:00:{i:02d}] Speaker 0: line {i}" for i in range(40))
    chunks = chunk_transcript(text, chunk_chars=120)

    assert len(chunks) > 1
    assert "\n".join(chunks) == text  # nothing lost, nothing duplicated
    for chunk in chunks:
        for line in chunk.splitlines():
            assert line.startswith("[00:00:")


def test_chunk_transcript_on_empty_input() -> None:
    assert chunk_transcript("") == []


# --- analyze_transcript ----------------------------------------------------


def test_analyze_single_pass(utterances, fake_llm: FakeLLMClient) -> None:
    analysis = analyze_transcript(
        utterances, fake_llm, provider="anthropic", meeting_url="https://m/x"
    )

    assert isinstance(analysis, MeetingAnalysis)
    assert len(fake_llm.json_calls) == 1
    assert not fake_llm.text_calls  # no map phase for a short transcript

    prompt = fake_llm.json_calls[0]["user"]
    assert "migration script" in prompt
    assert "https://m/x" in prompt
    assert "Speaker 0, Speaker 1" in prompt
    assert fake_llm.json_calls[0]["schema"]["required"]


def test_analyze_empty_transcript_raises(fake_llm: FakeLLMClient) -> None:
    with pytest.raises(AnalysisError, match="empty"):
        analyze_transcript([], fake_llm)


def test_analyze_wraps_provider_errors(utterances, fake_llm: FakeLLMClient) -> None:
    fake_llm.json_error = LLMError("rate limited")
    with pytest.raises(AnalysisError, match="rate limited"):
        analyze_transcript(utterances, fake_llm)


def test_analyze_uses_map_reduce_for_long_transcripts(
    fake_llm: FakeLLMClient,
) -> None:
    long_meeting = [
        Utterance("Speaker 0", f"Point number {i} about the quarterly plan.", i, i + 1)
        for i in range(400)
    ]
    analysis = analyze_transcript(
        long_meeting, fake_llm, provider="anthropic", max_single_pass_chars=500
    )

    assert fake_llm.text_calls, "expected a map phase"
    assert len(fake_llm.json_calls) == 1
    final_prompt = fake_llm.json_calls[0]["user"]
    assert "NOTES FOR PART 1" in final_prompt
    assert analysis.summary


def test_map_phase_survives_a_failed_chunk(fake_llm: FakeLLMClient) -> None:
    """One bad chunk must not cost the whole analysis."""
    fake_llm.text_error = LLMError("chunk exploded")
    long_meeting = [
        Utterance("Speaker 0", f"Point {i} about the plan.", i, i + 1)
        for i in range(200)
    ]

    analysis = analyze_transcript(long_meeting, fake_llm, max_single_pass_chars=500)

    assert analysis.summary  # the reduce phase still ran
    assert "analysis failed" in fake_llm.json_calls[0]["user"]


# --- rendering -------------------------------------------------------------


def test_render_analysis_markdown_has_every_section(fake_llm: FakeLLMClient) -> None:
    analysis = parse_analysis_payload(
        fake_llm.json_response, model="claude-opus-5", provider="anthropic"
    )
    rendered = render_analysis_markdown(analysis, meta={"utterances": 5})

    assert "## Summary" in rendered
    assert "## Key Discussion Points" in rendered
    assert "## Action Items" in rendered
    assert "## Sentiment" in rendered
    assert "- [ ] **Speaker 1** - Write the migration script _(due: Friday)_" in rendered
    assert "- [ ] **Unassigned** - Re-baseline the API timeline" in rendered
    assert "anthropic / claude-opus-5" in rendered
    assert "- **Utterances:** 5" in rendered


def test_render_analysis_markdown_with_nothing_to_report() -> None:
    rendered = render_analysis_markdown(MeetingAnalysis(summary="Nothing happened."))
    assert "_None identified._" in rendered
    assert "_No action items were agreed in this meeting._" in rendered


def test_action_item_render() -> None:
    assert ActionItem("Ship it").render() == "- [ ] **Unassigned** - Ship it"
    assert (
        ActionItem("Ship it", "Ann", "Monday").render()
        == "- [ ] **Ann** - Ship it _(due: Monday)_"
    )


def test_analysis_to_dict_is_json_friendly(fake_llm: FakeLLMClient) -> None:
    analysis = parse_analysis_payload(fake_llm.json_response)
    payload = analysis.to_dict()
    assert isinstance(payload["action_items"][0], dict)
    assert payload["action_items"][0]["description"] == "Write the migration script"


# --- client construction ---------------------------------------------------


def test_build_client_rejects_unknown_provider() -> None:
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        build_client("mistral", "key", "model")


def test_build_client_requires_a_key() -> None:
    with pytest.raises(LLMError, match="No API key"):
        build_client("anthropic", "", "claude-opus-5")


class _RecordingOpenAIModule(types.ModuleType):
    """Stand-in for the ``openai`` package that records constructor kwargs."""

    def __init__(self) -> None:
        super().__init__("openai")
        self.kwargs: dict[str, object] = {}

        def _factory(**kwargs: object) -> object:
            self.kwargs = kwargs
            return object()

        self.OpenAI = _factory


@pytest.fixture()
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> _RecordingOpenAIModule:
    module = _RecordingOpenAIModule()
    monkeypatch.setitem(sys.modules, "openai", module)
    return module


def test_openai_client_defaults_to_the_official_endpoint(
    fake_openai: _RecordingOpenAIModule,
) -> None:
    build_client("openai", "key", "gpt-4o")
    assert fake_openai.kwargs["base_url"] is None


def test_openai_client_honours_a_compatible_base_url(
    fake_openai: _RecordingOpenAIModule,
) -> None:
    """Free-tier providers (Groq, OpenRouter, Ollama) ride the OpenAI wire
    format; the adapter must send the key to their endpoint, not OpenAI's."""
    build_client(
        "openai",
        "groq-key",
        "llama-3.3-70b-versatile",
        "https://api.groq.com/openai/v1",
    )
    assert fake_openai.kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert fake_openai.kwargs["api_key"] == "groq-key"


def test_anthropic_client_ignores_the_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base_url is an OpenAI-adapter concept; Anthropic must not silently
    accept it and appear to be pointed somewhere it is not."""
    module = types.ModuleType("anthropic")
    seen: dict[str, object] = {}

    def _factory(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    module.Anthropic = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)

    build_client("anthropic", "key", "claude-opus-5", "https://example.invalid")
    assert "base_url" not in seen


# --- the Gemini adapter ----------------------------------------------------


class _Reply:
    """The parts of a chat completion the adapter actually reads."""

    def __init__(self, content: str) -> None:
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


class _ScriptedOpenAIModule(types.ModuleType):
    """A fake ``openai`` that records requests and replays scripted answers.

    Each entry in ``script`` is either text to return or an exception to raise.
    The exceptions have to come from the module itself - the adapter catches
    ``openai.APIStatusError`` and friends - which is why a test takes the
    module first and fills in its script afterwards.
    """

    def __init__(self, script: list[object]) -> None:
        super().__init__("openai")
        self.script = list(script)
        self.requests: list[dict] = []
        self.kwargs: dict = {}

        class APIError(Exception):
            pass

        class APIStatusError(APIError):
            def __init__(self, status_code: int = 400) -> None:
                super().__init__(f"HTTP {status_code}")
                self.status_code = status_code

        class APIConnectionError(APIError):
            pass

        self.APIError = APIError
        self.APIStatusError = APIStatusError
        self.APIConnectionError = APIConnectionError
        self.AuthenticationError = type("AuthenticationError", (APIStatusError,), {})
        self.NotFoundError = type("NotFoundError", (APIStatusError,), {})
        self.RateLimitError = type("RateLimitError", (APIStatusError,), {})

        module = self

        def _create(**kwargs: object) -> object:
            module.requests.append(kwargs)
            answer = module.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return _Reply(str(answer))

        def _factory(**kwargs: object) -> object:
            module.kwargs = kwargs
            return types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=_create)
                )
            )

        self.OpenAI = _factory


def _scripted(
    monkeypatch: pytest.MonkeyPatch, *script: object
) -> _ScriptedOpenAIModule:
    module = _ScriptedOpenAIModule(list(script))
    monkeypatch.setitem(sys.modules, "openai", module)
    return module


#: A schema shaped like the real ones: ``additionalProperties`` and all.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def test_gemini_talks_to_googles_compatibility_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Gemini key needs no new SDK: Google serves the OpenAI wire format."""
    module = _scripted(monkeypatch)
    build_client("gemini", "g-key", "gemini-3.8-flash")
    assert module.kwargs["base_url"] == GEMINI_BASE_URL
    assert module.kwargs["api_key"] == "g-key"


def test_a_base_url_still_wins_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """So a proxy in front of Gemini stays configurable."""
    module = _scripted(monkeypatch)
    build_client("gemini", "g-key", "gemini-3.8-flash", "https://proxy.invalid/v1")
    assert module.kwargs["base_url"] == "https://proxy.invalid/v1"


def test_gemini_sends_the_output_cap_as_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It does not understand OpenAI's newer max_completion_tokens."""
    module = _scripted(monkeypatch, "fine")
    client = build_client("gemini", "g-key", "gemini-3.8-flash")
    assert client.complete_text(system="s", user="u", max_tokens=1234) == "fine"
    request = module.requests[0]
    assert request["max_tokens"] == 1234
    assert "max_completion_tokens" not in request


def test_openai_still_sends_max_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Gemini overrides must not leak into the OpenAI path."""
    module = _scripted(monkeypatch, "fine")
    client = build_client("openai", "key", "gpt-4o")
    client.complete_text(system="s", user="u", max_tokens=99)
    assert module.requests[0]["max_completion_tokens"] == 99


def test_gemini_asks_for_a_schema_without_strict_or_additional_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are refused by the compatibility layer; the rest of it still goes."""
    module = _scripted(monkeypatch, '{"answer": "yes"}')
    client = build_client("gemini", "g-key", "gemini-3.8-flash")
    assert client.complete_json(system="s", user="u", schema=SCHEMA) == {
        "answer": "yes"
    }
    sent = module.requests[0]["response_format"]
    assert sent["type"] == "json_schema"
    assert "strict" not in sent["json_schema"]
    assert "additionalProperties" not in sent["json_schema"]["schema"]
    assert sent["json_schema"]["schema"]["required"] == ["answer"]


def test_gemini_falls_back_to_json_mode_when_the_schema_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing schema enforcement beats losing the write-up entirely."""
    module = _scripted(monkeypatch)
    module.script = [module.APIStatusError(400), '{"answer": "yes"}']
    client = build_client("gemini", "g-key", "gemini-3.8-flash")

    assert client.complete_json(system="s", user="u", schema=SCHEMA) == {
        "answer": "yes"
    }

    assert len(module.requests) == 2
    retry = module.requests[1]
    assert retry["response_format"] == {"type": "json_object"}
    # The schema has to survive somewhere, so it goes into the prompt.
    asked = retry["messages"][1]["content"]
    assert "JSON Schema" in asked
    assert '"answer"' in asked


def test_gemini_does_not_retry_a_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a 429 spends the same quota to be told the same thing."""
    module = _scripted(monkeypatch)
    module.script = [module.RateLimitError(429)]
    client = build_client("gemini", "g-key", "gemini-3.8-flash")
    with pytest.raises(LLMError, match="rate limit"):
        client.complete_json(system="s", user="u", schema=SCHEMA)
    assert len(module.requests) == 1


def test_a_gemini_failure_does_not_blame_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Somebody who never configured OpenAI should not be told OpenAI failed."""
    module = _scripted(monkeypatch)
    module.script = [module.AuthenticationError(401)]
    client = build_client("gemini", "g-key", "gemini-3.8-flash")
    with pytest.raises(LLMError, match="Gemini rejected the API key"):
        client.complete_text(system="s", user="u")


class TestDecisions:
    """What the meeting settled, kept apart from what it assigned.

    Decisions previously had nowhere to go and ended up buried in the summary
    prose - yet "we agreed three weeks, not two" is often the single most
    useful line in a set of notes, and it frequently has no owner at all, so
    it is not an action item either.
    """

    @staticmethod
    def _analysis(**overrides):
        payload = {
            "summary": "The team reviewed the timeline.",
            "key_points": ["Timeline discussed"],
            "decisions": ["The pilot will run for three weeks, starting Monday"],
            "action_items": [],
            "sentiment": "positive",
            "sentiment_rationale": "Constructive.",
        }
        payload.update(overrides)
        return parse_analysis_payload(payload)

    def test_decisions_are_parsed(self) -> None:
        assert self._analysis().decisions == [
            "The pilot will run for three weeks, starting Monday"
        ]

    def test_decisions_are_rendered_under_their_own_heading(self) -> None:
        markdown = render_analysis_markdown(self._analysis())
        assert "## Decisions" in markdown
        assert "- The pilot will run for three weeks, starting Monday" in markdown

    def test_decisions_come_before_action_items(self) -> None:
        """What was settled reads first; what happens next follows from it."""
        markdown = render_analysis_markdown(self._analysis())
        assert markdown.index("## Decisions") < markdown.index("## Action Items")

    def test_a_meeting_that_decided_nothing_says_so(self) -> None:
        """Silence must not be dressed up as consensus."""
        markdown = render_analysis_markdown(self._analysis(decisions=[]))
        assert "_Nothing was decided in this meeting._" in markdown

    def test_a_missing_field_is_not_an_error(self) -> None:
        """An older model or a partial response must still produce notes."""
        payload = {
            "summary": "Something was discussed.",
            "key_points": [],
            "action_items": [],
            "sentiment": "neutral",
            "sentiment_rationale": "",
        }
        assert parse_analysis_payload(payload).decisions == []

    @pytest.mark.parametrize("junk", [None, "", "   ", 42, {"a": 1}, []])
    def test_unusable_entries_are_dropped(self, junk) -> None:
        analysis = self._analysis(decisions=["A real decision", junk])
        assert analysis.decisions == ["A real decision"]

    def test_the_schema_requires_decisions(self) -> None:
        """Structured output only guarantees a field the schema demands."""
        from meetbot.analysis.summarize import _ANALYSIS_SCHEMA

        assert "decisions" in _ANALYSIS_SCHEMA["properties"]
        assert "decisions" in _ANALYSIS_SCHEMA["required"]

    def test_the_prompt_forbids_restating_an_action_item(self) -> None:
        """Observed against the real model.

        Asked for decisions on a meeting that made none, it restated the
        action item there instead - which pads the notes and makes the
        meeting look more conclusive than it was.
        """
        from meetbot.analysis.summarize import _SYSTEM_PROMPT

        prompt = _SYSTEM_PROMPT.lower()
        assert "never record the same thing as both" in prompt
        assert "empty list is a correct answer" in prompt

    def test_serialises_for_the_transcript_store(self) -> None:
        assert self._analysis().to_dict()["decisions"] == [
            "The pilot will run for three weeks, starting Monday"
        ]
