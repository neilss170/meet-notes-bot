"""Shared pytest fixtures.

Nothing here touches the network, a browser, or the real environment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from meetbot.config import Config
from meetbot.transcript.store import Utterance


@pytest.fixture
def utterances() -> list[Utterance]:
    """A short two-speaker meeting."""
    return [
        Utterance("Speaker 0", "Morning everyone, let's start with the roadmap.", 0.0, 3.5),
        Utterance("Speaker 0", "I'll go first.", 3.6, 4.8),
        Utterance("Speaker 1", "Sounds good.", 6.0, 7.0),
        Utterance("Speaker 0", "We're two weeks behind on the API work.", 40.0, 44.0),
        Utterance("Speaker 1", "I'll take the migration script by Friday.", 45.0, 48.5),
    ]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A valid config that points every path at a temp directory."""
    return Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        bot_name="Scribe (Recording)",
        output_dir=tmp_path / "out",
        deepgram_api_key="dg-test-key",
        anthropic_api_key="sk-ant-test",
        bridge_port=18765,
    )


class FakeLLMClient:
    """A stand-in for a provider adapter.

    Records every call and returns queued responses, so the analysis module can
    be tested without an API key or a network.
    """

    def __init__(
        self,
        json_response: dict[str, Any] | None = None,
        text_responses: Sequence[str] | None = None,
        model: str = "fake-model-1",
    ) -> None:
        self.model = model
        self.json_response = json_response or {}
        self.text_responses = list(text_responses or [])
        self.json_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []
        self.json_error: Exception | None = None
        self.text_error: Exception | None = None

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.json_calls.append(kwargs)
        if self.json_error is not None:
            raise self.json_error
        return self.json_response

    def complete_text(self, **kwargs: Any) -> str:
        self.text_calls.append(kwargs)
        if self.text_error is not None:
            raise self.text_error
        if self.text_responses:
            return self.text_responses.pop(0)
        return f"notes for call {len(self.text_calls)}"


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    """A :class:`FakeLLMClient` preloaded with a well-formed analysis."""
    return FakeLLMClient(
        json_response={
            "summary": "The team reviewed the roadmap and flagged API slippage.",
            "key_points": [
                "API work is two weeks behind",
                "Migration script still outstanding",
            ],
            "action_items": [
                {
                    "description": "Write the migration script",
                    "owner": "Speaker 1",
                    "due": "Friday",
                },
                {
                    "description": "Re-baseline the API timeline",
                    "owner": None,
                    "due": None,
                },
            ],
            "sentiment": "mixed",
            "sentiment_rationale": "Constructive, but concerned about the delay.",
        }
    )
