"""Command-line entrypoint for meetbot.

Subcommands:

``run``
    Join a meeting and produce a transcript plus analysis.
``analyze``
    Re-run the LLM analysis over an existing transcript - the recovery path
    when a live run captured audio but the summary step failed.
``format``
    Re-render a stored transcript as Markdown or plain text.
``check``
    Validate configuration and the Playwright install without joining
    anything.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from meetbot import __version__
from meetbot.analysis.llm import LLMError, build_client, probe_llm
from meetbot.analysis.summarize import (
    AnalysisError,
    analyze_transcript,
    render_analysis_markdown,
)
from meetbot.capture.deepgram import DeepgramError, probe_credentials
from meetbot.config import Config, ConfigError, load_config
from meetbot.logging_setup import configure_logging
from meetbot.runner import MeetingRun, run_meeting
from meetbot.transcript.format import format_timestamp, render_markdown, render_text
from meetbot.transcript.store import read_meta, read_utterances

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RUN_FAILED = 3
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="python -m meetbot",
        description=(
            "Join a Google Meet call as a guest, transcribe it live, and "
            "generate a post-meeting summary."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"meetbot {__version__}")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to a .env file (default: search upwards from the cwd).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Join a meeting and record it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    run_parser.add_argument(
        "--url", dest="meet_url", default=None, help="Google Meet URL to join."
    )
    run_parser.add_argument(
        "--name",
        dest="bot_name",
        default=None,
        help=(
            "Display name. Must disclose that the bot records; a disclosure "
            "suffix is appended if it does not."
        ),
    )
    run_parser.add_argument(
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=None,
        help="Directory that per-meeting output folders are created in.",
    )
    run_parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=None,
        help="Run Chromium headless.",
    )
    run_parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Show the browser window - the way to debug broken selectors.",
    )
    run_parser.add_argument(
        "--admission-timeout",
        dest="admission_timeout_s",
        type=int,
        default=None,
        help="Seconds to wait for a host to admit the bot.",
    )
    run_parser.add_argument(
        "--max-duration",
        dest="max_meeting_duration_s",
        type=int,
        default=None,
        help="Hard cap on meeting length, in seconds.",
    )
    run_parser.add_argument(
        "--per-participant",
        dest="per_participant_audio",
        action="store_true",
        default=None,
        help=(
            "Give each remote participant their own Deepgram stream instead of "
            "diarizing one mixed stream. Uses one connection per participant."
        ),
    )
    run_parser.add_argument(
        "--live-captions-overlay",
        dest="live_captions_overlay",
        action="store_true",
        default=None,
        help=(
            "Share a live-caption canvas as a screen-share (via 'Present "
            "now') and update it as utterances are transcribed - visible to "
            "every participant including the host, without turning the "
            "bot's own camera on."
        ),
    )
    run_parser.add_argument(
        "--no-speaker-names",
        dest="speaker_names_from_captions",
        action="store_false",
        default=None,
        help=(
            "Do not turn on Meet's live captions to recover real speaker "
            "names; leave utterances labelled Speaker 0 / Speaker 1. "
            "Captions are per-viewer and do not affect other participants."
        ),
    )
    run_parser.add_argument(
        "--anonymise-analysis",
        "--anonymize-analysis",
        dest="anonymise_analysis",
        action="store_true",
        default=None,
        help=(
            "Replace real names with 'Speaker A' / 'Speaker B' in the text "
            "sent to the LLM. The local transcript keeps the real names; "
            "only the third-party request is anonymised."
        ),
    )
    run_parser.add_argument(
        "--no-analysis",
        dest="analysis_enabled",
        action="store_false",
        default=None,
        help="Capture the transcript but skip the LLM analysis.",
    )
    _add_llm_flags(run_parser)

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Re-run the analysis over an existing transcript.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    analyze_parser.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="Path to a transcript.jsonl produced by a previous run.",
    )
    analyze_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write the analysis (default: analysis.md beside the input).",
    )
    _add_llm_flags(analyze_parser)

    format_parser = subparsers.add_parser(
        "format",
        help="Re-render a stored transcript.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    format_parser.add_argument("--transcript", type=Path, required=True)
    format_parser.add_argument(
        "--format", dest="fmt", choices=["markdown", "text"], default="markdown"
    )
    format_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file (default: write to stdout).",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the local web UI for sending the bot to meetings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    serve_parser.add_argument(
        "--port", type=int, default=8080, help="Port to listen on."
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Address to bind. Loopback by default and you should leave it "
            "there: the service has no authentication and can join meetings "
            "and read their transcripts."
        ),
    )
    serve_parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="Open the UI in your browser once the server is up.",
    )
    _add_llm_flags(serve_parser)

    check_parser = subparsers.add_parser(
        "check",
        help="Validate configuration, credentials and the browser; join nothing.",
    )
    check_parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the live Deepgram credential check (no network calls).",
    )

    return parser


def _add_llm_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-provider",
        dest="llm_provider",
        choices=["anthropic", "openai"],
        default=None,
        help="Which LLM provider generates the analysis.",
    )
    parser.add_argument(
        "--llm-model",
        dest="llm_model",
        default=None,
        help="Model id override for the chosen provider.",
    )


def _config_from_args(args: argparse.Namespace) -> Config:
    """Load config from the environment, then apply CLI overrides."""
    config = load_config(env_file=args.env_file)
    overrides = {
        field: getattr(args, field, None)
        for field in (
            "meet_url",
            "bot_name",
            "output_dir",
            "headless",
            "admission_timeout_s",
            "max_meeting_duration_s",
            "per_participant_audio",
            "live_captions_overlay",
            "speaker_names_from_captions",
            "anonymise_analysis",
            "analysis_enabled",
            "llm_provider",
            "llm_model",
            "log_level",
        )
    }
    # Switching provider on the command line must not carry over a model id
    # that belongs to the other provider. An empty string here is meaningful:
    # Config.__post_init__ reads it as "use the new provider's default".
    new_provider = overrides.get("llm_provider")
    if (
        new_provider
        and new_provider != config.llm_provider
        and not overrides.get("llm_model")
    ):
        logger.info(
            "Provider overridden to %s; using its default model instead of %r",
            new_provider,
            config.llm_model,
        )
        overrides["llm_model"] = ""

    return config.override(**overrides)


def cmd_run(args: argparse.Namespace) -> int:
    """Handle ``run``."""
    config = _config_from_args(args)
    try:
        config.validate(require_meeting=True)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    _print_disclosure_notice(config)

    try:
        run = asyncio.run(run_meeting(config))
    except KeyboardInterrupt:
        logger.warning("Interrupted before the run completed")
        return EXIT_INTERRUPTED

    _report(run)
    return EXIT_OK if run.succeeded else EXIT_RUN_FAILED


def _print_disclosure_notice(config: Config) -> None:
    logger.info(
        "Joining %s as %r. Participants will see this name, and Meet shows its "
        "own on-screen indicator when transcription is active - do not "
        "represent this bot as anything other than a recorder.",
        config.meet_url,
        config.bot_name,
    )


def _report(run: MeetingRun) -> None:
    """Log a summary of what the run produced."""
    logger.info("--- Run complete ---")
    logger.info("Output directory : %s", run.output_dir)
    logger.info("Transcript       : %s", run.transcript_jsonl)
    if run.transcript_markdown:
        logger.info("Readable         : %s", run.transcript_markdown)
    if run.analysis_markdown:
        logger.info("Analysis         : %s", run.analysis_markdown)
    logger.info("Utterances       : %d", run.utterance_count)
    logger.info("Wall clock       : %s", format_timestamp(run.duration_s))
    if run.end_reason is not None:
        logger.info("Ended because    : %s", run.end_reason.value)
    for error in run.errors:
        logger.warning("Issue            : %s", error)


def cmd_analyze(args: argparse.Namespace) -> int:
    """Handle ``analyze``."""
    config = _config_from_args(args)
    try:
        config.validate(require_meeting=False)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    transcript_path: Path = args.transcript
    try:
        utterances = read_utterances(transcript_path)
    except (FileNotFoundError, OSError) as exc:
        logger.error("Could not read %s: %s", transcript_path, exc)
        return EXIT_CONFIG_ERROR

    if not utterances:
        logger.error("%s contains no utterances; nothing to analyse", transcript_path)
        return EXIT_RUN_FAILED

    meta = read_meta(transcript_path) or {}
    captured = max((u.end for u in utterances), default=0.0)

    try:
        client = build_client(
            config.llm_provider,
            config.llm_api_key,
            config.llm_model,
            config.llm_base_url,
        )
        analysis = analyze_transcript(
            utterances,
            client,
            provider=config.llm_provider,
            meeting_url=str(meta.get("meet_url", "")),
            duration=format_timestamp(captured),
        )
    except (AnalysisError, LLMError) as exc:
        logger.error("Analysis failed: %s", exc)
        return EXIT_RUN_FAILED

    output = args.output or transcript_path.parent / "analysis.md"
    try:
        output.write_text(
            render_analysis_markdown(
                analysis,
                meta={
                    "source_transcript": transcript_path.name,
                    "utterances": len(utterances),
                    "generated_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error("Could not write %s: %s", output, exc)
        return EXIT_RUN_FAILED

    logger.info("Wrote %s", output)
    return EXIT_OK


def cmd_format(args: argparse.Namespace) -> int:
    """Handle ``format``."""
    try:
        utterances = read_utterances(args.transcript)
    except (FileNotFoundError, OSError) as exc:
        logger.error("Could not read %s: %s", args.transcript, exc)
        return EXIT_CONFIG_ERROR

    meta = read_meta(args.transcript) or {}
    if args.fmt == "markdown":
        rendered = render_markdown(
            utterances,
            meta={
                "meeting_url": meta.get("meet_url", "unknown"),
                "recorded_by": meta.get("bot_name", "unknown"),
                "started_at": meta.get("started_at", "unknown"),
            },
        )
    else:
        rendered = render_text(utterances)

    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            logger.error("Could not write %s: %s", args.output, exc)
            return EXIT_RUN_FAILED
        logger.info("Wrote %s", args.output)
    else:
        sys.stdout.write(rendered if rendered.endswith("\n") else rendered + "\n")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """Handle ``serve``: run the local web UI until interrupted."""
    try:
        import uvicorn
    except ImportError:
        logger.error(
            "The web UI needs uvicorn and fastapi: pip install fastapi uvicorn"
        )
        return EXIT_CONFIG_ERROR

    from meetbot.service.app import create_app

    config = _config_from_args(args)
    try:
        # No meeting URL yet - the whole point is that one arrives later, per
        # request - so only the settings a server needs are checked here.
        config.validate(require_meeting=False)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "Binding to %s exposes an unauthenticated service that can join "
            "meetings and read transcripts. Use 127.0.0.1 unless you have "
            "put your own authentication in front of it.",
            args.host,
        )

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
    logger.info("meetbot is at %s", url)
    logger.info("Recordings: %s", config.output_dir)

    if getattr(args, "open_browser", False):
        import threading
        import webbrowser

        # Deferred: opening the browser before uvicorn is listening shows an
        # error page the user then has to reload.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        create_app(config),
        host=args.host,
        port=args.port,
        log_level=(args.log_level or "info").lower(),
        # uvicorn otherwise replaces the handlers configure_logging installed.
        log_config=None,
    )
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """Handle ``check``: validate config and confirm Playwright is usable."""
    config = _config_from_args(args)
    ok = True

    try:
        config.validate(require_meeting=bool(config.meet_url))
        logger.info("Configuration looks valid")
    except ConfigError as exc:
        logger.error("%s", exc)
        ok = False

    logger.info("Bot display name : %r", config.bot_name)
    logger.info("Output directory : %s", config.output_dir)
    logger.info("Deepgram model   : %s", config.deepgram_model)
    logger.info("LLM              : %s / %s", config.llm_provider, config.llm_model)
    logger.info("Audio bridge     : %s", config.bridge_url)

    # The Deepgram key is the one credential a run cannot start without, so
    # check it here rather than leaving it to fail on a live call.
    if not config.deepgram_api_key:
        logger.error("DEEPGRAM_API_KEY is not set - transcription cannot start")
        ok = False
    elif args.offline:
        logger.info("Deepgram         : skipped (--offline)")
    else:
        try:
            detail = asyncio.run(
                probe_credentials(
                    config.deepgram_api_key,
                    model=config.deepgram_model,
                    language=config.deepgram_language,
                    sample_rate=config.sample_rate,
                )
            )
            logger.info("Deepgram OK (%s)", detail)
        except DeepgramError as exc:
            logger.error("Deepgram check failed: %s", exc)
            ok = False

    # The summary happens after the meeting ends, so an unusable LLM key or a
    # retired model id would otherwise only surface once the call is over and
    # the material is gone. Check it up front like the Deepgram key.
    if not config.analysis_enabled:
        logger.info("LLM              : skipped (analysis disabled)")
    elif not config.llm_api_key:
        logger.error(
            "No API key for LLM_PROVIDER=%s - the meeting summary cannot be "
            "generated",
            config.llm_provider,
        )
        ok = False
    elif args.offline:
        logger.info("LLM              : skipped (--offline)")
    else:
        try:
            detail = probe_llm(
                build_client(
                    config.llm_provider,
                    config.llm_api_key,
                    config.llm_model,
                    config.llm_base_url,
                )
            )
            logger.info("LLM OK (%s)", detail)
        except LLMError as exc:
            logger.error("LLM check failed: %s", exc)
            ok = False

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            version = browser.version
            browser.close()
        logger.info("Playwright Chromium OK (%s)", version)
    except ImportError:
        logger.error("Playwright is not installed: pip install playwright")
        ok = False
    except Exception as exc:
        logger.error(
            "Could not launch Chromium (%s: %s). Run: playwright install chromium",
            type(exc).__name__,
            exc,
        )
        ok = False

    return EXIT_OK if ok else EXIT_CONFIG_ERROR


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.log_level or "INFO")

    handlers = {
        "run": cmd_run,
        "analyze": cmd_analyze,
        "format": cmd_format,
        "check": cmd_check,
        "serve": cmd_serve,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces the choices
        parser.error(f"Unknown command {args.command!r}")
        return EXIT_CONFIG_ERROR

    try:
        return handler(args)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
