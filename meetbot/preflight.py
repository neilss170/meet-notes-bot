"""Everything that must be true before the bot can record a meeting.

Written after an afternoon in which every failure was discovered *during* a
live call: a Google session that had quietly expired, so Meet refused the bot
as an anonymous guest; a Deepgram option combination the server rejects, so
audio flowed for two minutes and produced nothing; a summary step that could
not have run because the key was never exercised.

None of that needed a meeting to discover. Each is a question that can be
asked in a few seconds beforehand, and the cost of not asking is a wasted
meeting with people waiting in it.

So the checks live here rather than in the CLI, and everything uses them: the
``check`` command, the service at startup, and the UI, which refuses to send
the bot while something is broken. Each check reports what to *do* about a
failure, not just that it failed - "the profile is signed out" is only useful
next to "run python -m meetbot login".

Every check probes with the settings a real run would use, not defaults. A
probe that exercises a different configuration from the meeting proves
nothing: the Deepgram failure above lived entirely in options the old check
did not send.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from meetbot.config import Config

logger = logging.getLogger(__name__)

#: How long to let the Google session check run. It starts a browser, so it
#: is the slowest check by far; everything else answers in a second or two.
SESSION_CHECK_TIMEOUT_S: Final[float] = 45.0

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


@dataclass(frozen=True)
class CheckResult:
    """One question asked and answered.

    Attributes:
        name: Short label, e.g. ``"Google sign-in"``.
        ok: Whether the thing works.
        detail: What was found, phrased for a human.
        fix: The command or action that resolves a failure. Empty when the
            check passed, or when there is nothing useful to suggest.
        blocking: Whether a meeting is pointless without this. A failed
            non-blocking check degrades the result rather than preventing it -
            no summary, or generic speaker labels.
        mode: Which way of recording this blocks. ``"both"`` for things every
            path needs; ``"bot"`` for the browser and the Google session, which
            local recording never touches; ``"local"`` for the audio devices,
            which the bot never touches. Without this a stale Google session
            reported the whole tool as unusable, when the default way to
            record does not involve Google at all.
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""
    blocking: bool = True
    mode: str = "both"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            "blocking": self.blocking,
            "mode": self.mode,
        }


@dataclass
class Preflight:
    """The full set of answers, and what they mean together."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether everything passed, including the non-blocking checks."""
        return all(result.ok for result in self.results)

    def _blocking_for(self, mode: str) -> list[CheckResult]:
        return [
            r for r in self.results if r.blocking and r.mode in ("both", mode)
        ]

    @property
    def can_record_locally(self) -> bool:
        """Whether this machine can be recorded with nothing joining the call."""
        return all(r.ok for r in self._blocking_for("local"))

    @property
    def can_send_bot(self) -> bool:
        """Whether the bot could join a meeting."""
        return all(r.ok for r in self._blocking_for("bot"))

    @property
    def can_record(self) -> bool:
        """Whether *any* way of recording would work."""
        return self.can_record_locally or self.can_send_bot

    @property
    def blockers(self) -> list[CheckResult]:
        """Failures that stop every way of recording.

        A check that only blocks one mode is not a blocker while the other
        mode still works - it is reported as a warning instead, so an expired
        Google session no longer reads as "this tool is broken".
        """
        if self.can_record:
            return [r for r in self.results if r.blocking and not r.ok
                    and r.mode == "both"]
        return [r for r in self.results if r.blocking and not r.ok]

    @property
    def warnings(self) -> list[CheckResult]:
        failed = [r for r in self.results if not r.ok]
        blockers = {id(r) for r in self.blockers}
        return [r for r in failed if id(r) not in blockers]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "can_record": self.can_record,
            "can_record_locally": self.can_record_locally,
            "can_send_bot": self.can_send_bot,
            "checks": [result.to_dict() for result in self.results],
        }

    def log(self) -> None:
        """Write the outcome out in the order a reader needs it."""
        blockers = {id(r) for r in self.blockers}
        for result in self.results:
            if result.ok:
                logger.info("  OK    %-16s %s", result.name, result.detail)
            elif id(result) in blockers:
                logger.error("  FAIL  %-16s %s", result.name, result.detail)
            else:
                logger.warning("  WARN  %-16s %s", result.name, result.detail)
        for result in self.blockers + self.warnings:
            if result.fix:
                logger.warning("To fix %s: %s", result.name, result.fix)


# -- individual checks -----------------------------------------------------


async def check_google_session(config: Config) -> CheckResult:
    """Whether the bot's browser profile is still signed in to Google.

    The check that matters most and was missing entirely. Google expires
    these sessions on its own schedule, and an expired one is invisible until
    Meet replaces the pre-join screen with "You can't join this video call" -
    by which point people are waiting in a meeting.

    Costs a few seconds because it starts a browser; there is no cheaper way
    to distinguish a live session from cookies that merely exist.
    """
    name = "Google sign-in"
    fix = "python -m meetbot login"

    if config.chrome_profile_dir is None:
        return CheckResult(
            name,
            ok=False,
            detail=(
                "No CHROME_PROFILE_DIR set, so the bot joins as an anonymous "
                "guest - which Meet increasingly refuses outright"
            ),
            fix=fix,
        )

    master = Path(config.chrome_profile_dir).expanduser()
    if not master.is_dir():
        return CheckResult(
            name, ok=False, detail=f"No profile at {master}", fix=fix
        )

    try:
        from playwright.async_api import async_playwright

        from meetbot.profile import clone_profile, discard_profile
    except ImportError as exc:
        return CheckResult(
            name,
            ok=False,
            detail=f"Playwright is not available ({exc})",
            fix="pip install playwright && playwright install chromium",
        )

    clone = None
    try:
        # A copy, so the check cannot disturb a profile a meeting is using,
        # and so it works while one is running.
        clone = await asyncio.to_thread(clone_profile, master)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(clone), headless=True
            )
            try:
                page = await context.new_page()
                await page.goto(
                    "https://myaccount.google.com/", wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(4000)
                body = await page.evaluate("() => document.body.innerText")
            finally:
                await context.close()
    except Exception as exc:  # noqa: BLE001 - any failure here means "unknown"
        return CheckResult(
            name, ok=False, detail=f"Could not check ({type(exc).__name__}: {exc})",
            fix=fix,
        )
    finally:
        if clone is not None:
            from meetbot.profile import discard_profile

            await asyncio.to_thread(discard_profile, clone)

    emails = _EMAIL_RE.findall(body or "")
    if not emails:
        return CheckResult(
            name,
            ok=False,
            detail="The profile is signed out; Meet will refuse the bot as a guest",
            fix=fix,
        )
    return CheckResult(name, ok=True, detail=f"signed in as {emails[0]}")


async def check_deepgram(config: Config) -> CheckResult:
    """Whether Deepgram accepts the exact stream a run would open.

    Deliberately built from this config rather than from defaults. The
    failure that took transcription out entirely - utterance_end_ms without
    interim results - lived in options a generic probe never sent, so it
    passed a check and then rejected every real stream.
    """
    name = "Deepgram"
    if not config.deepgram_api_key:
        return CheckResult(
            name, ok=False, detail="DEEPGRAM_API_KEY is not set",
            fix="Add DEEPGRAM_API_KEY to .env",
        )

    import websockets

    from meetbot.capture.deepgram import build_stream_url

    url = build_stream_url(
        model=config.deepgram_model,
        language=config.deepgram_language,
        sample_rate=config.sample_rate,
        diarize=not config.per_participant_audio,
        endpointing_ms=config.deepgram_endpointing_ms,
        utterance_end_ms=config.deepgram_utterance_end_ms,
        keyterms=config.deepgram_keyterms,
    )
    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Token {config.deepgram_api_key}"}
        ):
            pass
    except Exception as exc:  # noqa: BLE001 - the server's verdict is what matters
        reason = str(exc).splitlines()[0]
        return CheckResult(
            name,
            ok=False,
            detail=f"Rejected the stream options ({reason})",
            fix=(
                "Check DEEPGRAM_MODEL, DEEPGRAM_LANGUAGE and DEEPGRAM_KEYTERMS "
                "in .env - and that the account still has credit"
            ),
        )
    detail = f"{config.deepgram_model}/{config.deepgram_language}"
    if config.deepgram_keyterms:
        detail += f", {len(config.deepgram_keyterms)} key term(s)"
    return CheckResult(name, ok=True, detail=f"accepts {detail}")


def check_llm(config: Config) -> CheckResult:
    """Whether the summary step will work once the meeting is over.

    Non-blocking: a meeting with no summary still leaves a transcript, and
    the analysis can be re-run offline afterwards.
    """
    name = "AI notes"
    if not config.analysis_enabled:
        return CheckResult(name, ok=True, detail="disabled", blocking=False)
    if not config.llm_api_key:
        key = "ANTHROPIC_API_KEY" if config.llm_provider == "anthropic" else "OPENAI_API_KEY"
        return CheckResult(
            name, ok=False, detail=f"{key} is not set", blocking=False,
            fix=f"Add {key} to .env, or set ANALYSIS_ENABLED=false",
        )
    try:
        from meetbot.analysis.llm import build_client, probe_llm

        detail = probe_llm(
            build_client(
                config.llm_provider,
                config.llm_api_key,
                config.llm_model,
                config.llm_base_url,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, ok=False, detail=f"{type(exc).__name__}: {exc}", blocking=False,
            fix="Check the key and LLM_MODEL in .env",
        )
    return CheckResult(name, ok=True, detail=detail, blocking=False)


def check_config(config: Config) -> CheckResult:
    """Whether the configuration is internally coherent."""
    from meetbot.config import ConfigError

    try:
        config.validate(require_meeting=False)
    except ConfigError as exc:
        return CheckResult(
            "Configuration", ok=False, detail=str(exc).replace("\n", " "),
            fix="Fix the values in .env",
        )
    return CheckResult("Configuration", ok=True, detail="valid")


# -- the whole set ---------------------------------------------------------


def check_local_capture(config: Config) -> CheckResult:
    """Whether this machine can be recorded without a bot.

    Two separate things can be wrong, and they fail differently. Having no
    loopback device at all is a hard stop. Having one that is producing
    silence is not - nothing may be playing right now - but it is the single
    most common reason a recording comes back empty, so it is worth saying
    out loud rather than discovering afterwards.
    """
    try:
        from meetbot.capture.local import capture_available, probe_loopback
    except Exception as exc:  # noqa: BLE001 - a missing optional dependency
        return CheckResult(
            "Local capture",
            ok=False,
            detail=f"unavailable: {exc}",
            fix="pip install PyAudioWPatch numpy",
            mode="local",
        )

    ready, detail = capture_available()
    if not ready:
        return CheckResult(
            "Local capture",
            ok=False,
            detail=detail,
            fix="Check Windows Sound settings for an active playback device",
            mode="local",
        )

    try:
        probe = probe_loopback(seconds=0.4)
    except Exception as exc:  # noqa: BLE001 - probing must never be fatal
        return CheckResult(
            "Local capture", ok=True, detail=f"{detail} (not probed: {exc})",
            mode="local",
        )

    if probe.silent:
        # Not blocking: silence is correct when nothing is playing. It is
        # reported because muted output produces a well-formed empty
        # transcript, which looks like a bug in this tool rather than a
        # volume slider.
        return CheckResult(
            "Local capture",
            ok=False,
            detail=f"{detail} - but the speakers are silent right now",
            fix="If a meeting is audible, check the output is not muted",
            blocking=False,
            mode="local",
        )
    return CheckResult(
        "Local capture", ok=True, detail=f"{detail}; audio detected", mode="local"
    )


async def run_preflight(
    config: Config, *, include_session: bool = True, offline: bool = False
) -> Preflight:
    """Ask every question, and report the answers together.

    Args:
        config: The settings a run would use.
        include_session: Whether to check the Google sign-in. It starts a
            browser, so callers that need a fast answer can skip it.
        offline: Skip everything that needs the network.
    """
    results = [check_config(config)]
    results.append(await asyncio.to_thread(check_local_capture, config))

    if offline:
        results.append(
            CheckResult("Deepgram", ok=True, detail="skipped (offline)")
        )
        results.append(
            CheckResult("AI notes", ok=True, detail="skipped (offline)", blocking=False)
        )
    else:
        results.append(await check_deepgram(config))
        results.append(await asyncio.to_thread(check_llm, config))

    if include_session and not offline:
        try:
            session = await asyncio.wait_for(
                check_google_session(config), timeout=SESSION_CHECK_TIMEOUT_S
            )
            # Forced here rather than on each of the six results inside the
            # check: one place cannot be half-done, and every way that check
            # can fail is a bot-mode problem by definition.
            results.append(dataclasses.replace(session, mode="bot"))
        except asyncio.TimeoutError:
            results.append(
                CheckResult(
                    "Google sign-in",
                    ok=False,
                    detail=f"Check timed out after {SESSION_CHECK_TIMEOUT_S:.0f}s",
                    fix="python -m meetbot login",
                    mode="bot",
                )
            )

    return Preflight(results)


__all__ = [
    "SESSION_CHECK_TIMEOUT_S",
    "CheckResult",
    "Preflight",
    "check_config",
    "check_deepgram",
    "check_google_session",
    "check_llm",
    "check_local_capture",
    "run_preflight",
]
