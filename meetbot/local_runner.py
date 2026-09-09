"""Record a meeting from this machine, end to end, without joining it.

This is the sibling of :mod:`meetbot.runner`. That one drives a browser: sign
a Google account in, join the call, tap the page's audio. This one drives
nothing - it listens to what the speakers are already playing and what the
microphone is already hearing, so there is no account to expire, no lobby to
be admitted through, no DOM to break, and no extra participant in the call.

Everything downstream is shared. The transcript store, the Markdown renderer
and the analysis step do not care where the audio came from, so a locally
recorded meeting produces exactly the same artifacts as a bot-recorded one
and the notepad works on it unchanged.

Two things are genuinely better here than on the bot path:

**Speaker attribution for you is exact.** The microphone and the speakers stay
on separate channels all the way to Deepgram, so your own words are labelled
``You`` because of where they came from, not because a model guessed. Only the
far side needs diarization.

**It works for any meeting tool.** Zoom, Teams, Meet, a phone on speaker - all
of it is just audio arriving at the render endpoint.

The trade is that it captures whatever the machine plays, including a video in
another tab, and that it hears nothing at all if the speakers are muted. See
:mod:`meetbot.capture.local` for why, and :func:`meetbot.capture.local.probe_loopback`
for catching it before a meeting rather than after one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from meetbot.capture.deepgram import DeepgramLiveClient, TranscriptSegment
from meetbot.capture.local import (
    CHANNEL_COUNT,
    TARGET_RATE,
    AudioDevice,
    LocalCaptureError,
    LocalRecorder,
    default_devices,
    default_output_name,
    resolve_or_default,
)
from meetbot.config import Config
from meetbot.logging_setup import configure_logging
from meetbot.runner import (
    MeetingRun,
    _maybe_analyse,
    _write_transcript_markdown,
)
from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

logger = logging.getLogger(__name__)

#: Label for audio captured from your own microphone. Not a guess: the
#: channel is the attribution.
SELF_SPEAKER = "You"

#: Per-channel speaker labels handed to Deepgram. An empty entry means "use
#: diarization for this channel", which is what the far side needs.
CHANNEL_SPEAKERS = (SELF_SPEAKER, "")

#: How long the speakers may be silent before saying so. Long enough not to
#: nag during a genuine pause, short enough to catch muted output early.
SILENCE_WARN_S = 45.0

#: How often to check that playback is still coming out of the device being
#: recorded. Bluetooth headphones drop without warning.
DEVICE_CHECK_S = 10.0


def slugify(title: str, fallback: str = "local") -> str:
    """Turn a meeting title into something safe for a directory name."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug[:40] or fallback


def make_local_output_dir(config: Config, title: str = "") -> Path:
    """Create a timestamped directory for this recording's artifacts."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = config.output_dir / f"{stamp}-{slugify(title)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_devices(
    loopback_index: int | None = None, microphone_index: int | None = None
) -> tuple[AudioDevice, AudioDevice | None]:
    """Pick the devices to record, by index or by asking Windows.

    Thin alias for :func:`meetbot.capture.local.resolve_or_default`, kept
    because the runner's callers name it this way.

    Raises:
        LocalCaptureError: If there is no usable speaker loopback, which is
            the one device this cannot work without.
    """
    return resolve_or_default(loopback_index, microphone_index)


async def run_local_meeting(
    config: Config,
    *,
    title: str = "",
    loopback_index: int | None = None,
    microphone_index: int | None = None,
    on_stop: Callable[[Callable[[], None]], None] | None = None,
    on_output_dir: Callable[[Path], None] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    reconfigure_logging: bool = True,
) -> MeetingRun:
    """Record this machine until stopped, then transcribe and write it up.

    Args:
        config: A validated :class:`~meetbot.config.Config`. Only the audio
            and LLM settings are used; nothing here needs a meeting URL.
        title: What to call this recording. Also names the output directory.
        loopback_index: Device index of the speakers to tap. Defaults to
            whatever Windows is currently playing through.
        microphone_index: Device index of your microphone. Defaults to the
            system default; pass a negative value to record no microphone.
        on_stop: Called once with a zero-argument callable that ends the
            recording. This is the only way to stop it - unlike a call, a
            local recording has no natural end.
        on_output_dir: Called with the artifact directory as soon as it
            exists, so a caller can show partial results while recording.
        on_event: Called with each lifecycle event as it happens.
        reconfigure_logging: Set up per-run file logging. False for the
            service, which keeps its own configuration.

    Returns:
        A :class:`~meetbot.runner.MeetingRun` describing what was produced.

    Raises:
        LocalCaptureError: If this machine cannot be recorded from.
    """
    loopback, microphone = resolve_devices(loopback_index, microphone_index)

    output_dir = make_local_output_dir(config, title)
    if on_output_dir is not None:
        on_output_dir(output_dir)
    if reconfigure_logging:
        configure_logging(config.log_level, output_dir / "meetbot.log")

    run = MeetingRun(
        output_dir=output_dir, transcript_jsonl=output_dir / "transcript.jsonl"
    )
    logger.info("Recording this machine - no bot will join any meeting")
    logger.info("Speakers  : %s", loopback.name)
    logger.info("Microphone: %s", microphone.name if microphone else "(none)")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    if on_stop is not None:
        on_stop(lambda: loop.call_soon_threadsafe(stop_event.set))

    store = TranscriptStore(run.transcript_jsonl)
    store.write_meta(
        MeetingMeta(
            # There is no URL. The title is what identifies this recording,
            # and the meta record is what the UI reads for a name.
            meet_url=title or "Local recording",
            bot_name="Scribe (local capture)",
        )
    )

    def emit(kind: str, **details: Any) -> None:
        store.append_event(kind, **details)
        if on_event is not None:
            on_event(kind, details)

    emit(
        "local_capture_started",
        speakers=loopback.name,
        microphone=microphone.name if microphone else None,
    )

    started = time.monotonic()
    # Deepgram timestamps restart at zero on every reconnect; this rebases
    # them onto one continuous timeline for the whole recording.
    offset = 0.0

    def on_connect() -> None:
        nonlocal offset
        offset = time.monotonic() - started
        logger.info("Deepgram stream established at +%.1fs", offset)

    def on_segment(segment: TranscriptSegment) -> None:
        store.append(
            Utterance(
                speaker=segment.speaker,
                text=segment.text,
                start=segment.start + offset,
                end=segment.end + offset,
                channel="local",
                confidence=segment.confidence,
            )
        )
        run.utterance_count += 1

    client = DeepgramLiveClient(
        api_key=config.deepgram_api_key,
        on_segment=on_segment,
        on_connect=on_connect,
        model=config.deepgram_model,
        language=config.deepgram_language,
        sample_rate=TARGET_RATE,
        channels=CHANNEL_COUNT,
        multichannel=True,
        channel_speakers=CHANNEL_SPEAKERS,
        # Only the far side needs guessing at; the microphone channel names
        # itself, and parse_results_message ignores diarization there.
        diarize=True,
        default_speaker="Speaker 0",
        channel_label="local",
        keyterms=config.deepgram_keyterms,
    )

    recorder = LocalRecorder(loopback, microphone)
    pump_error: list[str] = []

    def pump() -> None:
        """Move captured blocks onto the event loop.

        ``send_audio`` writes to an asyncio.Queue, which is not thread-safe,
        so blocks are handed over with call_soon_threadsafe rather than being
        pushed in directly from the capture thread.
        """
        try:
            for block in recorder.blocks():
                if stop_event.is_set():
                    break
                loop.call_soon_threadsafe(client.send_audio, block)
        except Exception as exc:  # noqa: BLE001 - reported, never raised here
            pump_error.append(f"{type(exc).__name__}: {exc}")
            logger.exception("Audio pump failed")
            loop.call_soon_threadsafe(stop_event.set)

    transcription = asyncio.create_task(client.run())
    try:
        recorder.start()
    except LocalCaptureError:
        transcription.cancel()
        store.close()
        raise

    pump_thread = threading.Thread(target=pump, name="local-pump", daemon=True)
    pump_thread.start()
    watchdog = asyncio.create_task(_warn_if_silent(recorder, stop_event, emit))

    try:
        await stop_event.wait()
    finally:
        logger.info("Stopping the recording")
        recorder.stop()
        pump_thread.join(timeout=5)
        watchdog.cancel()
        await client.stop()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await transcription
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await watchdog

        stats = recorder.stats()
        emit("local_capture_stopped", **stats)
        run.duration_s = float(stats.get("elapsed_s") or 0.0)
        store.close()

    logger.info(
        "Captured %d utterance(s) in %s", run.utterance_count, run.output_dir
    )
    for label in ("them", "me"):
        source = stats.get(label)
        if isinstance(source, dict) and source.get("error"):
            run.errors.append(f"{label} capture failed: {source['error']}")
    run.errors.extend(pump_error)

    them = stats.get("them") or {}
    if run.utterance_count == 0 and not them.get("peak"):
        run.errors.append(
            "Nothing was recorded: the speakers produced silence for the whole "
            "session. Loopback captures the mix after the volume control, so "
            "muted or zero-volume output records nothing."
        )

    # A local recording ends because somebody stopped it, which is a normal
    # ending rather than the bot being ejected - so it has no EndReason. The
    # transcript is what says whether it worked.
    _write_transcript_markdown(config, run)
    await _maybe_analyse(config, run)
    return run


async def _warn_if_silent(
    recorder: LocalRecorder,
    stop_event: asyncio.Event,
    emit: Callable[..., None],
) -> None:
    """Watch for the two ways a recording silently produces nothing.

    Both end the same way - a perfectly well-formed empty transcript, with
    nothing looking wrong until the meeting is over - so both are worth
    interrupting for.

    **The output device moved.** Bluetooth headphones disconnect, Windows
    switches playback to the speakers, and the capture stream stays open on a
    device nobody is listening to any more. This is checked by name rather
    than inferred from silence, because it is exactly knowable.

    **Nothing is coming out at all.** Muted output, or zero volume. Reported
    once after :data:`SILENCE_WARN_S`, and again if it recurs, using a peak
    that resets on each check so a source that has *gone* quiet is
    distinguishable from one that never worked.
    """
    capturing = recorder.capturing
    silent_for = 0.0
    warned_silent = False
    warned_device = False

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=DEVICE_CHECK_S)
            return
        except asyncio.TimeoutError:
            pass

        if not warned_device:
            current = await asyncio.to_thread(default_output_name)
            # The loopback device is the render device's name plus a suffix,
            # so containment is the right test rather than equality.
            if current and current not in capturing:
                warned_device = True
                message = (
                    f"Windows is now playing through {current!r}, but this "
                    f"recording is capturing {capturing!r}. If the output "
                    "device changed - Bluetooth headphones disconnecting, "
                    "say - the rest of this recording will be silent. Stop "
                    "and start again to follow the new device."
                )
                logger.warning(message)
                emit("local_capture_device_changed", detail=message, now=current)

        if recorder.recent_peak() > 2:
            silent_for = 0.0
            warned_silent = False
            continue

        silent_for += DEVICE_CHECK_S
        if silent_for >= SILENCE_WARN_S and not warned_silent:
            warned_silent = True
            message = (
                f"No audio from the speakers for {int(silent_for)}s. If the "
                "meeting is audible, check the output is not muted - loopback "
                "captures the mix after the volume control."
            )
            logger.warning(message)
            emit("local_capture_silent", detail=message)



__all__ = [
    "CHANNEL_SPEAKERS",
    "DEVICE_CHECK_S",
    "SELF_SPEAKER",
    "SILENCE_WARN_S",
    "make_local_output_dir",
    "resolve_devices",
    "run_local_meeting",
    "slugify",
]
