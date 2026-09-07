"""Local WebSocket bridge between the browser hook and Deepgram.

The injected page script (``inject.js``) connects to a loopback WebSocket
server hosted here and streams PCM. This module decodes those frames, keeps one
:class:`~meetbot.capture.deepgram.DeepgramLiveClient` per audio channel, rebases
Deepgram's per-stream timestamps onto a single meeting timeline, and appends
finalised utterances to the transcript store.

Wire protocol (mirror of the comment at the top of ``inject.js``):

* Text frames are JSON control messages with a ``type`` field.
* Binary frames are ``[uint8 channel_id][int16 LE mono PCM...]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final

import websockets

from meetbot.capture.deepgram import (
    DeepgramError,
    DeepgramLiveClient,
    TranscriptSegment,
)
from meetbot.config import Config
from meetbot.transcript.store import TranscriptStore, Utterance

logger = logging.getLogger(__name__)

#: Channel id reserved for the single mixed stream.
MIXED_CHANNEL: Final[int] = 0

#: Bytes per PCM sample (16-bit).
_BYTES_PER_SAMPLE: Final[int] = 2


@dataclass
class ChannelState:
    """Bookkeeping for one audio channel.

    Deepgram reports segment timestamps relative to the *current* stream, which
    restarts at zero after a reconnect. To place an utterance on the meeting
    timeline we need two offsets: when this channel first produced audio
    (``meeting_offset_s``), and how much audio we had already forwarded on this
    channel when the current Deepgram stream opened (``stream_epoch_s``).
    """

    channel_id: int
    label: str
    client: DeepgramLiveClient
    task: asyncio.Task[None]
    meeting_offset_s: float = 0.0
    seconds_forwarded: float = 0.0
    stream_epoch_s: float = 0.0
    frames: int = 0

    def meeting_time(self, stream_relative_s: float) -> float:
        """Map a stream-relative timestamp onto the meeting timeline."""
        return self.meeting_offset_s + self.stream_epoch_s + stream_relative_s


@dataclass
class BridgeStats:
    """Summary counters, logged when the bridge shuts down."""

    frames: int = 0
    bytes_received: int = 0
    utterances: int = 0
    channels: int = 0
    browser_connections: int = 0
    decode_errors: int = 0
    labels: dict[int, str] = field(default_factory=dict)


class AudioBridge:
    """Hosts the loopback WebSocket server and fans audio out to Deepgram.

    Lifecycle::

        bridge = AudioBridge(config, store)
        await bridge.start()          # binds the port
        ...                           # meeting runs
        await bridge.stop()           # flushes Deepgram, closes connections
    """

    def __init__(
        self,
        config: Config,
        store: TranscriptStore,
        on_utterance: Callable[[Utterance], Any] | None = None,
    ) -> None:
        """Create a bridge.

        Args:
            config: Runtime configuration (port, sample rate, Deepgram keys).
            store: Transcript store that finalised utterances are appended to.
            on_utterance: Called with each finalised utterance, after it is
                persisted. May return a coroutine, in which case it is
                awaited. Used to mirror captions somewhere live (e.g. into
                the Meet chat panel); a failure in here is logged and never
                allowed to interrupt capture.
        """
        self._config = config
        self._store = store
        self._on_utterance = on_utterance
        self._server: Any | None = None
        self._channels: dict[int, ChannelState] = {}
        self._pending_labels: dict[int, str] = {}
        self._started_at: float | None = None
        self._stopping = False
        self._fatal: BaseException | None = None
        self.stats = BridgeStats()
        #: Real display name to label per-participant channels with, set by
        #: the runner once the roster is read and shows exactly one other
        #: person. ``None`` leaves the generic "participant-N" labels.
        self.sole_participant_name: str | None = None
        #: Optional ``(start, end) -> name | None`` lookup, set by the runner
        #: when caption-based speaker attribution is enabled. Returning
        #: ``None`` leaves whatever label diarization produced, so a missing
        #: or unmatched caption degrades to "Speaker 0" instead of guessing.
        self.speaker_resolver: Callable[[float, float], str | None] | None = None

    def meeting_elapsed(self) -> float:
        """Seconds since capture started - the clock ``Utterance.start`` uses.

        Anything feeding :attr:`speaker_resolver` must timestamp its
        observations with this, or the overlap arithmetic compares two
        unrelated clocks.
        """
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind the loopback WebSocket server.

        Raises:
            OSError: If the port is already in use.
        """
        self._started_at = time.monotonic()
        self._server = await websockets.serve(
            self._handle_client,
            self._config.bridge_host,
            self._config.bridge_port,
            max_size=None,
            # The browser sends continuously; a short ping keeps a wedged
            # connection from looking healthy.
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info(
            "Audio bridge listening on %s (sample_rate=%d, per_participant=%s)",
            self._config.bridge_url,
            self._config.sample_rate,
            self._config.per_participant_audio,
        )

    async def stop(self) -> None:
        """Flush Deepgram, close every channel, and shut the server down."""
        if self._stopping:
            return
        self._stopping = True

        for state in list(self._channels.values()):
            await state.client.stop()

        tasks = [state.task for state in self._channels.values()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=15.0)
            for task in pending:
                logger.warning(
                    "Deepgram task did not finish within 15s; cancelling: %s",
                    task.get_name(),
                )
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    logger.error("Deepgram task ended with an error: %s", exc)

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        self.stats.channels = len(self._channels)
        self.stats.labels = {cid: s.label for cid, s in self._channels.items()}
        logger.info(
            "Audio bridge stopped: %d frame(s), %.1f KiB, %d utterance(s), "
            "%d channel(s)",
            self.stats.frames,
            self.stats.bytes_received / 1024,
            self.stats.utterances,
            len(self._channels),
        )

    @property
    def fatal_error(self) -> BaseException | None:
        """A non-recoverable transcription error, if one occurred."""
        return self._fatal

    @property
    def received_any_audio(self) -> bool:
        """Whether at least one PCM frame arrived from the browser."""
        return self.stats.frames > 0

    # -- server handlers ---------------------------------------------------

    async def _handle_client(self, websocket: Any, *_: Any) -> None:
        """Serve one browser connection until it closes.

        The signature tolerates both the two-argument (``websocket, path``)
        handler of websockets < 13 and the single-argument form used since.
        """
        self.stats.browser_connections += 1
        peer = getattr(websocket, "remote_address", None)
        logger.info("Browser audio hook connected from %s", peer)
        try:
            async for message in websocket:
                if isinstance(message, (bytes, bytearray)):
                    self._handle_audio_frame(bytes(message))
                else:
                    self._handle_control_message(message)
        except websockets.exceptions.ConnectionClosedError as exc:
            logger.warning("Browser audio hook disconnected abruptly: %s", exc)
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("Browser audio hook disconnected")
        finally:
            logger.info(
                "Browser connection closed after %d frame(s)", self.stats.frames
            )

    def _handle_control_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring non-JSON control message from browser")
            return
        if not isinstance(payload, dict):
            logger.warning("Ignoring control message that is not an object")
            return

        kind = payload.get("type")
        if kind == "hello":
            logger.info(
                "Browser hook ready (sample_rate=%s, per_participant=%s)",
                payload.get("sampleRate"),
                payload.get("perParticipant"),
            )
        elif kind == "channel":
            channel_id = payload.get("id")
            label = payload.get("label")
            if isinstance(channel_id, int) and isinstance(label, str):
                self.set_channel_label(channel_id, label)
        elif kind == "track_ended":
            logger.info("Remote track ended: %s", payload.get("trackId"))
            self._store.append_event("track_ended", track_id=payload.get("trackId"))
        else:
            logger.debug("Unhandled control message: %s", kind)

    def _handle_audio_frame(self, frame: bytes) -> None:
        if len(frame) < 1 + _BYTES_PER_SAMPLE:
            self.stats.decode_errors += 1
            if self.stats.decode_errors % 100 == 1:
                logger.warning(
                    "Discarding undersized audio frame (%d bytes); %d so far",
                    len(frame),
                    self.stats.decode_errors,
                )
            return

        channel_id = frame[0]
        pcm = frame[1:]
        if len(pcm) % _BYTES_PER_SAMPLE:
            # A partial sample means a framing bug; drop the odd trailing byte
            # rather than shifting every subsequent sample by one byte.
            pcm = pcm[: len(pcm) - (len(pcm) % _BYTES_PER_SAMPLE)]

        self.stats.frames += 1
        self.stats.bytes_received += len(pcm)

        state = self._channels.get(channel_id)
        if state is None:
            if self._stopping:
                return
            state = self._open_channel(channel_id)
            if state is None:
                return

        state.frames += 1
        state.seconds_forwarded += len(pcm) / (
            self._config.sample_rate * _BYTES_PER_SAMPLE
        )
        state.client.send_audio(pcm)

    # -- channels ----------------------------------------------------------

    def set_channel_label(self, channel_id: int, label: str) -> None:
        """Name a channel, e.g. after resolving a participant's display name.

        Safe to call before the channel exists; the label is applied when the
        channel opens.
        """
        state = self._channels.get(channel_id)
        if state is None:
            self._pending_labels[channel_id] = label
            return
        if state.label != label:
            logger.info("Channel %d relabelled %r -> %r", channel_id, state.label, label)
            state.label = label

    def _open_channel(self, channel_id: int) -> ChannelState | None:
        """Start a Deepgram stream for a newly-seen channel."""
        default_label = "mixed" if channel_id == MIXED_CHANNEL else (
            # Only safe when exactly one other person is in the call: with
            # several, nothing links a given audio track to a given name, so
            # the generic label is the honest answer.
            self.sole_participant_name or f"participant-{channel_id}"
        )
        label = self._pending_labels.pop(channel_id, None) or default_label
        # A dedicated per-participant channel already has exactly one speaker,
        # so diarization would only add spurious splits. The mixed stream needs
        # it - that is the only way speakers get told apart there.
        per_participant = channel_id != MIXED_CHANNEL
        diarize = not per_participant
        default_speaker = label if per_participant else "Speaker 0"

        # Explicit None check: `self._started_at or ...` would treat a
        # legitimate 0.0 start time as "unset".
        now = time.monotonic()
        elapsed = now - (self._started_at if self._started_at is not None else now)

        placeholder: dict[str, ChannelState] = {}

        def on_segment(segment: TranscriptSegment) -> Any:
            # Returning the (possibly coroutine) result lets deepgram.py's
            # receiver await it, so on_utterance can be async without this
            # closure needing to be.
            return self._record_segment(channel_id, segment)

        def on_connect() -> None:
            state = placeholder.get("state")
            if state is not None:
                state.stream_epoch_s = state.seconds_forwarded

        try:
            client = DeepgramLiveClient(
                api_key=self._config.deepgram_api_key,
                on_segment=on_segment,
                model=self._config.deepgram_model,
                language=self._config.deepgram_language,
                sample_rate=self._config.sample_rate,
                diarize=diarize,
                default_speaker=default_speaker,
                channel_label=label,
                on_connect=on_connect,
                endpointing_ms=self._config.deepgram_endpointing_ms,
                utterance_end_ms=self._config.deepgram_utterance_end_ms,
            )
        except Exception:
            logger.exception("Could not create Deepgram client for channel %d", channel_id)
            return None

        task = asyncio.create_task(
            self._supervise(client), name=f"deepgram-{label}"
        )
        state = ChannelState(
            channel_id=channel_id,
            label=label,
            client=client,
            task=task,
            meeting_offset_s=max(0.0, elapsed),
        )
        placeholder["state"] = state
        self._channels[channel_id] = state
        logger.info(
            "Opened audio channel %d (%s, diarize=%s) at t+%.1fs",
            channel_id,
            label,
            diarize,
            elapsed,
        )
        self._store.append_event(
            "channel_opened",
            channel_id=channel_id,
            label=label,
            diarize=diarize,
            offset_s=round(elapsed, 3),
        )
        return state

    async def _supervise(self, client: DeepgramLiveClient) -> None:
        """Run one Deepgram client, recording fatal errors for the caller."""
        try:
            await client.run()
        except DeepgramError as exc:
            # A bad key or a rejected model will not fix itself; surface it so
            # the run can end with a clear message instead of silence.
            logger.error(
                "Fatal transcription error on channel %s: %s",
                client.channel_label,
                exc,
            )
            self._fatal = exc
            self._store.append_event(
                "transcription_failed",
                channel=client.channel_label,
                error=str(exc),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Deepgram client for channel %s crashed", client.channel_label
            )
            self._fatal = exc
            self._store.append_event(
                "transcription_failed",
                channel=client.channel_label,
                error=repr(exc),
            )

    def _record_segment(self, channel_id: int, segment: TranscriptSegment) -> Any:
        """Persist one finalised segment to the transcript store.

        Returns whatever ``on_utterance`` returns (a coroutine, if it is
        async), so the caller can await it; ``None`` otherwise.
        """
        state = self._channels.get(channel_id)
        if state is None:
            logger.warning("Segment for unknown channel %d; dropping", channel_id)
            return None

        speaker = segment.speaker
        if channel_id != MIXED_CHANNEL:
            # Per-participant capture: the channel label *is* the speaker.
            speaker = state.label

        start = round(state.meeting_time(segment.start), 3)
        end = round(state.meeting_time(segment.end), 3)

        # Meet's own captions carry the speaker names that the audio path
        # cannot see. When the timeline can name whoever was talking across
        # this window, that real name beats a diarization index.
        if self.speaker_resolver is not None:
            try:
                resolved = self.speaker_resolver(start, end)
            except Exception:  # noqa: BLE001 - attribution must never stop capture
                logger.exception("Speaker resolver failed; keeping %r", speaker)
            else:
                if resolved:
                    speaker = resolved

        utterance = Utterance(
            speaker=speaker,
            text=segment.text,
            start=start,
            end=end,
            channel=state.label,
            confidence=segment.confidence,
        )
        try:
            self._store.append(utterance)
        except (OSError, ValueError):
            logger.exception("Could not persist utterance from channel %d", channel_id)
            return None

        self.stats.utterances += 1
        logger.info("[%s] %s: %s", state.label, speaker, segment.text)

        if self._on_utterance is not None:
            return self._on_utterance(utterance)
        return None
