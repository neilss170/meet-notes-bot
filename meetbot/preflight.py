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
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            "blocking": self.blocking,
        }


@dataclass
class Preflight:
    """The full set of answers, and what they mean together."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether everything passed, including the non-blocking checks."""
        return all(result.ok for result in self.results)

    @property
    def can_record(self) -> bool:
        """Whether a meeting is worth starting at all."""
        return all(result.ok for result in self.results if result.blocking)

    @property
    def blockers(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocking and not r.ok]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.blocking and not r.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "can_record": self.can_record,
            "checks": [result.to_dict() for result in self.results],
        }

    def log(self) -> None:
        """Write the outcome out in the order a reader needs it."""
        for result in self.results:
            if result.ok:
                logger.info("  OK    %-16s %s", result.name, result.detail)
            elif result.blocking:
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
            results.append(
                await asyncio.wait_for(
                    check_google_session(config), timeout=SESSION_CHECK_TIMEOUT_S
                )
            )
        except asyncio.TimeoutError:
            results.append(
                CheckResult(
                    "Google sign-in",
                    ok=False,
                    detail=f"Check timed out after {SESSION_CHECK_TIMEOUT_S:.0f}s",
                    fix="python -m meetbot login",
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
    "run_preflight",
]
