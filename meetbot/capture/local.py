"""Capture a meeting from this machine, without joining it.

The bot path signs a Google account into Chromium, joins the call as a
participant, and taps the remote audio from inside the page. It works, and it
costs: a Google account whose session expires every few hours, a lobby to be
admitted through, DOM selectors that break whenever Meet ships a redesign, and
a visible extra participant that changes how people talk.

None of that is necessary. The meeting is already playing through this
machine's speakers, and you are already talking into its microphone. Windows
exposes both:

**WASAPI loopback** taps the render endpoint - what the speakers are playing -
so it hears every remote participant, in any application, with no bot and no
account. Zoom, Teams, Meet and a phone on speaker all look the same to it.

**The microphone** is captured separately rather than mixed in. Keeping the
two apart is not tidiness: it is free, perfect speaker attribution for at
least one person. Diarization guesses who spoke from voice characteristics
and labels them ``Speaker 0``; the microphone channel is *definitionally*
you. So the two streams are interleaved into a two-channel signal - channel 0
you, channel 1 everyone else - and Deepgram is asked to transcribe the
channels separately.

Two things about loopback are worth knowing before trusting it:

**It taps the mix after the volume control.** Muted speakers or a volume of
zero record silence, and nothing about the capture looks broken while it
happens. :func:`probe_loopback` exists so that is caught before a meeting
rather than discovered after one.

**It hears everything the machine plays**, not just the meeting - a video in
another tab, a notification chime. That is the trade for not needing a bot.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: What Deepgram is fed. Speech carries almost nothing above 8 kHz, so 16 kHz
#: is the standard ASR rate and costs a third of the bytes of 48 kHz.
TARGET_RATE = 16_000

#: Channel layout of the interleaved stream handed to the transcriber.
CHANNEL_ME = 0
CHANNEL_THEM = 1
CHANNEL_COUNT = 2

#: Milliseconds of audio in each block emitted downstream.
BLOCK_MS = 100

#: Frames read from a device at a time. Small enough to keep latency low,
#: large enough that the read loop is not spinning.
DEVICE_CHUNK = 1024

#: How far a source may run ahead before the oldest audio is dropped. Two
#: devices free-run on separate clocks and drift apart; without a cap the
#: faster one's buffer grows for the whole meeting and the streams desync.
MAX_BUFFER_S = 2.0


class LocalCaptureError(RuntimeError):
    """Raised when this machine cannot be recorded from."""


@dataclass(frozen=True)
class AudioDevice:
    """One capturable device.

    Attributes:
        index: PortAudio device index, passed back to open the stream.
        name: Human-readable name, as Windows reports it.
        channels: Maximum input channels the device offers.
        rate: The device's native sample rate. WASAPI shared mode will not
            convert, so this is the rate audio actually arrives at.
        is_loopback: True for a render endpoint being tapped, false for a
            real microphone.
    """

    index: int
    name: str
    channels: int
    rate: int
    is_loopback: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the API and the UI."""
        return {
            "index": self.index,
            "name": self.name,
            "channels": self.channels,
            "rate": self.rate,
            "is_loopback": self.is_loopback,
        }


def _pyaudio() -> Any:
    """Import the WASAPI-capable PyAudio fork, or explain how to get it."""
    try:
        import pyaudiowpatch
    except ImportError as exc:  # pragma: no cover - import guard
        raise LocalCaptureError(
            "Recording this machine needs PyAudioWPatch, which provides "
            "WASAPI loopback. Install it with: pip install PyAudioWPatch"
        ) from exc
    return pyaudiowpatch


def _numpy() -> Any:
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - import guard
        raise LocalCaptureError(
            "Recording this machine needs numpy to resample audio. "
            "Install it with: pip install numpy"
        ) from exc
    return numpy


# -- device discovery -------------------------------------------------------


def list_devices() -> tuple[list[AudioDevice], list[AudioDevice]]:
    """Every capturable device on this machine.

    Returns:
        ``(loopbacks, microphones)``. Loopbacks tap what the speakers play;
        microphones are real inputs. Both lists may be empty.

    Raises:
        LocalCaptureError: If WASAPI is unavailable, which on Windows means
            the audio service is not running.
    """
    pyaudio = _pyaudio()
    audio = pyaudio.PyAudio()
    try:
        try:
            wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as exc:
            raise LocalCaptureError(
                f"WASAPI is not available on this machine: {exc}"
            ) from exc

        loopbacks = [
            AudioDevice(
                index=int(d["index"]),
                name=str(d["name"]),
                channels=int(d["maxInputChannels"]),
                rate=int(d["defaultSampleRate"]),
                is_loopback=True,
            )
            for d in audio.get_loopback_device_info_generator()
        ]
        microphones = [
            AudioDevice(
                index=int(d["index"]),
                name=str(d["name"]),
                channels=int(d["maxInputChannels"]),
                rate=int(d["defaultSampleRate"]),
                is_loopback=False,
            )
            for d in (
                audio.get_device_info_by_index(i)
                for i in range(audio.get_device_count())
            )
            if d["maxInputChannels"] > 0
            and not d.get("isLoopbackDevice")
            and d["hostApi"] == wasapi["index"]
        ]
        return loopbacks, microphones
    finally:
        audio.terminate()


def default_output_name() -> str:
    """Name of the device Windows is currently playing through.

    Cheap enough to poll: a recording watches this so it can say something
    when Bluetooth headphones drop and playback moves elsewhere.
    """
    pyaudio = _pyaudio()
    audio = pyaudio.PyAudio()
    try:
        wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        return str(
            audio.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        )
    except (OSError, KeyError):
        return ""
    finally:
        audio.terminate()


def default_devices() -> tuple[AudioDevice | None, AudioDevice | None]:
    """The loopback for the default speakers, and the default microphone.

    The loopback is matched to the *current* default output rather than
    simply taken first, so plugging in headphones mid-session picks the
    device the meeting is actually playing through.
    """
    pyaudio = _pyaudio()
    audio = pyaudio.PyAudio()
    try:
        try:
            wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
            out_name = str(
                audio.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
            )
        except (OSError, KeyError):
            out_name = ""
    finally:
        audio.terminate()

    loopbacks, microphones = list_devices()
    loopback = next((d for d in loopbacks if out_name and out_name in d.name), None)
    loopback = loopback or (loopbacks[0] if loopbacks else None)
    microphone = microphones[0] if microphones else None
    return loopback, microphone


def find_device(index: int) -> AudioDevice | None:
    """Look up a device by index across both kinds."""
    loopbacks, microphones = list_devices()
    return next((d for d in loopbacks + microphones if d.index == index), None)


# -- resampling -------------------------------------------------------------


class Resampler:
    """Device audio to 16 kHz mono, with filter state kept across blocks.

    Two details matter and are easy to get wrong:

    **Anti-aliasing.** Dropping samples to go from 48 kHz to 16 kHz folds
    everything above 8 kHz back down into the speech band as noise. A
    windowed-sinc low-pass runs first.

    **State.** Audio arrives in blocks. Filtering each block independently
    leaves a discontinuity at every boundary - a faint click every 21 ms at
    the default chunk size - so the filter's history and the fractional
    read position carry over from one block to the next.
    """

    def __init__(self, in_rate: int, channels: int, out_rate: int = TARGET_RATE):
        """Create a resampler for one device's stream.

        Args:
            in_rate: The device's native sample rate.
            channels: Channels the device delivers. Anything above one is
                averaged down to mono.
            out_rate: Target rate.
        """
        np = _numpy()
        self._np = np
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.channels = max(1, int(channels))
        self._ratio = self.in_rate / self.out_rate
        self._phase = 0.0
        # Cutoff just under the output Nyquist, with a little margin so the
        # transition band lands below it rather than straddling it.
        taps = 64 if self.in_rate != self.out_rate else 0
        if taps:
            cutoff = 0.45 * self.out_rate / self.in_rate
            n = np.arange(taps) - (taps - 1) / 2.0
            kernel = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
            self._kernel = (kernel / kernel.sum()).astype(np.float64)
        else:
            self._kernel = None
        self._history = np.zeros(0, dtype=np.float64)

    def process(self, pcm: bytes) -> bytes:
        """Convert one block of interleaved int16 PCM to 16 kHz mono int16."""
        np = self._np
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        if self.channels > 1:
            usable = (samples.size // self.channels) * self.channels
            if usable == 0:
                return b""
            samples = samples[:usable].reshape(-1, self.channels).mean(axis=1)
        if samples.size == 0:
            return b""

        if self._kernel is None:
            return samples.astype(np.int16).tobytes()

        # Filter across the boundary by prepending the previous tail.
        padded = np.concatenate([self._history, samples])
        if padded.size < self._kernel.size:
            self._history = padded
            return b""
        filtered = np.convolve(padded, self._kernel, mode="valid")
        self._history = padded[-(self._kernel.size - 1):]

        # Read positions continue from wherever the last block stopped.
        positions = np.arange(self._phase, filtered.size - 1, self._ratio)
        if positions.size == 0:
            self._phase -= filtered.size
            return b""
        left = positions.astype(np.int64)
        frac = positions - left
        out = filtered[left] * (1.0 - frac) + filtered[left + 1] * frac
        self._phase = positions[-1] + self._ratio - filtered.size

        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# -- capture ----------------------------------------------------------------


class _Source:
    """One device, filled by PortAudio's callback and drained by the consumer.

    The callback runs on PortAudio's own real-time thread, so it does the
    least possible work: append the bytes and return. Resampling happens in
    :meth:`drain`, on whichever thread is consuming blocks, where being slow
    costs latency rather than dropped audio.
    """

    def __init__(self, device: AudioDevice, label: str) -> None:
        self.device = device
        self.label = label
        #: Raw device bytes, written by the audio callback. deque.append and
        #: popleft are atomic in CPython, so no lock is needed here.
        self.raw: deque[bytes] = deque(maxlen=512)
        self.buffer = bytearray()
        self.frames = 0
        self.peak = 0
        #: Peak since the last time anyone asked. Reset on read, so a source
        #: that has gone quiet is distinguishable from one that was never
        #: working - which cumulative `peak` cannot tell you.
        self.recent_peak = 0
        self.error: str | None = None
        self.dropped = 0
        self.overflows = 0
        self.stream: Any = None
        self._resampler = Resampler(device.rate, device.channels)
        self._np = _numpy()

    def callback(self, in_data: bytes, _frames: int, _time: Any, status: int) -> tuple:
        """PortAudio callback. Must stay trivial - see the class docstring."""
        if status:
            self.overflows += 1
        if in_data:
            self.raw.append(in_data)
        return (None, 0)  # (no output, paContinue)

    def drain(self) -> None:
        """Resample everything the callback has delivered so far."""
        np = self._np
        pending: list[bytes] = []
        while True:
            try:
                pending.append(self.raw.popleft())
            except IndexError:
                break
        if not pending:
            return
        for chunk in pending:
            block = np.frombuffer(chunk, dtype=np.int16)
            if block.size:
                loudest = int(np.abs(block).max())
                self.peak = max(self.peak, loudest)
                self.recent_peak = max(self.recent_peak, loudest)
            converted = self._resampler.process(chunk)
            if converted:
                self.buffer.extend(converted)
        limit = int(MAX_BUFFER_S * TARGET_RATE) * 2
        if len(self.buffer) > limit:
            # Clock drift, or a stalled consumer. Dropping the oldest audio
            # keeps the two channels aligned; letting the buffer grow would
            # desync them for the rest of the meeting.
            excess = len(self.buffer) - limit
            del self.buffer[:excess]
            self.dropped += excess // 2

    def take(self, want_bytes: int) -> bytes:
        """Up to ``want_bytes`` of 16 kHz mono, zero-padded when behind."""
        self.drain()
        block = bytes(self.buffer[:want_bytes])
        del self.buffer[:want_bytes]
        self.frames += len(block) // 2
        if len(block) < want_bytes:
            block += b"\x00" * (want_bytes - len(block))
        return block



class LocalRecorder:
    """Captures this machine's speakers and microphone as one stream.

    Yields interleaved two-channel 16 kHz PCM16: channel 0 is the microphone
    (you), channel 1 is everything the speakers played (everyone else).

    Usage::

        recorder = LocalRecorder(loopback, microphone)
        recorder.start()
        for block in recorder.blocks():
            deepgram.send_audio(block)
        recorder.stop()
    """

    def __init__(
        self,
        loopback: AudioDevice | None,
        microphone: AudioDevice | None = None,
        *,
        block_ms: int = BLOCK_MS,
    ) -> None:
        """Create a recorder.

        Args:
            loopback: The render endpoint to tap. Required - without it there
                is no meeting to record, only your own half of it.
            microphone: Your own input. Optional; when absent, channel 0 is
                silence and only the far side is transcribed.
            block_ms: Milliseconds of audio per emitted block.

        Raises:
            LocalCaptureError: If no loopback device was given.
        """
        if loopback is None:
            raise LocalCaptureError(
                "No speaker loopback device was found, so there is nothing to "
                "record. Check that Windows has an active playback device."
            )
        self.loopback = loopback
        self.microphone = microphone
        self.block_frames = int(TARGET_RATE * block_ms / 1000)
        self._stop = threading.Event()
        self._audio: Any = None
        self._them = _Source(loopback, "them")
        self._me = _Source(microphone, "me") if microphone else None
        self._started_at: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the devices and begin buffering.

        The PyAudio instance and both streams are created here, on the
        caller's thread. Creating them inside worker threads segfaults -
        WASAPI wants COM initialised per thread, and PortAudio's host API is
        not built to be constructed concurrently - which is why capture runs
        on PortAudio's own callback threads rather than any of ours.
        """
        if self._started_at is not None:
            return
        pyaudio = _pyaudio()
        self._stop.clear()
        self._audio = pyaudio.PyAudio()
        opened: list[_Source] = []
        try:
            for source in filter(None, (self._them, self._me)):
                source.stream = self._audio.open(
                    format=pyaudio.paInt16,
                    channels=source.device.channels,
                    rate=source.device.rate,
                    input=True,
                    input_device_index=source.device.index,
                    frames_per_buffer=DEVICE_CHUNK,
                    stream_callback=source.callback,
                )
                opened.append(source)
        except OSError as exc:
            for source in opened:
                with contextlib.suppress(Exception):
                    source.stream.close()
            self._audio.terminate()
            self._audio = None
            raise LocalCaptureError(
                f"Could not open an audio device for recording: {exc}"
            ) from exc

        self._started_at = time.time()
        logger.info(
            "Recording this machine: speakers=%r microphone=%r",
            self.loopback.name,
            self.microphone.name if self.microphone else None,
        )

    def stop(self) -> None:
        """Stop the streams and release the devices. Idempotent."""
        self._stop.set()
        for source in filter(None, (self._them, self._me)):
            if source.stream is not None:
                with contextlib.suppress(Exception):
                    source.stream.stop_stream()
                with contextlib.suppress(Exception):
                    source.stream.close()
                source.stream = None
        if self._audio is not None:
            with contextlib.suppress(Exception):
                self._audio.terminate()
            self._audio = None

    # -- output ------------------------------------------------------------

    def blocks(self) -> Iterator[bytes]:
        """Yield interleaved two-channel blocks until :meth:`stop`.

        A block is emitted on a wall-clock cadence rather than when both
        devices have delivered, so one stalled device cannot hold up the
        other - its channel is padded with silence instead.
        """
        period = self.block_frames / TARGET_RATE
        want = self.block_frames * 2
        next_at = time.monotonic()
        while not self._stop.is_set():
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                if self._stop.wait(delay):
                    break
            else:
                # Fell behind; resync rather than spiral.
                next_at = time.monotonic()
            yield self._interleave(
                self._me.take(want) if self._me else b"\x00" * want,
                self._them.take(want),
            )

    def _interleave(self, me: bytes, them: bytes) -> bytes:
        np = _numpy()
        a = np.frombuffer(me, dtype=np.int16)
        b = np.frombuffer(them, dtype=np.int16)
        size = min(a.size, b.size)
        out = np.empty(size * CHANNEL_COUNT, dtype=np.int16)
        out[CHANNEL_ME::CHANNEL_COUNT] = a[:size]
        out[CHANNEL_THEM::CHANNEL_COUNT] = b[:size]
        return out.tobytes()

    # -- diagnostics -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """What each source has actually delivered."""
        payload: dict[str, Any] = {
            "elapsed_s": round(time.time() - self._started_at, 1)
            if self._started_at
            else 0.0,
            "them": self._source_stats(self._them),
        }
        payload["me"] = self._source_stats(self._me) if self._me else None
        return payload

    def recent_peak(self, *, reset: bool = True) -> int:
        """Loudest sample the speakers produced since this was last called."""
        peak = self._them.recent_peak
        if reset:
            self._them.recent_peak = 0
        return peak

    @property
    def capturing(self) -> str:
        """Name of the device being recorded, for comparing against default."""
        return self.loopback.name

    @staticmethod
    def _source_stats(source: _Source) -> dict[str, Any]:
        return {
            "device": source.device.name,
            "seconds": round(source.frames / TARGET_RATE, 1),
            "peak": source.peak,
            "dropped_frames": source.dropped,
            "error": source.error,
        }


# -- readiness --------------------------------------------------------------


@dataclass(frozen=True)
class LoopbackProbe:
    """The result of listening to the speakers for a moment."""

    device: str
    peak: int
    seconds: float
    silent: bool

    @property
    def detail(self) -> str:
        if self.silent:
            return (
                f"{self.device} produced silence. If audio really was playing, "
                "the speakers are muted or the volume is zero - loopback taps "
                "the mix after the volume control, so it records nothing."
            )
        return f"{self.device} is producing audio (peak {self.peak})"


def probe_loopback(seconds: float = 1.0, *, device: AudioDevice | None = None) -> LoopbackProbe:
    """Listen to the speakers briefly and report whether anything is there.

    Silence is not necessarily a fault - nothing may be playing. It is a
    fault when it happens during a meeting, and the point of probing is to
    surface the muted-speakers case before a recording rather than after it.

    Raises:
        LocalCaptureError: If there is no loopback device to listen to.
    """
    pyaudio = _pyaudio()
    np = _numpy()
    if device is None:
        device, _ = default_devices()
    if device is None:
        raise LocalCaptureError(
            "No speaker loopback device was found. Windows needs an active "
            "playback device for this machine to be recorded."
        )

    audio = pyaudio.PyAudio()
    frames: list[bytes] = []
    try:
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=device.channels,
            rate=device.rate,
            input=True,
            input_device_index=device.index,
            frames_per_buffer=DEVICE_CHUNK,
        )
        try:
            deadline = time.time() + seconds
            while time.time() < deadline:
                frames.append(stream.read(DEVICE_CHUNK, exception_on_overflow=False))
        finally:
            with contextlib.suppress(Exception):
                stream.stop_stream()
            with contextlib.suppress(Exception):
                stream.close()
    except OSError as exc:
        raise LocalCaptureError(f"Could not open {device.name}: {exc}") from exc
    finally:
        audio.terminate()

    raw = b"".join(frames)
    samples = np.frombuffer(raw, dtype=np.int16)
    peak = int(np.abs(samples).max()) if samples.size else 0
    return LoopbackProbe(
        device=device.name,
        peak=peak,
        seconds=round(samples.size / max(1, device.rate * device.channels), 2),
        # A handful of LSBs is dither and idle noise, not audio.
        silent=peak <= 2,
    )


def capture_available() -> tuple[bool, str]:
    """Whether this machine can be recorded, and why not when it cannot."""
    try:
        loopbacks, microphones = list_devices()
    except LocalCaptureError as exc:
        return False, str(exc)
    if not loopbacks:
        return False, (
            "No speaker loopback device. Windows needs an active playback "
            "device - check Sound settings."
        )
    detail = f"speakers: {loopbacks[0].name}"
    if microphones:
        detail += f"; microphone: {microphones[0].name}"
    else:
        detail += "; no microphone (your own voice will not be recorded)"
    return True, detail


__all__ = [
    "BLOCK_MS",
    "CHANNEL_COUNT",
    "CHANNEL_ME",
    "CHANNEL_THEM",
    "TARGET_RATE",
    "AudioDevice",
    "LocalCaptureError",
    "LocalRecorder",
    "LoopbackProbe",
    "Resampler",
    "capture_available",
    "default_devices",
    "default_output_name",
    "find_device",
    "list_devices",
    "probe_loopback",
]
