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
import contextlib
import logging
import sys
from typing import Any
import time

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

    login_parser = subparsers.add_parser(
        "login",
        help="Sign the bot's browser into a Google account (one time).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    login_parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Where to keep the signed-in profile (default: CHROME_PROFILE_DIR).",
    )
    login_parser.add_argument(
        "--channel",
        default=None,
        help="Browser to sign in through (chrome, msedge). Default: whichever "
             "is installed, since Google often refuses bundled Chromium.",
    )
    login_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for you to finish signing in.",
    )

    users_parser = subparsers.add_parser(
        "users",
        help="Manage web-UI accounts (add, list, passwd, delete).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    users_parser.add_argument(
        "action", choices=("list", "add", "passwd", "delete")
    )
    users_parser.add_argument(
        "username", nargs="?", help="Account to act on (not needed for 'list')."
    )
    users_parser.add_argument(
        "--role",
        choices=("admin", "member"),
        default="member",
        help="Role for a new account.",
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


def cmd_login(args: argparse.Namespace) -> int:
    """Handle ``login``: open a real browser so a human can sign in once.

    Deliberately manual. Google actively blocks scripted logins, and storing
    a password to type in would be both fragile and a much worse thing to
    keep on disk than a session cookie. So the browser is opened visibly, the
    person signs in themselves, and the resulting session is what persists.
    """
    config = _config_from_args(args)
    profile = args.profile_dir or config.chrome_profile_dir
    if profile is None:
        profile = Path.home() / ".meetbot" / "chrome-profile"
        logger.info("No CHROME_PROFILE_DIR set; using %s", profile)
    profile = Path(profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)

    return asyncio.run(
        _login(profile, args.timeout, getattr(args, "channel", None))
    )


async def _first_available_channel(playwright: Any) -> str | None:
    """The first real browser Playwright can drive here, if any.

    Decided by launching one rather than guessing at install paths, which
    differ by platform, architecture and whether the install is per-user.
    Costs about a second, once, during an interactive command.
    """
    for channel in ("chrome", "msedge"):
        try:
            browser = await playwright.chromium.launch(
                channel=channel, headless=True
            )
        except Exception:  # noqa: BLE001 - absence is the expected outcome
            continue
        await browser.close()
        return channel
    return None


async def _login(
    profile: Path, timeout_s: int, channel_override: str | None = None
) -> int:
    """Drive the interactive sign-in and report whether it stuck."""
    from playwright.async_api import async_playwright

    from meetbot.join import _CHROMIUM_ARGS

    logger.info("Opening a browser window. Sign in to the Google account the")
    logger.info("bot should join meetings as, then leave the window alone.")
    logger.info("Profile: %s", profile)

    async with async_playwright() as playwright:
        launch: dict[str, Any] = {
            "headless": False,  # the entire point: a human has to drive this
            "args": list(_CHROMIUM_ARGS),
            "viewport": {"width": 1100, "height": 800},
        }
        # Sign in through real Chrome when it is installed. Google refuses
        # sign-in on browsers it considers insecure, and Playwright's bundled
        # Chromium is regularly one of them - the window opens, the password
        # is accepted, and the session never lands. Meetings can still run on
        # bundled Chromium afterwards; only this step is fussy.
        channel = channel_override or await _first_available_channel(playwright)
        if channel:
            launch["channel"] = channel
            logger.info("Signing in through %s.", channel)
        else:
            logger.warning(
                "No installed Chrome or Edge found; falling back to bundled "
                "Chromium. Google may refuse to sign in to it."
            )
        context = await playwright.chromium.launch_persistent_context(
            str(profile), **launch
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://accounts.google.com/", wait_until="domcontentloaded")

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if not context.pages:
                    logger.warning("Browser closed before sign-in completed")
                    return EXIT_CONFIG_ERROR
                # Google sends a signed-in visitor on to myaccount.google.com;
                # that redirect is the signal, rather than scraping the page.
                if "myaccount.google.com" in page.url:
                    logger.info("Signed in. Verifying against Meet...")
                    await page.goto(
                        "https://meet.google.com/", wait_until="domcontentloaded"
                    )
                    await page.wait_for_timeout(2500)
                    body = await page.evaluate("() => document.body.innerText")
                    if "Sign in" in body and "New meeting" not in body:
                        logger.warning(
                            "Signed in to Google, but Meet still shows a sign-in "
                            "prompt. The session may not cover Meet."
                        )
                        return EXIT_CONFIG_ERROR
                    logger.info("Meet recognises the session.")
                    logger.info("")
                    logger.info("Add this to your .env so runs use it:")
                    logger.info("    CHROME_PROFILE_DIR=%s", profile)
                    logger.info("")
                    logger.info(
                        "Treat that directory like a password - it holds live "
                        "Google session cookies."
                    )
                    return EXIT_OK
                await asyncio.sleep(2.0)

            logger.error("Timed out after %ds waiting for sign-in", timeout_s)
            return EXIT_CONFIG_ERROR
        finally:
            with contextlib.suppress(Exception):
                await context.close()


def cmd_serve(args: argparse.Namespace) -> int:
    """Handle ``serve``: run the local web UI until interrupted."""
    try:
        import uvicorn
    except ImportError:
        logger.error(
            "The web UI needs: pip install fastapi uvicorn python-multipart"
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
            "Binding to %s serves meetbot to the network. Accounts are "
            "required, but sessions travel as cookies: put TLS in front of "
            "it, or passwords and session tokens cross the wire in clear.",
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
    """Handle ``check``: confirm everything a real run depends on.

    Shares its checks with the service and the web UI - see
    :mod:`meetbot.preflight` - so "check says it is fine" and "the bot will
    actually work" cannot drift apart.
    """
    from meetbot.preflight import run_preflight

    config = _config_from_args(args)

    logger.info("Bot display name : %r", config.bot_name)
    logger.info("Output directory : %s", config.output_dir)
    logger.info("Deepgram model   : %s / %s", config.deepgram_model, config.deepgram_language)
    logger.info("LLM              : %s / %s", config.llm_provider, config.llm_model)
    logger.info("Audio bridge     : %s", config.bridge_url)
    logger.info("")

    report = asyncio.run(
        run_preflight(
            config,
            include_session=not args.offline,
            offline=args.offline,
        )
    )
    report.log()

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            logger.info("  OK    %-16s %s", "Playwright", browser.version)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("  FAIL  %-16s %s", "Playwright", exc)
        logger.warning("To fix Playwright: playwright install chromium")
        return EXIT_CONFIG_ERROR

    logger.info("")
    if report.can_record:
        logger.info("Ready to record.")
        return EXIT_OK
    logger.error("Not ready - fix the items above, then run check again.")
    return EXIT_CONFIG_ERROR


def cmd_serve(args: argparse.Namespace) -> int:
    """Handle ``serve``: run the local web UI until interrupted."""
    try:
        import uvicorn
    except ImportError:
        logger.error(
            "The web UI needs: pip install fastapi uvicorn python-multipart"
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
            "Binding to %s serves meetbot to the network. Accounts are "
            "required, but sessions travel as cookies: put TLS in front of "
            "it, or passwords and session tokens cross the wire in clear.",
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


def cmd_users(args: argparse.Namespace) -> int:
    """Handle ``users``: account management from the terminal.

    The admin panel in the web UI covers day-to-day work. This exists for the
    case the UI cannot help with: nobody can sign in, because the only admin
    password was lost. Without it a forgotten password would mean deleting
    the account database and starting over.
    """
    import getpass

    from meetbot.service.auth import AuthError, Role, UserStore

    config = _config_from_args(args)
    store = UserStore(Path(config.service_state_dir).expanduser() / "users.json")

    if args.action == "list":
        if not len(store):
            logger.info("No accounts yet. 'meetbot serve' creates a first admin.")
        for user in store.list():
            logger.info("%-20s %s", user.username, user.role.value)
        return EXIT_OK

    if not args.username:
        logger.error("%s needs a username", args.action)
        return EXIT_CONFIG_ERROR

    try:
        if args.action == "delete":
            store.delete(args.username)
            logger.info("Deleted %s", args.username)
            return EXIT_OK

        # Prompted rather than passed as an argument: a password on the
        # command line lands in shell history and in the process list.
        password = getpass.getpass(f"Password for {args.username}: ")
        if password != getpass.getpass("Repeat: "):
            logger.error("Passwords did not match")
            return EXIT_CONFIG_ERROR

        if args.action == "add":
            store.add(args.username, password, Role(args.role))
            logger.info("Created %s (%s)", args.username, args.role)
        else:
            store.set_password(args.username, password)
            logger.info("Password updated for %s", args.username)
    except AuthError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR
    return EXIT_OK


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
        "login": cmd_login,
        "users": cmd_users,
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
