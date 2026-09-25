"""Configuration loading and validation for meetbot.

Configuration comes from (in increasing order of precedence):

1. Built-in defaults in this module.
2. Environment variables, optionally seeded from a ``.env`` file.
3. Explicit CLI flags, applied by :mod:`meetbot.cli` via :meth:`Config.override`.

Secrets are *only* ever read from the environment - never hardcoded and never
written back to disk by this package.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal, Mapping

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

LLMProvider = Literal["anthropic", "openai"]

#: Default display name. It deliberately announces the bot and that it is
#: recording - see ``README.md`` -> "Consent and disclosure".
DEFAULT_BOT_NAME: Final[str] = "Scribe (Recording)"

DEFAULT_ANTHROPIC_MODEL: Final[str] = "claude-opus-5"
DEFAULT_OPENAI_MODEL: Final[str] = "gpt-4o"
DEFAULT_DEEPGRAM_MODEL: Final[str] = "nova-3"

#: Substrings that make it obvious to other participants that this account is
#: a recording bot. If a configured display name contains none of them we
#: append a disclosure suffix rather than silently joining under an opaque name.
_DISCLOSURE_TOKENS: Final[tuple[str, ...]] = (
    "record",
    "transcri",
    "notetaker",
    "note taker",
    "notes bot",
)
_DISCLOSURE_SUFFIX: Final[str] = " (Recording)"

#: Google Meet caps the guest display name; keep well inside it.
MAX_DISPLAY_NAME_LEN: Final[int] = 60

_MEET_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https://meet\.google\.com/"
    r"(?:[a-z]{3}-[a-z]{4}-[a-z]{3}|lookup/[A-Za-z0-9_-]+)"
    r"(?:[?#].*)?$"
)


class ConfigError(ValueError):
    """Raised when configuration is missing or internally inconsistent."""


def ensure_disclosure(name: str) -> str:
    """Return ``name`` guaranteed to disclose that the participant records.

    The display name is the only in-call signal other participants get about
    this bot before it starts transcribing, so disclosure is enforced here
    rather than left to the caller.
    """
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        cleaned = DEFAULT_BOT_NAME
    lowered = cleaned.lower()
    if not any(token in lowered for token in _DISCLOSURE_TOKENS):
        logger.warning(
            "Display name %r does not disclose recording; appending %r. "
            "Participants must be able to tell this account is a recorder.",
            cleaned,
            _DISCLOSURE_SUFFIX.strip(),
        )
        cleaned = f"{cleaned}{_DISCLOSURE_SUFFIX}"
    return cleaned[:MAX_DISPLAY_NAME_LEN].strip()


def validate_meet_url(url: str) -> str:
    """Validate and normalise a Google Meet URL.

    Accepts the ``https://meet.google.com/abc-defg-hij`` and
    ``https://meet.google.com/lookup/<nickname>`` forms. Raises
    :class:`ConfigError` for anything else so we fail before launching a
    browser rather than staring at a 404 page for five minutes.
    """
    candidate = url.strip()
    if candidate.startswith("meet.google.com/"):
        candidate = f"https://{candidate}"
    if not _MEET_URL_RE.match(candidate):
        raise ConfigError(
            f"{url!r} is not a recognised Google Meet URL. "
            "Expected https://meet.google.com/xxx-yyyy-zzz"
        )
    return candidate


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_str(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    return raw.strip() if raw and raw.strip() else default


@dataclass(frozen=True)
class Config:
    """Fully-resolved runtime configuration.

    Instances are immutable; use :meth:`override` to derive a variant with CLI
    flags applied.
    """

    # --- Meeting ---------------------------------------------------------
    meet_url: str = ""
    bot_name: str = DEFAULT_BOT_NAME
    output_dir: Path = Path("recordings")

    # --- Browser / join behaviour ----------------------------------------
    headless: bool = True
    admission_timeout_s: int = 300
    max_meeting_duration_s: int = 4 * 60 * 60
    #: Leave once the bot is the only participant left for this many seconds.
    alone_timeout_s: int = 120
    #: Playwright browser channel, e.g. "chrome". None uses bundled Chromium.
    browser_channel: str | None = None

    # --- Audio capture ----------------------------------------------------
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8765
    sample_rate: int = 16_000
    #: When true, each remote participant gets an independent Deepgram stream
    #: so speaker labels are real names instead of "Speaker 0". Costs one
    #: Deepgram connection per participant.
    per_participant_audio: bool = False
    #: When true, share a live-caption canvas as a screen-share (via
    #: "Present now") and update it with each finalised utterance, visible
    #: to every participant including the host - without turning the bot's
    #: own camera on. Off by default: unlike everything else in this
    #: package, this changes what OTHER people in the call see, not just
    #: what the operator records - it deserves an explicit, deliberate
    #: opt-in. There is no host-only variant: a screen-share is seen by
    #: everyone in the call, so if the transcript should stay private, leave
    #: this off and read it in the web UI instead.
    #: The one Meet-DOM-dependent step (clicking "Present now") is the
    #: least-tested selector in the project; see the note on
    #: PRESENT_NOW_BUTTON in selectors.py.
    live_captions_overlay: bool = False
    #: Turn Meet's own live captions on (for this browser only) and use the
    #: speaker name Meet attaches to each caption entry to label utterances,
    #: instead of Deepgram's generic "Speaker 0" / "Speaker 1".
    #:
    #: This is the only way to get real names: nothing in the page links an
    #: audio track to a participant, but Meet does the attribution
    #: server-side and renders the result. Only the name and the timing are
    #: used - the caption text itself is discarded, because Deepgram's
    #: transcription is more accurate and Meet rewrites captions in place.
    #:
    #: Enabling captions is a per-viewer setting: it does not turn on
    #: captions, transcription or recording for anyone else in the call.
    #: Degrades silently to generic labels if captions are unavailable.
    speaker_names_from_captions: bool = True
    #: Replace real participant names with stable pseudonyms ("Speaker A")
    #: in the transcript text sent to the LLM. The local transcript keeps the
    #: real names; only the third-party request is anonymised. Off by
    #: default - the names are usually the point of the summary - but a
    #: one-flag control for meetings where personal names should not leave
    #: the machine.
    anonymise_analysis: bool = False
    #: Directory holding a persistent Chromium profile. When set, the browser
    #: reuses it instead of starting from a blank slate, so a Google account
    #: signed in once with ``python -m meetbot login`` stays signed in.
    #:
    #: This is not a nicety. Meet increasingly refuses anonymous guests
    #: outright - the pre-join screen is replaced by "You can't join this
    #: video call" and the host is never even asked - which no selector can
    #: work around. A signed-in profile is the only fix, and it also makes
    #: the bot appear as a named participant rather than an anonymous guest.
    #:
    #: The directory holds live Google session cookies. Treat it like a
    #: password: keep it out of version control and off shared machines.
    chrome_profile_dir: Path | None = None

    #: Where the web service keeps its account database and session-signing
    #: key. Outside the repository by default: it holds password hashes and
    #: a key that mints valid sessions.
    service_state_dir: Path = Path("~/.meetbot")

    #: Run the readiness checks when the service starts. On by default: it
    #: is how an operator learns the bot cannot record *before* sending it
    #: into a meeting. Tests turn it off, because it opens a real Deepgram
    #: stream and calls the LLM - a suite that depends on the network and on
    #: live credentials is slow, flaky, and fails for reasons unrelated to
    #: the code under test.
    preflight_on_start: bool = True

    #: Notice when a call starts on this machine - any app taking the
    #: microphone - and offer to record it, in the page and as a Windows
    #: notification. It reads the per-app microphone state Windows already
    #: keeps for its own privacy indicator, and opens no audio. Tests turn it
    #: off so a suite run during a call does not raise notifications.
    call_alerts: bool = True

    # --- Transcription ----------------------------------------------------
    deepgram_api_key: str = ""
    deepgram_model: str = DEFAULT_DEEPGRAM_MODEL
    deepgram_language: str = "en-US"
    #: Silence (ms) that ends an utterance. This is an accuracy control, not
    #: just a latency one: at 300 ms - shorter than an ordinary pause for
    #: breath - sentences were cut in half, which reads badly and denies the
    #: model the context it uses to choose words and place punctuation.
    #: Lower it for snappier live captions, raise it for cleaner text.
    deepgram_endpointing_ms: int = 800
    #: Words to bias transcription towards: participant names, product names,
    #: team jargon. ASR gets these wrong most often - they are rare in the
    #: language model and frequently not English words at all - so this is
    #: the cheapest accuracy improvement available. nova-3 only.
    deepgram_keyterms: tuple[str, ...] = ()

    #: Ceiling (ms) on how long one utterance may run without a pause, so an
    #: unbroken stretch of speech is still split into readable turns.
    deepgram_utterance_end_ms: int = 1000

    # --- Analysis ---------------------------------------------------------
    analysis_enabled: bool = True
    llm_provider: LLMProvider = "anthropic"
    llm_model: str = ""
    #: Override the OpenAI adapter's endpoint. Any OpenAI-compatible provider
    #: (Groq, OpenRouter, a local Ollama) works with ``llm_provider="openai"``
    #: and its key in ``OPENAI_API_KEY``. Blank uses api.openai.com.
    llm_base_url: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # --- Logging ----------------------------------------------------------
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        # Normalise here so every construction path (env, tests, CLI) gets the
        # same guarantees.
        object.__setattr__(self, "bot_name", ensure_disclosure(self.bot_name))
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        object.__setattr__(
            self, "service_state_dir", Path(self.service_state_dir).expanduser()
        )
        if not self.llm_model:
            object.__setattr__(self, "llm_model", self.default_llm_model)

    @property
    def default_llm_model(self) -> str:
        """The model used when none is configured, for the active provider."""
        if self.llm_provider == "anthropic":
            return DEFAULT_ANTHROPIC_MODEL
        return DEFAULT_OPENAI_MODEL

    @property
    def llm_api_key(self) -> str:
        """API key for the configured LLM provider (may be empty)."""
        if self.llm_provider == "anthropic":
            return self.anthropic_api_key
        return self.openai_api_key

    @property
    def bridge_url(self) -> str:
        """WebSocket URL the injected browser hook connects back to."""
        return f"ws://{self.bridge_host}:{self.bridge_port}"

    def override(self, **kwargs: Any) -> "Config":
        """Return a copy with the non-``None`` keyword arguments applied.

        CLI flags default to ``None`` so that "flag not passed" is
        distinguishable from "flag passed with a falsy value".
        """
        applied = {k: v for k, v in kwargs.items() if v is not None}
        unknown = set(applied) - set(self.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"Unknown config field(s): {sorted(unknown)}")
        if not applied:
            return self
        return replace(self, **applied)

    def validate(self, *, require_meeting: bool = True) -> None:
        """Raise :class:`ConfigError` if the config cannot support a run.

        Args:
            require_meeting: When ``False`` (e.g. offline re-analysis of an
                existing transcript) the Meet URL and Deepgram key are not
                required.
        """
        problems: list[str] = []

        if require_meeting:
            if not self.meet_url:
                problems.append("meet_url is required (pass --url)")
            else:
                try:
                    validate_meet_url(self.meet_url)
                except ConfigError as exc:
                    problems.append(str(exc))
            if not self.deepgram_api_key:
                problems.append(
                    "DEEPGRAM_API_KEY is not set - live transcription cannot start"
                )
            if not 1 <= self.bridge_port <= 65535:
                problems.append(f"bridge_port {self.bridge_port} is out of range")
            if self.admission_timeout_s <= 0:
                problems.append("admission_timeout_s must be positive")

        if self.llm_provider not in ("anthropic", "openai"):
            problems.append(
                "llm_provider must be 'anthropic' or 'openai', "
                f"got {self.llm_provider!r}"
            )
        elif self.analysis_enabled and not self.llm_api_key:
            key_name = (
                "ANTHROPIC_API_KEY"
                if self.llm_provider == "anthropic"
                else "OPENAI_API_KEY"
            )
            problems.append(
                f"{key_name} is not set but analysis is enabled "
                "(set the key, or pass --no-analysis)"
            )

        if self.sample_rate not in (8_000, 16_000, 24_000, 48_000):
            problems.append(
                f"sample_rate {self.sample_rate} is not a supported rate "
                "(8000, 16000, 24000 or 48000)"
            )

        if problems:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))

    def redacted(self) -> dict[str, Any]:
        """A dict of the config safe to log - secrets replaced by a marker."""
        out: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name.endswith("_api_key"):
                value = f"<set:{len(value)} chars>" if value else "<unset>"
            elif isinstance(value, Path):
                value = str(value)
            out[name] = value
        return out


def load_config(
    env_file: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Build a :class:`Config` from ``.env`` plus the process environment.

    Args:
        env_file: Path to a dotenv file. When ``None``, python-dotenv searches
            upwards from the working directory for a ``.env``.
        env: Environment mapping to read (defaults to :data:`os.environ`).
            Injectable so tests never touch the real process environment.
    """
    if env is None:
        if env_file is not None:
            load_dotenv(env_file, override=False)
        else:
            load_dotenv(override=False)
        env = os.environ

    provider_raw = _env_str(env, "LLM_PROVIDER", "anthropic").lower()
    if provider_raw not in ("anthropic", "openai"):
        raise ConfigError(
            f"LLM_PROVIDER must be 'anthropic' or 'openai', got {provider_raw!r}"
        )
    provider: LLMProvider = "anthropic" if provider_raw == "anthropic" else "openai"

    return Config(
        meet_url=_env_str(env, "MEET_URL", ""),
        bot_name=_env_str(env, "BOT_DISPLAY_NAME", DEFAULT_BOT_NAME),
        output_dir=Path(_env_str(env, "OUTPUT_DIR", "recordings")),
        headless=_env_bool(env, "HEADLESS", True),
        admission_timeout_s=_env_int(env, "ADMISSION_TIMEOUT_S", 300),
        max_meeting_duration_s=_env_int(env, "MAX_MEETING_DURATION_S", 4 * 60 * 60),
        alone_timeout_s=_env_int(env, "ALONE_TIMEOUT_S", 120),
        browser_channel=_env_str(env, "BROWSER_CHANNEL", "") or None,
        bridge_host=_env_str(env, "BRIDGE_HOST", "127.0.0.1"),
        bridge_port=_env_int(env, "BRIDGE_PORT", 8765),
        sample_rate=_env_int(env, "SAMPLE_RATE", 16_000),
        per_participant_audio=_env_bool(env, "PER_PARTICIPANT_AUDIO", False),
        live_captions_overlay=_env_bool(env, "LIVE_CAPTIONS_OVERLAY", False),
        speaker_names_from_captions=_env_bool(
            env, "SPEAKER_NAMES_FROM_CAPTIONS", True
        ),
        anonymise_analysis=_env_bool(env, "ANONYMISE_ANALYSIS", False),
        chrome_profile_dir=(
            Path(env["CHROME_PROFILE_DIR"]).expanduser()
            if env.get("CHROME_PROFILE_DIR")
            else None
        ),
        service_state_dir=Path(
            _env_str(env, "SERVICE_STATE_DIR", "~/.meetbot")
        ).expanduser(),
        preflight_on_start=_env_bool(env, "PREFLIGHT_ON_START", True),
        call_alerts=_env_bool(env, "CALL_ALERTS", True),
        deepgram_api_key=_env_str(env, "DEEPGRAM_API_KEY", ""),
        deepgram_model=_env_str(env, "DEEPGRAM_MODEL", DEFAULT_DEEPGRAM_MODEL),
        deepgram_language=_env_str(env, "DEEPGRAM_LANGUAGE", "en-US"),
        deepgram_endpointing_ms=_env_int(env, "DEEPGRAM_ENDPOINTING_MS", 800),
        deepgram_utterance_end_ms=_env_int(env, "DEEPGRAM_UTTERANCE_END_MS", 1000),
        deepgram_keyterms=tuple(
            term.strip()
            for term in _env_str(env, "DEEPGRAM_KEYTERMS", "").split(",")
            if term.strip()
        ),
        analysis_enabled=_env_bool(env, "ANALYSIS_ENABLED", True),
        llm_provider=provider,
        llm_model=_env_str(env, "LLM_MODEL", ""),
        llm_base_url=_env_str(env, "LLM_BASE_URL", ""),
        anthropic_api_key=_env_str(env, "ANTHROPIC_API_KEY", ""),
        openai_api_key=_env_str(env, "OPENAI_API_KEY", ""),
        log_level=_env_str(env, "LOG_LEVEL", "INFO").upper(),
    )
