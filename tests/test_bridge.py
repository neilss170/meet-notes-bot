"""Tests for the audio bridge's frame decoding, routing and timeline mapping.

The Deepgram client is replaced with a stub, so nothing here opens a socket.
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest

from meetbot.capture import bridge as bridge_module
from meetbot.capture.bridge import MIXED_CHANNEL, AudioBridge
from meetbot.capture.deepgram import TranscriptSegment
from meetbot.config import Config
from meetbot.transcript.store import TranscriptStore, iter_records, read_utterances


class StubDeepgramClient:
    """Captures audio instead of sending it, and exposes the segment callback."""

    instances: list["StubDeepgramClient"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.on_segment = kwargs["on_segment"]
        self.on_connect = kwargs.get("on_connect")
        self.channel_label = kwargs.get("channel_label", "")
        self.audio: list[bytes] = []
        self.stopped = False
        StubDeepgramClient.instances.append(self)

    def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def stop(self) -> None:
        self.stopped = True

    async def run(self) -> None:
        if self.on_connect is not None:
            self.on_connect()
        # Stay alive until the bridge cancels or stops us.
        while not self.stopped:
            await asyncio.sleep(0.01)


@pytest.fixture
def stub_deepgram(monkeypatch):
    StubDeepgramClient.instances = []
    monkeypatch.setattr(bridge_module, "DeepgramLiveClient", StubDeepgramClient)
    return StubDeepgramClient


@pytest.fixture
def store(tmp_path: Path):
    with TranscriptStore(tmp_path / "transcript.jsonl") as store:
        yield store


def pcm_frame(channel: int, samples: list[int]) -> bytes:
    """Build a wire frame: one channel byte plus int16 LE samples."""
    return bytes([channel]) + struct.pack(f"<{len(samples)}h", *samples)


def make_bridge(store: TranscriptStore, **overrides) -> AudioBridge:
    config = Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        sample_rate=16_000,
        **overrides,
    )
    return AudioBridge(config, store)


# --- frame decoding --------------------------------------------------------


async def test_audio_frame_opens_a_channel_and_forwards_pcm(
    store, stub_deepgram
) -> None:
    bridge = make_bridge(store)
    bridge._started_at = 0.0

    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2, 3, 4]))

    assert len(stub_deepgram.instances) == 1
    client = stub_deepgram.instances[0]
    assert client.audio == [struct.pack("<4h", 1, 2, 3, 4)]
    assert client.kwargs["diarize"] is True
    assert client.kwargs["channel_label"] == "mixed"
    assert bridge.received_any_audio

    await bridge.stop()


async def test_per_participant_channels_disable_diarization(
    store, stub_deepgram
) -> None:
    bridge = make_bridge(store, per_participant_audio=True)
    bridge._started_at = 0.0

    bridge._handle_audio_frame(pcm_frame(1, [10, 20]))
    bridge._handle_audio_frame(pcm_frame(2, [30, 40]))

    assert len(stub_deepgram.instances) == 2
    for client in stub_deepgram.instances:
        assert client.kwargs["diarize"] is False
    assert stub_deepgram.instances[0].kwargs["default_speaker"] == "participant-1"

    await bridge.stop()


async def test_frames_route_to_their_own_channel(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._started_at = 0.0

    bridge._handle_audio_frame(pcm_frame(1, [1, 1]))
    bridge._handle_audio_frame(pcm_frame(2, [2, 2]))
    bridge._handle_audio_frame(pcm_frame(1, [3, 3]))

    first, second = stub_deepgram.instances
    assert len(first.audio) == 2
    assert len(second.audio) == 1

    await bridge.stop()


async def test_undersized_frames_are_discarded(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._started_at = 0.0

    bridge._handle_audio_frame(b"")
    bridge._handle_audio_frame(bytes([0]))
    bridge._handle_audio_frame(bytes([0, 0x01]))  # channel byte + half a sample

    assert not stub_deepgram.instances
    assert bridge.stats.decode_errors == 3
    assert not bridge.received_any_audio

    await bridge.stop()


async def test_odd_trailing_byte_is_trimmed_not_shifted(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._started_at = 0.0

    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [7, 8]) + b"\xff")

    assert stub_deepgram.instances[0].audio == [struct.pack("<2h", 7, 8)]

    await bridge.stop()


# --- control messages ------------------------------------------------------


async def test_channel_label_is_applied_when_the_channel_opens(
    store, stub_deepgram
) -> None:
    bridge = make_bridge(store, per_participant_audio=True)
    bridge._started_at = 0.0

    bridge._handle_control_message(json.dumps({"type": "channel", "id": 1, "label": "Dana"}))
    bridge._handle_audio_frame(pcm_frame(1, [1, 2]))

    assert stub_deepgram.instances[0].kwargs["default_speaker"] == "Dana"

    await bridge.stop()


async def test_relabelling_an_open_channel(store, stub_deepgram) -> None:
    bridge = make_bridge(store, per_participant_audio=True)
    bridge._started_at = 0.0

    bridge._handle_audio_frame(pcm_frame(1, [1, 2]))
    bridge.set_channel_label(1, "Priya")

    assert bridge._channels[1].label == "Priya"

    await bridge.stop()


async def test_malformed_control_messages_are_survivable(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._handle_control_message("not json")
    bridge._handle_control_message("[1, 2, 3]")
    bridge._handle_control_message(json.dumps({"type": "unknown"}))
    await bridge.stop()


async def test_track_ended_is_recorded_as_an_event(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._handle_control_message(
        json.dumps({"type": "track_ended", "trackId": "abc"})
    )
    await bridge.stop()
    store.close()

    kinds = [
        r["kind"] for r in iter_records(store.path) if r["type"] == "event"
    ]
    assert "track_ended" in kinds


# --- transcript timeline ---------------------------------------------------


async def test_segments_are_written_with_meeting_relative_times(
    store, stub_deepgram
) -> None:
    bridge = make_bridge(store)
    # Pretend the channel's first audio arrived 30 s into the meeting.
    bridge._started_at = bridge_module.time.monotonic() - 30.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))

    bridge._record_segment(
        MIXED_CHANNEL,
        TranscriptSegment(speaker="Speaker 1", text="hello", start=2.0, end=3.5),
    )
    await bridge.stop()
    store.close()

    utterances = read_utterances(store.path)
    assert len(utterances) == 1
    assert utterances[0].speaker == "Speaker 1"
    assert utterances[0].start == pytest.approx(32.0, abs=0.5)
    assert utterances[0].end == pytest.approx(33.5, abs=0.5)
    assert utterances[0].channel == "mixed"


async def test_reconnect_rebases_timestamps_onto_the_meeting_timeline(
    store, stub_deepgram
) -> None:
    """Deepgram restarts its clock at zero after a reconnect."""
    bridge = make_bridge(store)
    bridge._started_at = 0.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [0] * 16_000))  # 1.0 s

    state = bridge._channels[MIXED_CHANNEL]
    state.meeting_offset_s = 0.0
    assert state.seconds_forwarded == pytest.approx(1.0)

    # Simulate a reconnect: the stub's on_connect rebases the epoch.
    stub_deepgram.instances[0].on_connect()
    bridge._record_segment(
        MIXED_CHANNEL,
        TranscriptSegment(speaker="Speaker 0", text="after reconnect", start=0.5,
                          end=1.0),
    )
    await bridge.stop()
    store.close()

    utterance = read_utterances(store.path)[0]
    # 1.0 s already forwarded + 0.5 s into the new stream.
    assert utterance.start == pytest.approx(1.5)


async def test_per_participant_speaker_label_comes_from_the_channel(
    store, stub_deepgram
) -> None:
    bridge = make_bridge(store, per_participant_audio=True)
    bridge._started_at = 0.0
    bridge._handle_control_message(
        json.dumps({"type": "channel", "id": 3, "label": "Dana"})
    )
    bridge._handle_audio_frame(pcm_frame(3, [1, 2]))

    bridge._record_segment(
        3, TranscriptSegment(speaker="Speaker 0", text="mine", start=0.0, end=1.0)
    )
    await bridge.stop()
    store.close()

    assert read_utterances(store.path)[0].speaker == "Dana"


async def test_segment_for_an_unknown_channel_is_dropped(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._record_segment(
        99, TranscriptSegment(speaker="Speaker 0", text="orphan", start=0.0, end=1.0)
    )
    await bridge.stop()
    store.close()

    assert read_utterances(store.path) == []


async def test_channel_open_is_recorded_as_an_event(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._started_at = 0.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))
    await bridge.stop()
    store.close()

    events = [r for r in iter_records(store.path) if r["type"] == "event"]
    assert any(e["kind"] == "channel_opened" for e in events)


async def test_stop_flushes_every_channel(store, stub_deepgram) -> None:
    bridge = make_bridge(store, per_participant_audio=True)
    bridge._started_at = 0.0
    bridge._handle_audio_frame(pcm_frame(1, [1, 2]))
    bridge._handle_audio_frame(pcm_frame(2, [3, 4]))

    await bridge.stop()

    assert all(client.stopped for client in stub_deepgram.instances)


async def test_frames_after_stop_do_not_open_new_channels(store, stub_deepgram) -> None:
    bridge = make_bridge(store)
    bridge._started_at = 0.0
    await bridge.stop()

    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))

    assert not stub_deepgram.instances


async def test_caption_names_replace_diarization_labels(store, stub_deepgram) -> None:
    """A resolved caption name wins over Deepgram's generic speaker index."""
    bridge = make_bridge(store)
    bridge._started_at = bridge_module.time.monotonic() - 30.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))
    bridge.speaker_resolver = lambda start, end: "Neil Sharma"

    bridge._record_segment(
        MIXED_CHANNEL,
        TranscriptSegment(speaker="Speaker 1", text="hello", start=2.0, end=3.5),
    )
    await bridge.stop()
    store.close()

    assert read_utterances(store.path)[0].speaker == "Neil Sharma"


async def test_unresolved_captions_leave_the_generic_label(store, stub_deepgram) -> None:
    """No caption match must degrade to Speaker N, not to a blank or a guess."""
    bridge = make_bridge(store)
    bridge._started_at = bridge_module.time.monotonic() - 30.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))
    bridge.speaker_resolver = lambda start, end: None

    bridge._record_segment(
        MIXED_CHANNEL,
        TranscriptSegment(speaker="Speaker 1", text="hello", start=2.0, end=3.5),
    )
    await bridge.stop()
    store.close()

    assert read_utterances(store.path)[0].speaker == "Speaker 1"


async def test_a_failing_resolver_never_stops_capture(store, stub_deepgram) -> None:
    """Speaker naming is an enhancement; it must not cost us the utterance."""
    bridge = make_bridge(store)
    bridge._started_at = bridge_module.time.monotonic() - 30.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))

    def boom(start: float, end: float) -> str:
        raise RuntimeError("caption panel exploded")

    bridge.speaker_resolver = boom
    bridge._record_segment(
        MIXED_CHANNEL,
        TranscriptSegment(speaker="Speaker 1", text="hello", start=2.0, end=3.5),
    )
    await bridge.stop()
    store.close()

    utterances = read_utterances(store.path)
    assert len(utterances) == 1
    assert utterances[0].speaker == "Speaker 1"
    assert utterances[0].text == "hello"


async def test_meeting_elapsed_shares_the_utterance_clock(store, stub_deepgram) -> None:
    """The resolver's clock and Utterance.start must be the same timeline."""
    bridge = make_bridge(store)
    assert bridge.meeting_elapsed() == 0.0  # not started yet
    bridge._started_at = bridge_module.time.monotonic() - 12.0
    assert bridge.meeting_elapsed() == pytest.approx(12.0, abs=0.5)


async def test_one_caption_match_names_a_speakers_whole_meeting(
    store, stub_deepgram
) -> None:
    """The point of the registry, end to end.

    Utterance 1 gets a caption match. Utterances 2 and 3 have no caption
    overlap at all, but carry the same diarization id, so they take the same
    name instead of falling back to "Speaker 0".
    """
    bridge = make_bridge(store)
    bridge._started_at = bridge_module.time.monotonic() - 30.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))

    # Captions resolve only the first two windows; enough to clear min_votes.
    resolved = {(0.0, 1.0): "Neil Sharma", (1.0, 2.0): "Neil Sharma"}
    bridge.speaker_resolver = lambda start, end: resolved.get(
        (round(start - 30.0, 3), round(end - 30.0, 3))
    )

    for start, end in ((0.0, 1.0), (1.0, 2.0), (5.0, 6.0), (9.0, 10.0)):
        bridge._record_segment(
            MIXED_CHANNEL,
            TranscriptSegment(
                speaker="Speaker 0", text=f"line {start}", start=start, end=end
            ),
        )
    await bridge.stop()
    store.close()

    speakers = [u.speaker for u in read_utterances(store.path)]
    assert speakers == ["Neil Sharma"] * 4, (
        f"unmatched utterances kept a generic label: {speakers}"
    )
    assert bridge.speaker_registry.mapping() == {"Speaker 0": "Neil Sharma"}


async def test_an_unlearned_speaker_still_falls_back(store, stub_deepgram) -> None:
    """Learning one speaker must not put their name on everyone else."""
    bridge = make_bridge(store)
    bridge._started_at = bridge_module.time.monotonic() - 30.0
    bridge._handle_audio_frame(pcm_frame(MIXED_CHANNEL, [1, 2]))
    bridge.speaker_resolver = lambda start, end: (
        "Neil Sharma" if start < 32.5 else None
    )

    for speaker, start, end in (
        ("Speaker 0", 0.0, 1.0),
        ("Speaker 0", 1.0, 2.0),
        ("Speaker 1", 5.0, 6.0),
    ):
        bridge._record_segment(
            MIXED_CHANNEL,
            TranscriptSegment(speaker=speaker, text="x", start=start, end=end),
        )
    await bridge.stop()
    store.close()

    speakers = [u.speaker for u in read_utterances(store.path)]
    assert speakers == ["Neil Sharma", "Neil Sharma", "Speaker 1"]
