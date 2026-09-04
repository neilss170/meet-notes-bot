"""Integration tests for the run orchestration.

The browser and the Deepgram bridge are replaced with fakes, so these run
offline. What they pin down is the ordering guarantee the runner exists to
provide: the raw transcript is written and closed *before* the analysis step,
so an analysis failure never costs you the transcript.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meetbot import runner as runner_module
from meetbot.analysis.summarize import AnalysisError, MeetingAnalysis
from meetbot.config import Config
from meetbot.join import EndReason, JoinError, JoinOutcome, MeetingState
from meetbot.runner import meeting_code, run_meeting
from meetbot.transcript.store import Utterance, iter_records, read_utterances


class FakeSession:
    """Stands in for :class:`meetbot.join.MeetSession`."""

    join_error: Exception | None = None
    end_reason = EndReason.MEETING_ENDED

    def __init__(self, config: Config, *, on_event=None) -> None:
        self.config = config
        self.on_event = on_event
        self.started = False
        self.closed = False
        self.left = False

    async def start(self) -> None:
        self.started = True

    async def join(self):
        if type(self).join_error is not None:
            raise type(self).join_error
        if self.on_event:
            self.on_event("joined", {"display_name": self.config.bot_name})
        return JoinOutcome.ADMITTED

    async def wait_until_meeting_ends(self, *, on_poll=None):
        if on_poll is not None:
            result = on_poll(MeetingState(in_call=True, participant_count=3, ended=False))
            if hasattr(result, "__await__"):
                await result
        return type(self).end_reason

    async def leave(self) -> None:
        self.left = True

    async def close(self) -> None:
        self.closed = True

    def request_stop(self) -> None:
        pass

    async def capture_stats(self):
        return {"connected": True, "channels": []}


class FakeBridge:
    """Stands in for :class:`meetbot.capture.bridge.AudioBridge`."""

    utterances: list[Utterance] = []

    def __init__(self, config: Config, store, on_utterance=None) -> None:
        self.config = config
        self.store = store
        self.on_utterance = on_utterance
        self.stopped = False
        self.fatal_error = None

    async def start(self) -> None:
        # Simulate live transcription landing in the store during the call.
        for utterance in type(self).utterances:
            self.store.append(utterance)

    async def stop(self) -> None:
        self.stopped = True

    @property
    def received_any_audio(self) -> bool:
        return bool(type(self).utterances)


@pytest.fixture
def fakes(monkeypatch):
    FakeSession.join_error = None
    FakeSession.end_reason = EndReason.MEETING_ENDED
    FakeBridge.utterances = [
        Utterance("Speaker 0", "Let's review the roadmap.", 0.0, 3.0),
        Utterance("Speaker 1", "I'll own the migration by Friday.", 4.0, 8.0),
    ]
    monkeypatch.setattr(runner_module, "MeetSession", FakeSession)
    monkeypatch.setattr(runner_module, "AudioBridge", FakeBridge)
    return FakeSession, FakeBridge


@pytest.fixture
def run_config(tmp_path: Path) -> Config:
    return Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        bridge_port=18999,
    )


def _stub_analysis(monkeypatch, result=None, error=None):
    def fake(config, utterances, duration):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(runner_module, "_analyse_sync", fake)


async def test_happy_path_writes_every_artifact(fakes, run_config, monkeypatch) -> None:
    _stub_analysis(
        monkeypatch,
        result=MeetingAnalysis(
            summary="Roadmap reviewed.",
            key_points=["Migration is the critical path"],
            model="claude-opus-5",
            provider="anthropic",
        ),
    )

    run = await run_meeting(run_config)

    assert run.succeeded
    assert run.end_reason is EndReason.MEETING_ENDED
    assert run.utterance_count == 2
    assert not run.errors

    assert run.transcript_jsonl.exists()
    assert run.transcript_markdown is not None and run.transcript_markdown.exists()
    assert run.analysis_markdown is not None and run.analysis_markdown.exists()

    assert "migration" in run.transcript_markdown.read_text(encoding="utf-8").lower()
    assert "Roadmap reviewed." in run.analysis_markdown.read_text(encoding="utf-8")


async def test_transcript_survives_a_failed_analysis(
    fakes, run_config, monkeypatch
) -> None:
    """The whole point of running analysis last."""
    _stub_analysis(monkeypatch, error=AnalysisError("the model was unreachable"))

    run = await run_meeting(run_config)

    assert run.utterance_count == 2
    assert run.transcript_jsonl.exists()
    assert run.transcript_markdown is not None and run.transcript_markdown.exists()
    assert run.analysis_markdown is None
    assert any("the model was unreachable" in error for error in run.errors)
    # The transcript is complete and re-analysable.
    assert len(read_utterances(run.transcript_jsonl)) == 2


async def test_unexpected_analysis_error_is_also_contained(
    fakes, run_config, monkeypatch
) -> None:
    _stub_analysis(monkeypatch, error=RuntimeError("boom"))

    run = await run_meeting(run_config)

    assert run.utterance_count == 2
    assert run.transcript_markdown is not None
    assert any("boom" in error for error in run.errors)


async def test_analysis_is_skipped_when_disabled(fakes, run_config, monkeypatch) -> None:
    called = False

    def fake(*args, **kwargs):
        nonlocal called
        called = True
        return MeetingAnalysis(summary="x")

    monkeypatch.setattr(runner_module, "_analyse_sync", fake)

    run = await run_meeting(run_config.override(analysis_enabled=False))

    assert not called
    assert run.analysis_markdown is None
    assert run.transcript_markdown is not None


async def test_failed_join_still_returns_a_result(fakes, run_config) -> None:
    FakeSession.join_error = JoinError(JoinOutcome.DENIED, "host said no")

    run = await run_meeting(run_config)

    assert not run.succeeded
    assert any("host said no" in error for error in run.errors)
    assert run.transcript_jsonl.exists()

    kinds = [r.get("kind") for r in iter_records(run.transcript_jsonl)
             if r["type"] == "event"]
    assert "join_failed" in kinds


async def test_empty_capture_is_reported_and_analysis_skipped(
    fakes, run_config, monkeypatch
) -> None:
    FakeBridge.utterances = []
    _stub_analysis(monkeypatch, result=MeetingAnalysis(summary="never called"))

    run = await run_meeting(run_config)

    assert run.utterance_count == 0
    assert run.analysis_markdown is None
    assert any("No audio ever reached the bridge" in e for e in run.errors)
    assert any("transcript is empty" in e for e in run.errors)


async def test_lifecycle_events_are_recorded(fakes, run_config, monkeypatch) -> None:
    _stub_analysis(monkeypatch, result=MeetingAnalysis(summary="s"))

    run = await run_meeting(run_config)

    kinds = [r.get("kind") for r in iter_records(run.transcript_jsonl)
             if r["type"] == "event"]
    assert "joined" in kinds
    assert "participant_count" in kinds
    assert "meeting_ended" in kinds


async def test_meta_record_captures_the_meeting(fakes, run_config, monkeypatch) -> None:
    _stub_analysis(monkeypatch, result=MeetingAnalysis(summary="s"))

    run = await run_meeting(run_config)

    meta = next(r for r in iter_records(run.transcript_jsonl) if r["type"] == "meta")
    assert meta["meet_url"] == run_config.meet_url
    assert "Recording" in meta["bot_name"]


async def test_output_directory_is_named_after_the_meeting(
    fakes, run_config, monkeypatch
) -> None:
    _stub_analysis(monkeypatch, result=MeetingAnalysis(summary="s"))

    run = await run_meeting(run_config)

    assert run.output_dir.name.endswith("-abc-defg-hij")
    assert (run.output_dir / "meetbot.log").exists()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://meet.google.com/abc-defg-hij", "abc-defg-hij"),
        ("https://meet.google.com/lookup/team-standup", "team-standup"),
        ("https://meet.google.com/", "meeting"),
    ],
)
def test_meeting_code(url: str, expected: str) -> None:
    assert meeting_code(url) == expected
