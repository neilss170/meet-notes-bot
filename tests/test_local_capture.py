"""Tests for recording this machine instead of sending a bot.

No real audio device is opened here. PortAudio is replaced with a fake, so
these run on CI's Linux boxes where WASAPI does not exist at all - the parts
that genuinely need hardware are exercised by hand against a real machine.

The signal processing *is* tested for real, because a resampler that quietly
aliases produces a transcript full of plausible-looking nonsense rather than
an error anyone would notice.
"""

from __future__ import annotations

from typing import Any

import pytest

from meetbot.capture.deepgram import build_stream_url, parse_results_message
from meetbot.capture.local import (
    CHANNEL_COUNT,
    TARGET_RATE,
    AudioDevice,
    LocalCaptureError,
    LocalRecorder,
)
from meetbot.local_runner import CHANNEL_SPEAKERS, SELF_SPEAKER, slugify

np = pytest.importorskip("numpy", reason="numpy is needed to resample audio")

SPEAKERS = AudioDevice(
    index=10, name="Speaker (Realtek) [Loopback]", channels=2, rate=48_000,
    is_loopback=True,
)
MIC = AudioDevice(
    index=9, name="Microphone Array", channels=2, rate=48_000, is_loopback=False,
)


def tone(hz: float, seconds: float, rate: int, channels: int = 1) -> bytes:
    """Interleaved int16 PCM of a sine wave."""
    t = np.arange(int(rate * seconds)) / rate
    mono = (0.5 * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)
    if channels == 1:
        return mono.tobytes()
    return np.repeat(mono[:, None], channels, axis=1).ravel().tobytes()


def dominant_hz(pcm: bytes, rate: int) -> float:
    """The strongest frequency in a mono int16 buffer."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if samples.size < 16:
        return 0.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    return float(np.fft.rfftfreq(samples.size, 1 / rate)[int(np.argmax(spectrum))])


# --- resampling ------------------------------------------------------------


class TestResampler:
    @staticmethod
    def _feed(resampler, pcm: bytes, chunk: int = 4096) -> bytes:
        """Push audio through in blocks, as a device delivers it."""
        return b"".join(
            resampler.process(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk)
        )

    def test_converts_48k_stereo_to_16k_mono(self) -> None:
        from meetbot.capture.local import Resampler

        out = self._feed(Resampler(48_000, 2), tone(1000, 1.0, 48_000, 2))
        samples = np.frombuffer(out, dtype=np.int16)
        assert abs(samples.size - TARGET_RATE) < TARGET_RATE * 0.02

    def test_preserves_the_signal(self) -> None:
        from meetbot.capture.local import Resampler

        out = self._feed(Resampler(48_000, 2), tone(1000, 1.0, 48_000, 2))
        assert abs(dominant_hz(out, TARGET_RATE) - 1000) < 15

    def test_filters_rather_than_aliases(self) -> None:
        """The bug this guards is silent and ruinous.

        Decimating 48 kHz to 16 kHz without a low-pass folds everything above
        8 kHz back into the speech band. A 12 kHz tone would reappear at
        4 kHz - inaudible as an error, but it turns speech into noise the
        recogniser then transcribes as confident nonsense.
        """
        from meetbot.capture.local import Resampler

        out = self._feed(Resampler(48_000, 1), tone(12_000, 1.0, 48_000, 1))
        samples = np.frombuffer(out, dtype=np.int16).astype(np.float64)
        rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
        assert rms < 500, f"12 kHz survived decimation at rms {rms:.0f}"

    def test_block_size_does_not_change_the_result(self) -> None:
        """Filter state has to carry across blocks.

        Without it every block boundary is a discontinuity - a click every
        20 ms or so, which is exactly the kind of fault that sounds like a
        bad microphone rather than a bug.
        """
        from meetbot.capture.local import Resampler

        audio = tone(1000, 0.5, 48_000, 1)
        big = self._feed(Resampler(48_000, 1), audio, chunk=48_000)
        small = self._feed(Resampler(48_000, 1), audio, chunk=2048)
        # Lengths agree to within a sample or two of rounding.
        assert abs(len(big) - len(small)) <= 8
        assert abs(dominant_hz(big, TARGET_RATE) - dominant_hz(small, TARGET_RATE)) < 5

    def test_a_matching_rate_is_passed_through(self) -> None:
        from meetbot.capture.local import Resampler

        audio = tone(500, 0.2, TARGET_RATE, 1)
        out = Resampler(TARGET_RATE, 1).process(audio)
        assert len(out) == len(audio)

    def test_empty_input_is_not_an_error(self) -> None:
        from meetbot.capture.local import Resampler

        assert Resampler(48_000, 2).process(b"") == b""


# --- the recorder ----------------------------------------------------------


class _FakeStream:
    def __init__(self) -> None:
        self.closed = False
        self.stopped = False

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakePyAudio:
    """Stands in for PortAudio: records what was opened, opens nothing."""

    paInt16 = 8

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []
        self.terminated = False

    def PyAudio(self) -> "_FakePyAudio":  # noqa: N802 - mirrors the real API
        return self

    def open(self, **kwargs: Any) -> _FakeStream:
        self.opened.append(kwargs)
        stream = _FakeStream()
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def fake_portaudio(monkeypatch) -> _FakePyAudio:
    fake = _FakePyAudio()
    monkeypatch.setattr("meetbot.capture.local._pyaudio", lambda: fake)
    return fake


class TestLocalRecorder:
    def test_recording_needs_something_to_listen_to(self) -> None:
        """Without the speakers there is no meeting, only your own half."""
        with pytest.raises(LocalCaptureError, match="nothing to record"):
            LocalRecorder(None)

    def test_opens_both_devices(self, fake_portaudio) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        try:
            indexes = [o["input_device_index"] for o in fake_portaudio.opened]
            assert sorted(indexes) == [9, 10]
            # Callback mode, not a read loop: creating PyAudio on our own
            # threads segfaults, because WASAPI wants COM per thread.
            assert all(o.get("stream_callback") for o in fake_portaudio.opened)
        finally:
            recorder.stop()

    def test_works_without_a_microphone(self, fake_portaudio) -> None:
        recorder = LocalRecorder(SPEAKERS, None)
        recorder.start()
        try:
            assert len(fake_portaudio.opened) == 1
            assert fake_portaudio.opened[0]["input_device_index"] == 10
        finally:
            recorder.stop()

    def test_stop_releases_the_devices(self, fake_portaudio) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        recorder.stop()
        assert all(s.closed for s in fake_portaudio.streams)
        assert fake_portaudio.terminated

    def test_stop_is_idempotent(self, fake_portaudio) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        recorder.stop()
        recorder.stop()

    def test_the_speakers_land_on_their_own_channel(self, fake_portaudio) -> None:
        """Channel 1 is everyone else; channel 0 is you. Never mixed."""
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        try:
            # Feed the loopback source directly, as its callback would.
            recorder._them.callback(tone(1000, 0.4, 48_000, 2), 0, None, 0)
            block = recorder._interleave(
                recorder._me.take(1600 * 2), recorder._them.take(1600 * 2)
            )
            frames = np.frombuffer(block, dtype=np.int16)
            assert frames.size % CHANNEL_COUNT == 0
            them = frames[1::CHANNEL_COUNT]
            me = frames[0::CHANNEL_COUNT]
            assert np.abs(them).max() > 1000, "the speakers produced nothing"
            assert np.abs(me).max() == 0, "silence leaked onto the wrong channel"
        finally:
            recorder.stop()

    def test_a_stalled_device_is_padded_rather_than_blocking(
        self, fake_portaudio
    ) -> None:
        """One dead device must not stop the other being recorded."""
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        try:
            block = recorder._them.take(1600 * 2)
            assert len(block) == 1600 * 2
            assert set(block) == {0}
        finally:
            recorder.stop()

    def test_stats_report_each_source(self, fake_portaudio) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        try:
            stats = recorder.stats()
            assert stats["them"]["device"] == SPEAKERS.name
            assert stats["me"]["device"] == MIC.name
            assert stats["them"]["error"] is None
        finally:
            recorder.stop()


class TestGoingQuiet:
    """Two ways a recording produces nothing while looking healthy."""

    def test_recent_peak_resets_so_going_quiet_is_visible(
        self, fake_portaudio
    ) -> None:
        """Cumulative peak cannot tell "stopped" from "still working".

        Bluetooth headphones that disconnect mid-meeting leave the capture
        stream open on a device nobody is listening to. The audio simply
        stops - but the maximum since the beginning stays high forever, so
        only a peak that resets can notice.
        """
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        try:
            recorder._them.callback(tone(1000, 0.2, 48_000, 2), 0, None, 0)
            recorder._them.drain()
            assert recorder.recent_peak() > 1000, "the audio was not seen"
            # Nothing further arrives - the device has gone away.
            assert recorder.recent_peak() == 0
            assert recorder._them.peak > 1000, "cumulative peak should persist"
        finally:
            recorder.stop()

    def test_the_loopback_name_contains_the_render_device_name(self) -> None:
        """How the device-change check works, pinned.

        Windows names a loopback after its render endpoint plus a suffix, so
        the watchdog tests containment rather than equality. If that naming
        ever stopped holding, the check would fire on every recording.
        """
        assert "Speaker (Realtek)" in SPEAKERS.name
        assert SPEAKERS.name.endswith("[Loopback]")

    def test_a_changed_default_output_would_not_match(self) -> None:
        moved_to = "Headphones (AirPods - Find My)"
        assert moved_to not in SPEAKERS.name


# --- multichannel transcription --------------------------------------------


class TestMultichannel:
    def test_the_url_asks_for_separate_channels(self) -> None:
        url = build_stream_url(
            model="nova-3", language="en-US", sample_rate=TARGET_RATE,
            diarize=True, channels=2, multichannel=True,
        )
        assert "multichannel=true" in url
        assert "channels=2" in url

    def test_mono_streams_do_not_ask_for_it(self) -> None:
        url = build_stream_url(
            model="nova-3", language="en-US", sample_rate=16_000, diarize=True,
        )
        assert "multichannel" not in url

    def test_your_own_channel_is_named_not_guessed(self) -> None:
        """The microphone channel *is* the attribution.

        Diarization would call this "Speaker 3" or whatever it inferred from
        the voice; the channel already knows better, so it wins.
        """
        segments = parse_results_message(
            {
                "is_final": True,
                "channel_index": [0, 2],
                "channel": {"alternatives": [{
                    "transcript": "hello there",
                    "words": [
                        {"word": "hello", "start": 0.0, "end": 0.4, "speaker": 3},
                        {"word": "there", "start": 0.4, "end": 0.8, "speaker": 3},
                    ],
                }]},
            },
            default_speaker="Speaker 0",
            channel_speakers=CHANNEL_SPEAKERS,
        )
        assert [(s.speaker, s.text) for s in segments] == [
            (SELF_SPEAKER, "hello there")
        ]

    def test_the_far_side_still_gets_diarized(self) -> None:
        segments = parse_results_message(
            {
                "is_final": True,
                "channel_index": [1, 2],
                "channel": {"alternatives": [{
                    "transcript": "hi bye",
                    "words": [
                        {"word": "hi", "start": 0.0, "end": 0.4, "speaker": 0},
                        {"word": "bye", "start": 0.5, "end": 0.9, "speaker": 1},
                    ],
                }]},
            },
            default_speaker="Speaker 0",
            channel_speakers=CHANNEL_SPEAKERS,
        )
        assert [s.speaker for s in segments] == ["Speaker 0", "Speaker 1"]

    def test_mono_results_are_unaffected(self) -> None:
        """The bot path sends one channel and must behave exactly as before."""
        segments = parse_results_message(
            {
                "is_final": True,
                "channel": {"alternatives": [{
                    "transcript": "hello",
                    "words": [
                        {"word": "hello", "start": 0.0, "end": 0.4, "speaker": 2}
                    ],
                }]},
            },
            default_speaker="Speaker 0",
        )
        assert [s.speaker for s in segments] == ["Speaker 2"]

    def test_an_unknown_channel_index_falls_back(self) -> None:
        segments = parse_results_message(
            {
                "is_final": True,
                "channel_index": [7, 8],
                "channel": {"alternatives": [{"transcript": "hm", "words": []}]},
            },
            default_speaker="Speaker 0",
            channel_speakers=CHANNEL_SPEAKERS,
        )
        assert segments[0].speaker == "Speaker 0"


# --- naming ----------------------------------------------------------------


class TestSlugify:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Weekly Roadmap Sync", "weekly-roadmap-sync"),
            ("  Spaces  Everywhere  ", "spaces-everywhere"),
            ("Client call: Acme (Q3)", "client-call-acme-q3"),
            ("", "local"),
            ("!!!", "local"),
            ("café ☕", "caf"),
        ],
    )
    def test_titles_become_safe_directory_names(self, title, expected) -> None:
        assert slugify(title) == expected

    def test_long_titles_are_capped(self) -> None:
        assert len(slugify("word " * 50)) <= 40


class TestAutoGain:
    """A quiet microphone is the difference between a transcript and noise.

    Measured on the first real recording: the speakers arrived at -1.6 dBFS
    and the laptop's array microphone at -20.4 dBFS. Deepgram transcribed the
    loud channel perfectly and mangled the quiet one - dropping words, and
    inventing others that were never said.
    """

    @staticmethod
    def _run(signal, **kwargs):
        from meetbot.capture.local import AutoGain

        gain = AutoGain(**kwargs)
        out = b"".join(
            gain.process(signal[i:i + 1600].tobytes())
            for i in range(0, len(signal), 1600)
        )
        return np.frombuffer(out, dtype=np.int16), gain

    @staticmethod
    def _speech(amplitude: float, seconds: float = 4.0):
        """A tone modulated like speech, at a given fraction of full scale."""
        t = np.arange(int(16_000 * seconds)) / 16_000
        wave = amplitude * np.sin(2 * np.pi * 200 * t) * (
            1 + 0.5 * np.sin(2 * np.pi * 3 * t)
        )
        return (wave * 32767).astype(np.int16)

    def test_a_quiet_channel_is_lifted(self) -> None:
        quiet = self._speech(0.1)
        out, gain = self._run(quiet)
        assert gain.applied_max > 2.0, "a 20 dB deficit was left alone"
        assert np.abs(out).max() > np.abs(quiet).max() * 2

    def test_lifting_never_clips(self) -> None:
        """Clipping would trade quiet-and-garbled for loud-and-garbled."""
        out, _ = self._run(self._speech(0.1))
        assert int(np.abs(out).max()) < 32767

    def test_an_already_loud_channel_is_untouched(self) -> None:
        """This runs on the speakers too, where it must do nothing at all."""
        loud = self._speech(0.8)
        out, gain = self._run(loud)
        assert gain.applied_max == pytest.approx(1.0, abs=0.01)
        assert out.tobytes() == loud.tobytes()

    def test_the_noise_floor_is_not_amplified(self) -> None:
        """The failure this guards against invents speech from nothing.

        Turning up the room between sentences pushes hiss into the range
        where a recogniser starts hearing words in it. Silence stays silent.
        """
        rng = np.random.default_rng(0)
        hiss = rng.normal(0, 40, 16_000 * 3).astype(np.int16)
        out, gain = self._run(hiss)
        assert gain.applied_max == pytest.approx(1.0, abs=0.01)
        assert int(np.abs(out).max()) == int(np.abs(hiss).max())

    def test_gain_is_bounded(self) -> None:
        """Past a point the room is louder than the speech ever was."""
        from meetbot.capture.local import MAX_GAIN

        _, gain = self._run(self._speech(0.02, seconds=8.0))
        assert gain.applied_max <= MAX_GAIN + 0.01

    def test_a_silent_gap_does_not_reset_the_level(self) -> None:
        """Speech, a pause, then speech - the pause must not pump the gain."""
        signal = self._speech(0.1, seconds=6.0).copy()
        signal[16_000:32_000] = 0
        out, gain = self._run(signal)
        assert gain.applied_max > 2.0
        # The silent stretch stays silent rather than becoming amplified hiss.
        assert int(np.abs(out[17_000:31_000]).max()) == 0

    def test_empty_input_is_returned_unchanged(self) -> None:
        from meetbot.capture.local import AutoGain

        assert AutoGain().process(b"") == b""

    def test_it_can_be_switched_off(self, fake_portaudio) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC, auto_gain=False)
        try:
            assert recorder._them.gain is None
            assert recorder._me.gain is None
        finally:
            recorder.stop()

    def test_stats_report_the_untouched_peak(self, fake_portaudio) -> None:
        """A microphone that is genuinely too quiet must stay diagnosable.

        Reporting the post-gain level would hide the problem behind the thing
        compensating for it.
        """
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.start()
        try:
            stats = recorder.stats()
            assert "gain" in stats["me"]
            assert stats["me"]["peak"] == 0
        finally:
            recorder.stop()
