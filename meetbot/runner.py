"""End-to-end orchestration: join, capture, transcribe, analyse.

:func:`run_meeting` wires the pieces together and, importantly, defines what
happens when each of them fails. The ordering guarantee this module exists to
provide is: **the raw transcript is always written and closed before the
analysis step runs**, so a failing LLM call, a missing API key or a network
outage costs you the summary and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meetbot.analysis.llm import LLMError, build_client
from meetbot.analysis.summarize import (
    AnalysisError,
    MeetingAnalysis,
    analyze_transcript,
    render_analysis_markdown,
)
from meetbot.capture.bridge import AudioBridge
from meetbot.config import Config
from meetbot.join import EndReason, JoinError, MeetSession, MeetingState
from meetbot.logging_setup import configure_logging
from meetbot.transcript.format import format_timestamp, render_markdown
from meetbot.transcript.store import (
    MeetingMeta,
    TranscriptStore,
    Utterance,
    read_utterances,
)

logger = logging.getLogger(__name__)

#: Warn if no audio has reached the bridge after this many seconds in-call.
_SILENT_CAPTURE_WARNING_S = 60.0


@dataclass
class MeetingRun:
    """Everything a completed (or failed) run produced."""

    output_dir: Path
    transcript_jsonl: Path
    transcript_markdown: Path | None = None
    analysis_markdown: Path | None = None
    end_reason: EndReason | None = None
    utterance_count: int = 0
    duration_s: float = 0.0
    analysis: MeetingAnalysis | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """True when the bot joined and captured at least one utterance."""
        return self.end_reason is not None and self.utterance_count > 0


def meeting_code(meet_url: str) -> str:
    """Extract the ``abc-defg-hij`` code from a Meet URL, for naming outputs."""
    match = re.search(r"([a-z]{3}-[a-z]{4}-[a-z]{3})", meet_url)
    if match:
        return match.group(1)
    match = re.search(r"/lookup/([A-Za-z0-9_-]+)", meet_url)
    return match.group(1) if match else "meeting"


def make_output_dir(config: Config) -> Path:
    """Create and return a timestamped directory for this run's artifacts."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = config.output_dir / f"{stamp}-{meeting_code(config.meet_url)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _install_signal_handlers(on_signal: Any) -> list[Any]:
    """Route SIGINT/SIGTERM to ``on_signal``, returning handlers to restore.

    Uses the asyncio loop's handler where available and falls back to
    :func:`signal.signal` on platforms that do not support it (notably Windows,
    where ``add_signal_handler`` raises ``NotImplementedError``).
    """
    restore: list[Any] = []
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, on_signal)
            restore.append(("loop", sig, None))
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                previous = signal.signal(sig, lambda *_: on_signal())
                restore.append(("signal", sig, previous))
            except (OSError, ValueError) as exc:
                logger.debug("Could not install a handler for %s: %s", signame, exc)
    return restore


def _restore_signal_handlers(restore: list[Any]) -> None:
    loop = asyncio.get_running_loop()
    for kind, sig, previous in restore:
        with contextlib.suppress(Exception):
            if kind == "loop":
                loop.remove_signal_handler(sig)
            else:
                signal.signal(sig, previous)


async def run_meeting(config: Config) -> MeetingRun:
    """Join the configured meeting and run the full pipeline.

    Args:
        config: A validated :class:`~meetbot.config.Config`.

    Returns:
        A :class:`MeetingRun` describing what was produced. Recoverable
        failures (analysis errors, a denied join after audio was captured) are
        recorded in :attr:`MeetingRun.errors` rather than raised, so the caller
        can still report the artifacts that *were* written.
    """
    output_dir = make_output_dir(config)
    configure_logging(config.log_level, log_file=output_dir / "meetbot.log")
    logger.info("Run artifacts will be written to %s", output_dir)
    logger.debug("Effective configuration: %s", config.redacted())

    transcript_path = output_dir / "transcript.jsonl"
    run = MeetingRun(output_dir=output_dir, transcript_jsonl=transcript_path)
    started = time.monotonic()

    store = TranscriptStore(transcript_path)
    store.write_meta(MeetingMeta(meet_url=config.meet_url, bot_name=config.bot_name))

    session = MeetSession(
        config, on_event=lambda kind, details: store.append_event(kind, **details)
    )

    async def on_utterance(utterance: Utterance) -> None:
        if not config.live_captions_overlay:
            return
        updated = await session.update_caption(utterance.speaker, utterance.text)
        if not updated:
            logger.warning("Caption overlay not updated (best effort)")

    bridge = AudioBridge(
        config,
        store,
        on_utterance=on_utterance if config.live_captions_overlay else None,
    )
    stop_handlers: list[Any] = []

    try:
        try:
            await bridge.start()
        except OSError as exc:
            raise RuntimeError(
                f"Could not bind the audio bridge to {config.bridge_url}: {exc}. "
                "Another process may be using the port - set BRIDGE_PORT."
            ) from exc

        stop_handlers = _install_signal_handlers(session.request_stop)

        await session.start()
        try:
            await session.join()
        except JoinError as exc:
            logger.error("Could not join the meeting: %s", exc)
            store.append_event("join_failed", outcome=exc.outcome.value, error=str(exc))
            run.errors.append(str(exc))
            return run

        if config.per_participant_audio:
            names = await session.read_participant_names()
            unique = sorted(set(names))
            if len(unique) == 1:
                bridge.sole_participant_name = unique[0]
                logger.info("Labelling speech as %r (only other participant)", unique[0])
            elif unique:
                logger.info(
                    "%d other participants (%s); speakers stay generic - nothing "
                    "links an audio track to a name in Meet's DOM",
                    len(unique),
                    ", ".join(unique),
                )

        if config.live_captions_overlay:
            if await session.start_caption_overlay():
                store.append_event("caption_overlay_started")
            else:
                run.errors.append(
                    "Could not start the caption overlay; captions were "
                    "captured but never made visible in the call"
                )

        run.end_reason = await session.wait_until_meeting_ends(
            on_poll=_make_health_check(bridge, session, store)
        )
        store.append_event("meeting_ended", reason=run.end_reason.value)
        logger.info("Meeting finished: %s", run.end_reason.value)

        await session.leave()

    except Exception as exc:
        logger.exception("The meeting run failed")
        run.errors.append(f"{type(exc).__name__}: {exc}")
        with contextlib.suppress(Exception):
            store.append_event("run_failed", error=repr(exc))
    finally:
        # Shut down in dependency order: stop the browser producing audio,
        # then flush Deepgram, then close the transcript.
        if stop_handlers:
            _restore_signal_handlers(stop_handlers)
        with contextlib.suppress(Exception):
            await session.close()
        with contextlib.suppress(Exception):
            await bridge.stop()
        if bridge.fatal_error is not None:
            run.errors.append(f"Transcription error: {bridge.fatal_error}")
        if not bridge.received_any_audio:
            message = (
                "No audio ever reached the bridge. The browser-side WebRTC hook "
                "may have failed to install (check meetbot.browser log lines), "
                "or nobody spoke."
            )
            logger.warning(message)
            run.errors.append(message)
        store.close()

    run.duration_s = time.monotonic() - started
    _write_transcript_markdown(config, run)
    await _maybe_analyse(config, run)
    return run


def _make_health_check(
    bridge: AudioBridge, session: MeetSession, store: TranscriptStore
):
    """Build the per-poll callback that watches for a dead capture path."""
    first_poll = time.monotonic()
    warned = False
    last_count: int | None = None

    async def check(state: MeetingState) -> None:
        nonlocal warned, last_count

        if state.participant_count != last_count:
            last_count = state.participant_count
            if state.participant_count is not None:
                store.append_event(
                    "participant_count", count=state.participant_count
                )
                logger.info("Participants in call: %d", state.participant_count)

        if warned or bridge.received_any_audio:
            return
        if time.monotonic() - first_poll < _SILENT_CAPTURE_WARNING_S:
            return

        warned = True
        stats = await session.capture_stats()
        logger.warning(
            "No audio captured after %.0fs in the call. Browser hook stats: %s",
            _SILENT_CAPTURE_WARNING_S,
            stats,
        )
        store.append_event("capture_silent", browser_stats=stats)

    return check


def _write_transcript_markdown(config: Config, run: MeetingRun) -> None:
    """Render the JSONL transcript to Markdown, tolerating a failure."""
    try:
        utterances = read_utterances(run.transcript_jsonl)
    except (OSError, FileNotFoundError) as exc:
        logger.error("Could not read back the transcript: %s", exc)
        run.errors.append(f"Could not read the transcript: {exc}")
        return

    run.utterance_count = len(utterances)
    captured = max((u.end for u in utterances), default=0.0)
    markdown_path = run.output_dir / "transcript.md"
    try:
        markdown_path.write_text(
            render_markdown(
                utterances,
                title="Meeting Transcript",
                meta={
                    "meeting_url": config.meet_url,
                    "recorded_by": config.bot_name,
                    "captured_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            ),
            encoding="utf-8",
        )
        run.transcript_markdown = markdown_path
        logger.info(
            "Wrote %s (%d utterance(s), %s captured)",
            markdown_path,
            len(utterances),
            format_timestamp(captured),
        )
    except OSError as exc:
        logger.error("Could not write the Markdown transcript: %s", exc)
        run.errors.append(f"Could not write transcript.md: {exc}")


async def _maybe_analyse(config: Config, run: MeetingRun) -> None:
    """Run the LLM analysis, downgrading any failure to a recorded error."""
    if not config.analysis_enabled:
        logger.info("Analysis is disabled; skipping the summary step")
        return
    if run.utterance_count == 0:
        logger.warning("No utterances captured; skipping analysis")
        run.errors.append("Analysis skipped: the transcript is empty")
        return

    try:
        utterances = read_utterances(run.transcript_jsonl)
        captured = max((u.end for u in utterances), default=0.0)
        # The provider SDKs are synchronous; keep the event loop free.
        analysis = await asyncio.to_thread(
            _analyse_sync, config, utterances, format_timestamp(captured)
        )
    except (AnalysisError, LLMError) as exc:
        logger.error("Analysis failed: %s", exc)
        logger.info(
            "The raw transcript is unaffected: %s. Re-run the analysis later "
            "with: python -m meetbot analyze --transcript %s",
            run.transcript_jsonl,
            run.transcript_jsonl,
        )
        run.errors.append(f"Analysis failed: {exc}")
        return
    except Exception as exc:
        logger.exception("Unexpected error during analysis")
        run.errors.append(f"Analysis failed unexpectedly: {type(exc).__name__}: {exc}")
        return

    run.analysis = analysis
    analysis_path = run.output_dir / "analysis.md"
    try:
        analysis_path.write_text(
            render_analysis_markdown(
                analysis,
                meta={
                    "meeting_url": config.meet_url,
                    "utterances": run.utterance_count,
                    "generated_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            ),
            encoding="utf-8",
        )
        run.analysis_markdown = analysis_path
        logger.info("Wrote %s", analysis_path)
    except OSError as exc:
        logger.error("Could not write the analysis: %s", exc)
        run.errors.append(f"Could not write analysis.md: {exc}")


def _analyse_sync(
    config: Config, utterances: list[Any], duration: str
) -> MeetingAnalysis:
    """Blocking analysis call, run in a worker thread."""
    client = build_client(
        config.llm_provider,
        config.llm_api_key,
        config.llm_model,
        config.llm_base_url,
    )
    return analyze_transcript(
        utterances,
        client,
        provider=config.llm_provider,
        meeting_url=config.meet_url,
        duration=duration,
    )
