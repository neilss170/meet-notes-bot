"""Tests for the LLM analysis module.

Every LLM call is mocked - these tests never require an API key or a network.
"""

from __future__ import annotations

import sys
import types

import pytest

from meetbot.analysis.llm import LLMError, build_client
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
        build_client("gemini", "key", "model")


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
