"""Tests for Deepgram result parsing and stream-URL construction.

These cover the pure functions only - the socket lifecycle is exercised
manually against the live API.
"""

from __future__ import annotations

import json
import types
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.exceptions import InvalidStatus

from meetbot.capture import deepgram
from meetbot.capture.deepgram import build_stream_url, parse_results_message


def _results(words, *, is_final=True, transcript=None, confidence=0.9):
    text = transcript if transcript is not None else " ".join(w["word"] for w in words)
    return {
        "type": "Results",
        "is_final": is_final,
        "channel": {
            "alternatives": [
                {"transcript": text, "confidence": confidence, "words": words}
            ]
        },
    }


def test_build_stream_url_sets_our_options() -> None:
    url = build_stream_url(
        model="nova-3", language="en-US", sample_rate=16_000, diarize=True
    )
    params = parse_qs(urlparse(url).query)

    assert urlparse(url).scheme == "wss"
    assert params["encoding"] == ["linear16"]
    assert params["sample_rate"] == ["16000"]
    assert params["model"] == ["nova-3"]
    assert params["diarize"] == ["true"]
    # We persist finals only; interim hypotheses would just churn the JSONL.
    assert params["interim_results"] == ["false"]


def test_build_stream_url_can_disable_diarization() -> None:
    url = build_stream_url(
        model="nova-3", language="en-US", sample_rate=16_000, diarize=False
    )
    assert parse_qs(urlparse(url).query)["diarize"] == ["false"]


def test_interim_results_are_ignored() -> None:
    payload = _results([{"word": "hello", "start": 0.0, "end": 0.5}], is_final=False)
    assert parse_results_message(payload, default_speaker="Speaker 0") == []


def test_empty_transcript_is_ignored() -> None:
    payload = _results([], transcript="   ")
    assert parse_results_message(payload, default_speaker="Speaker 0") == []


def test_missing_alternatives_are_ignored() -> None:
    payload = {"type": "Results", "is_final": True, "channel": {"alternatives": []}}
    assert parse_results_message(payload, default_speaker="Speaker 0") == []


def test_single_speaker_produces_one_segment() -> None:
    payload = _results(
        [
            {"word": "hello", "punctuated_word": "Hello,", "start": 0.0, "end": 0.4,
             "speaker": 0, "confidence": 0.98},
            {"word": "team", "punctuated_word": "team.", "start": 0.4, "end": 0.9,
             "speaker": 0, "confidence": 0.92},
        ]
    )
    segments = parse_results_message(payload, default_speaker="Speaker 0")

    assert len(segments) == 1
    assert segments[0].speaker == "Speaker 0"
    assert segments[0].text == "Hello, team."
    assert segments[0].start == 0.0
    assert segments[0].end == 0.9
    assert segments[0].confidence is not None


def test_diarized_result_splits_on_speaker_change() -> None:
    payload = _results(
        [
            {"word": "hi", "start": 0.0, "end": 0.3, "speaker": 0},
            {"word": "there", "start": 0.3, "end": 0.6, "speaker": 0},
            {"word": "hello", "start": 0.8, "end": 1.1, "speaker": 1},
            {"word": "again", "start": 3.0, "end": 3.4, "speaker": 0},
        ]
    )
    segments = parse_results_message(payload, default_speaker="Speaker 0")

    assert [s.speaker for s in segments] == ["Speaker 0", "Speaker 1", "Speaker 0"]
    assert [s.text for s in segments] == ["hi there", "hello", "again"]
    assert segments[0].start == 0.0
    assert segments[0].end == 0.6
    assert segments[2].start == 3.0


def test_words_without_speaker_ids_use_the_default_label() -> None:
    """Per-participant capture runs with diarization off - no speaker field."""
    payload = _results(
        [
            {"word": "just", "start": 0.0, "end": 0.2},
            {"word": "me", "start": 0.2, "end": 0.5},
        ]
    )
    segments = parse_results_message(payload, default_speaker="Priya")

    assert len(segments) == 1
    assert segments[0].speaker == "Priya"
    assert segments[0].text == "just me"


def test_result_without_word_timings_still_yields_a_segment() -> None:
    payload = {
        "type": "Results",
        "is_final": True,
        "start": 12.0,
        "duration": 2.5,
        "channel": {"alternatives": [{"transcript": "no word list", "words": []}]},
    }
    segments = parse_results_message(payload, default_speaker="Speaker 0")

    assert len(segments) == 1
    assert segments[0].text == "no word list"
    assert segments[0].start == 12.0
    assert segments[0].end == 14.5


def test_punctuated_word_is_preferred() -> None:
    payload = _results(
        [{"word": "yes", "punctuated_word": "Yes!", "start": 0.0, "end": 0.3,
          "speaker": 0}]
    )
    segments = parse_results_message(payload, default_speaker="Speaker 0")
    assert segments[0].text == "Yes!"


def test_blank_words_are_skipped() -> None:
    payload = _results(
        [
            {"word": "", "start": 0.0, "end": 0.1, "speaker": 0},
            {"word": "real", "start": 0.1, "end": 0.4, "speaker": 0},
        ]
    )
    segments = parse_results_message(payload, default_speaker="Speaker 0")
    assert [s.text for s in segments] == ["real"]


# --- credential probe ------------------------------------------------------


class _FakeSocket:
    """Minimal stand-in for a Deepgram websocket, replaying scripted frames."""

    def __init__(self, frames: list[object]) -> None:
        self._frames = list(frames)
        self.sent: list[object] = []

    async def __aenter__(self) -> "_FakeSocket":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, data: object) -> None:
        self.sent.append(data)

    async def recv(self) -> object:
        if not self._frames:
            raise AssertionError("probe read more frames than were scripted")
        return self._frames.pop(0)


def _patch_connect(monkeypatch, result):
    def _connect(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(deepgram.websockets, "connect", _connect)


async def test_probe_accepts_a_key_that_returns_results(monkeypatch) -> None:
    socket = _FakeSocket([json.dumps({"type": "Results"})])
    _patch_connect(monkeypatch, socket)

    detail = await deepgram.probe_credentials(
        "key", model="nova-3", language="en-US"
    )

    assert "key accepted" in detail
    # Audio must actually be sent, then the stream closed - a probe that only
    # opened a socket would pass against a key with no streaming scope.
    assert any(isinstance(frame, bytes) for frame in socket.sent)
    assert json.loads(socket.sent[-1])["type"] == "CloseStream"


async def test_probe_accepts_a_metadata_only_close(monkeypatch) -> None:
    _patch_connect(monkeypatch, _FakeSocket([json.dumps({"type": "Metadata"})]))
    detail = await deepgram.probe_credentials("k", model="nova-3", language="en-US")
    assert "closed cleanly" in detail


async def test_probe_skips_binary_frames(monkeypatch) -> None:
    """Deepgram may send binary keepalives; they are not results."""
    _patch_connect(
        monkeypatch,
        _FakeSocket([b"\x00\x01", json.dumps({"type": "Results"})]),
    )
    detail = await deepgram.probe_credentials("k", model="nova-3", language="en-US")
    assert "key accepted" in detail


async def test_probe_reports_a_rejected_key(monkeypatch) -> None:
    response = types.SimpleNamespace(status_code=401)
    _patch_connect(monkeypatch, InvalidStatus(response))  # type: ignore[arg-type]

    with pytest.raises(deepgram.DeepgramError, match="rejected the API key"):
        await deepgram.probe_credentials("bad", model="nova-3", language="en-US")


async def test_probe_reports_a_bad_model_language_pair(monkeypatch) -> None:
    response = types.SimpleNamespace(status_code=400)
    _patch_connect(monkeypatch, InvalidStatus(response))  # type: ignore[arg-type]

    with pytest.raises(deepgram.DeepgramError, match="valid pair"):
        await deepgram.probe_credentials("k", model="nope", language="xx")


async def test_probe_reports_an_unreachable_host(monkeypatch) -> None:
    _patch_connect(monkeypatch, OSError("name resolution failed"))

    with pytest.raises(deepgram.DeepgramError, match="Could not reach Deepgram"):
        await deepgram.probe_credentials("k", model="nova-3", language="en-US")


class TestTranscriptionQuality:
    """Settings that decide how readable the transcript is.

    Endpointing was 300ms - shorter than an ordinary pause for breath - so
    utterances were cut mid-sentence. That reads badly and also costs
    accuracy, because the model loses the surrounding context it uses to
    choose words and place punctuation.
    """

    @staticmethod
    def _params(**kwargs):
        from urllib.parse import parse_qsl, urlparse

        from meetbot.capture.deepgram import build_stream_url

        defaults = dict(model="nova-3", language="en-US", sample_rate=16000, diarize=True)
        defaults.update(kwargs)
        return dict(parse_qsl(urlparse(build_stream_url(**defaults)).query))

    def test_punctuation_and_formatting_are_requested(self) -> None:
        params = self._params()
        assert params["punctuate"] == "true"
        assert params["smart_format"] == "true"

    def test_endpointing_survives_an_ordinary_pause(self) -> None:
        params = self._params()
        assert int(params["endpointing"]) >= 500, (
            "too short: sentences get cut in half mid-phrase"
        )

    def test_an_utterance_ceiling_is_set(self) -> None:
        """Continuous speech must still break into readable turns."""
        params = self._params()
        assert int(params["utterance_end_ms"]) > 0

    def test_the_tuning_is_overridable(self) -> None:
        params = self._params(endpointing_ms=1500, utterance_end_ms=2000)
        assert params["endpointing"] == "1500"
        assert params["utterance_end_ms"] == "2000"
