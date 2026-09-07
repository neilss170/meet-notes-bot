"""Deepgram live transcription over a raw WebSocket.

This talks to ``wss://api.deepgram.com/v1/listen`` directly rather than through
the ``deepgram-sdk`` package. The reason is stability: the SDK's client surface
has changed shape across major versions, while the streaming WebSocket protocol
(query-string options in, JSON results out) has been stable. Depending on the
protocol means a `pip install` a year from now does not silently break the
audio path. The trade-off is that we implement keepalive, reconnect and result
parsing ourselves, which is what this module is.

One :class:`DeepgramLiveClient` owns one connection, which corresponds to one
audio channel - the mixed stream, or a single participant.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL: Final[str] = "wss://api.deepgram.com/v1/listen"

#: Silence that ends an utterance. Long enough to survive an ordinary pause
#: for breath - at the old 300 ms, sentences were being cut in half, which
#: cost both readability and the context the model uses to pick words and
#: punctuation.
DEFAULT_ENDPOINTING_MS = 800

#: Hard ceiling on how long one utterance may run without a pause, so
#: continuous speech still gets broken into readable turns.
DEFAULT_UTTERANCE_END_MS = 1000

#: Deepgram closes an idle socket after ~12 s; ping well inside that.
KEEPALIVE_INTERVAL_S: Final[float] = 8.0

#: Reconnect backoff bounds, in seconds.
_BACKOFF_INITIAL_S: Final[float] = 1.0
_BACKOFF_MAX_S: Final[float] = 30.0


class DeepgramError(RuntimeError):
    """Raised for non-recoverable Deepgram failures (e.g. a bad API key)."""


@dataclass(frozen=True)
class TranscriptSegment:
    """One speaker-homogeneous run of words from a final Deepgram result."""

    speaker: str
    text: str
    start: float
    end: float
    confidence: float | None = None


#: Callback invoked with each finalised segment. May be async.
SegmentHandler = Callable[[TranscriptSegment], Awaitable[None] | None]


def build_stream_url(
    *,
    model: str,
    language: str,
    sample_rate: int,
    diarize: bool,
    encoding: str = "linear16",
    channels: int = 1,
    endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
    utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS,
) -> str:
    """Build the Deepgram streaming URL with our option set.

    ``interim_results`` is off deliberately: we persist finalised utterances
    only, so partial hypotheses would just be rewritten noise in the JSONL.

    ``endpointing_ms`` is the silence that ends an utterance, and it is a
    real accuracy control rather than only a latency one. It was 300 ms,
    which is shorter than an ordinary pause for breath: speech was cut into
    fragments mid-sentence, which both reads badly and denies the model the
    surrounding context it uses to choose words and place punctuation.
    """
    params = {
        "encoding": encoding,
        "sample_rate": str(sample_rate),
        "channels": str(channels),
        "model": model,
        "language": language,
        "punctuate": "true",
        # Sentence casing, punctuation, and formatting of numbers, dates and
        # times. Wants whole phrases to work on, hence the endpointing above.
        "smart_format": "true",
        "interim_results": "false",
        "diarize": "true" if diarize else "false",
        "endpointing": str(endpointing_ms),
        # Deepgram closes an utterance after this much silence even when
        # endpointing has not fired, which keeps a long unbroken stretch of
        # speech from becoming one enormous block.
        "utterance_end_ms": str(utterance_end_ms),
    }
    return f"{DEEPGRAM_WS_URL}?{urlencode(params)}"


def parse_results_message(
    payload: dict[str, Any], *, default_speaker: str
) -> list[TranscriptSegment]:
    """Turn one Deepgram ``Results`` message into transcript segments.

    With diarization on, a single result can contain words from more than one
    speaker; consecutive words sharing a speaker id become one segment. With
    diarization off (per-participant capture) everything collapses to a single
    segment labelled ``default_speaker``.

    Non-final results and empty transcripts yield an empty list.
    """
    if not payload.get("is_final", False):
        return []

    channel = payload.get("channel") or {}
    alternatives = channel.get("alternatives") or []
    if not alternatives:
        return []

    best = alternatives[0]
    transcript = (best.get("transcript") or "").strip()
    if not transcript:
        return []

    words = best.get("words") or []
    if not words:
        start = float(payload.get("start", 0.0))
        return [
            TranscriptSegment(
                speaker=default_speaker,
                text=transcript,
                start=start,
                end=start + float(payload.get("duration", 0.0)),
                confidence=best.get("confidence"),
            )
        ]

    segments: list[TranscriptSegment] = []
    current_speaker: str | None = None
    current_words: list[str] = []
    current_start = 0.0
    current_end = 0.0
    confidences: list[float] = []

    def flush() -> None:
        if current_speaker is None or not current_words:
            return
        segments.append(
            TranscriptSegment(
                speaker=current_speaker,
                text=" ".join(current_words),
                start=current_start,
                end=current_end,
                confidence=(
                    sum(confidences) / len(confidences) if confidences else None
                ),
            )
        )

    for word in words:
        raw_speaker = word.get("speaker")
        speaker = (
            default_speaker if raw_speaker is None else f"Speaker {int(raw_speaker)}"
        )
        text = word.get("punctuated_word") or word.get("word") or ""
        if not text:
            continue
        if speaker != current_speaker:
            flush()
            current_speaker = speaker
            current_words = []
            current_start = float(word.get("start", 0.0))
            confidences = []
        current_words.append(text)
        current_end = float(word.get("end", current_end))
        word_confidence = word.get("confidence")
        if isinstance(word_confidence, (int, float)):
            confidences.append(float(word_confidence))

    flush()
    return segments


class DeepgramLiveClient:
    """A single live-transcription connection with reconnect and keepalive.

    Audio is handed in with :meth:`send_audio` from the bridge's receive loop;
    :meth:`run` owns the connection lifecycle and drains an internal queue.
    Decoupling the two means a Deepgram reconnect never blocks the browser's
    WebSocket, and audio recorded during the outage is buffered rather than
    lost outright.
    """

    def __init__(
        self,
        *,
        api_key: str,
        on_segment: SegmentHandler,
        model: str,
        language: str,
        sample_rate: int,
        diarize: bool = True,
        default_speaker: str = "Speaker 0",
        channel_label: str = "mixed",
        max_queued_frames: int = 600,
        on_connect: Callable[[], None] | None = None,
        endpointing_ms: int = DEFAULT_ENDPOINTING_MS,
        utterance_end_ms: int = DEFAULT_UTTERANCE_END_MS,
    ) -> None:
        """Create a client. Nothing connects until :meth:`run` is awaited.

        Args:
            api_key: Deepgram API key.
            on_segment: Called for each finalised transcript segment.
            model: Deepgram model name, e.g. ``"nova-3"``.
            language: BCP-47 language tag.
            sample_rate: PCM sample rate of the audio handed to
                :meth:`send_audio`.
            diarize: Enable Deepgram speaker diarization. Turn this off when a
                channel already carries exactly one participant.
            default_speaker: Label used when no diarization id is available.
            channel_label: Human-readable channel name, used in logs.
            max_queued_frames: Bound on buffered audio frames during an
                outage. Oldest frames are dropped past this.
            on_connect: Called each time a Deepgram stream is (re)established.
                Segment timestamps are relative to the current stream, so the
                caller needs this to rebase them onto the meeting timeline.
            endpointing_ms: Silence that ends an utterance. Raising it gives
                the model more context per utterance, which improves both
                wording and punctuation; lowering it makes captions appear
                sooner but fragments sentences.
            utterance_end_ms: Ceiling on one utterance's length without a
                pause, so continuous speech is still broken into turns.
        """
        self._api_key = api_key
        self._on_segment = on_segment
        self._on_connect = on_connect
        self._url = build_stream_url(
            model=model,
            language=language,
            sample_rate=sample_rate,
            diarize=diarize,
            endpointing_ms=endpointing_ms,
            utterance_end_ms=utterance_end_ms,
        )
        self._default_speaker = default_speaker
        self._channel_label = channel_label
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=max_queued_frames
        )
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._dropped_frames = 0
        self._segments_emitted = 0

    # -- public API --------------------------------------------------------

    @property
    def channel_label(self) -> str:
        """The human-readable label for this channel."""
        return self._channel_label

    @property
    def stats(self) -> dict[str, int | bool]:
        """Counters for logging at shutdown."""
        return {
            "segments": self._segments_emitted,
            "dropped_frames": self._dropped_frames,
            "connected": self._connected.is_set(),
        }

    def send_audio(self, pcm: bytes) -> None:
        """Queue a PCM frame for transmission. Never blocks.

        If the queue is full (Deepgram is down or slow) the oldest frame is
        discarded and a warning is logged - dropping the oldest audio keeps the
        live transcript current rather than falling further behind.
        """
        if self._stopping.is_set():
            return
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            self._dropped_frames += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(pcm)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                # The sender drained or refilled the queue between our two
                # operations. Not an error: this frame is simply dropped too,
                # and the counter below still reports it.
                self._dropped_frames += 1
            if self._dropped_frames % 50 == 1:
                logger.warning(
                    "Deepgram queue full on channel %s; dropped %d frame(s) so far",
                    self._channel_label,
                    self._dropped_frames,
                )

    async def stop(self) -> None:
        """Signal the run loop to flush and exit."""
        self._stopping.set()
        await self._queue.put(None)

    async def run(self) -> None:
        """Maintain the connection until :meth:`stop` is called.

        Reconnects with exponential backoff on transport errors. Authentication
        failures are fatal and re-raised as :class:`DeepgramError`, because
        retrying a bad key forever helps nobody.
        """
        backoff = _BACKOFF_INITIAL_S
        while not self._stopping.is_set():
            try:
                await self._run_once()
                backoff = _BACKOFF_INITIAL_S
            except DeepgramError:
                raise
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                if self._stopping.is_set():
                    break
                logger.warning(
                    "Deepgram connection lost on channel %s (%s: %s); "
                    "reconnecting in %.1fs",
                    self._channel_label,
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
            except Exception:
                logger.exception(
                    "Unexpected error on Deepgram channel %s; reconnecting in %.1fs",
                    self._channel_label,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
        logger.info(
            "Deepgram channel %s finished: %s", self._channel_label, self.stats
        )

    # -- internals ---------------------------------------------------------

    async def _connect(self):
        """Open the websocket, tolerating the library's header-kwarg rename."""
        headers = {"Authorization": f"Token {self._api_key}"}
        try:
            try:
                return await websockets.connect(
                    self._url, additional_headers=headers, max_size=None
                )
            except TypeError:
                # websockets < 13 spells this `extra_headers`.
                return await websockets.connect(
                    self._url, extra_headers=headers, max_size=None
                )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                # Retrying a rejected key forever helps nobody - make it fatal.
                raise DeepgramError(
                    f"Deepgram rejected the API key (HTTP {status}). "
                    "Check DEEPGRAM_API_KEY."
                ) from exc
            if status == 400:
                raise DeepgramError(
                    f"Deepgram rejected the stream options (HTTP {status}). "
                    f"Check DEEPGRAM_MODEL ({self._url.split('model=')[-1][:20]}) "
                    "and DEEPGRAM_LANGUAGE."
                ) from exc
            raise

    async def _run_once(self) -> None:
        socket = await self._connect()
        self._connected.set()
        logger.info("Deepgram connected for channel %s", self._channel_label)
        if self._on_connect is not None:
            self._on_connect()
        sender = asyncio.create_task(
            self._sender(socket), name=f"dg-send-{self._channel_label}"
        )
        keepalive = asyncio.create_task(
            self._keepalive(socket), name=f"dg-ping-{self._channel_label}"
        )
        try:
            await self._receiver(socket)
        finally:
            self._connected.clear()
            for task in (sender, keepalive):
                task.cancel()
            await asyncio.gather(sender, keepalive, return_exceptions=True)
            await socket.close()

    async def _sender(self, socket) -> None:
        while True:
            frame = await self._queue.get()
            if frame is None:
                # Ask Deepgram to flush and close cleanly so trailing speech
                # is not lost when the meeting ends mid-sentence.
                await socket.send(json.dumps({"type": "CloseStream"}))
                return
            await socket.send(frame)

    async def _keepalive(self, socket) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            await socket.send(json.dumps({"type": "KeepAlive"}))

    async def _receiver(self, socket) -> None:
        async for raw in socket:
            if isinstance(raw, bytes):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "Ignoring non-JSON message from Deepgram on channel %s",
                    self._channel_label,
                )
                continue

            message_type = payload.get("type")
            if message_type == "Results":
                for segment in parse_results_message(
                    payload, default_speaker=self._default_speaker
                ):
                    self._segments_emitted += 1
                    result = self._on_segment(segment)
                    if asyncio.iscoroutine(result):
                        await result
            elif message_type == "Metadata":
                logger.debug(
                    "Deepgram metadata for channel %s: request_id=%s",
                    self._channel_label,
                    payload.get("request_id"),
                )
            elif message_type == "Error":
                logger.error(
                    "Deepgram reported an error on channel %s: %s",
                    self._channel_label,
                    payload,
                )


async def probe_credentials(
    api_key: str,
    *,
    model: str,
    language: str,
    sample_rate: int = 16_000,
    timeout_s: float = 15.0,
) -> str:
    """Open a real streaming connection and confirm Deepgram accepts the key.

    This is the offline half of a pre-flight: it exercises auth, TLS
    reachability, the model/language combination and our own result parsing,
    without a browser or a meeting. A synthetic tone stands in for speech -
    it transcribes to nothing, which is fine, because the point is that a
    ``Results`` frame comes back at all.

    Args:
        api_key: The Deepgram key to test.
        model: Model id, e.g. ``"nova-3"``.
        language: BCP-47 language tag.
        sample_rate: PCM sample rate to declare and generate.
        timeout_s: Give up if Deepgram sends nothing in this long.

    Returns:
        A human-readable description of what came back.

    Raises:
        DeepgramError: If the key is rejected, the model is unknown, or
            nothing arrives before ``timeout_s``.
    """
    import math
    import struct

    url = build_stream_url(
        model=model, language=language, sample_rate=sample_rate, diarize=False
    )
    # One second of a 440 Hz sine at -12 dBFS, as 16-bit little-endian mono.
    amplitude = int(0.25 * 32767)
    tone = b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * n / sample_rate)))
        for n in range(sample_rate)
    )

    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Token {api_key}"}
        ) as socket:
            for _ in range(3):
                await socket.send(tone)
            await socket.send(json.dumps({"type": "CloseStream"}))

            deadline = asyncio.get_running_loop().time() + timeout_s
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise DeepgramError(
                        f"Deepgram accepted the connection but sent no results "
                        f"within {timeout_s:.0f}s"
                    )
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                if isinstance(raw, bytes):
                    continue
                payload = json.loads(raw)
                if payload.get("type") == "Results":
                    return f"key accepted, {model}/{language} streamed a result frame"
                if payload.get("type") == "Metadata":
                    return f"key accepted, {model}/{language} closed cleanly"
    except DeepgramError:
        raise
    except InvalidStatus as exc:
        # Same status handling as the live client, so a key that fails here
        # fails there for the same stated reason.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            raise DeepgramError(
                f"Deepgram rejected the API key (HTTP {status}). "
                "Check DEEPGRAM_API_KEY."
            ) from exc
        if status == 400:
            raise DeepgramError(
                f"Deepgram rejected the request (HTTP 400) - check that model "
                f"{model!r} and language {language!r} are a valid pair."
            ) from exc
        raise DeepgramError(f"Deepgram refused the connection (HTTP {status})") from exc
    except asyncio.TimeoutError as exc:
        raise DeepgramError(
            f"Deepgram did not respond within {timeout_s:.0f}s"
        ) from exc
    except OSError as exc:
        raise DeepgramError(f"Could not reach Deepgram ({exc})") from exc
