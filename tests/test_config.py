"""Tests for configuration loading, validation and disclosure enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from meetbot.config import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_BOT_NAME,
    DEFAULT_OPENAI_MODEL,
    Config,
    ConfigError,
    ensure_disclosure,
    load_config,
    validate_meet_url,
)


# --- disclosure ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Meeting Notes Bot (Recording)",
        "Acme Transcription Service",
        "Team Notetaker",
        "recording assistant",
    ],
)
def test_names_that_already_disclose_are_untouched(name: str) -> None:
    assert ensure_disclosure(name) == " ".join(name.split())


@pytest.mark.parametrize("name", ["Helper", "Alex", "Assistant", ""])
def test_names_without_disclosure_get_a_suffix(name: str) -> None:
    result = ensure_disclosure(name)
    assert "record" in result.lower()


def test_empty_name_falls_back_to_the_default() -> None:
    assert ensure_disclosure("   ") == DEFAULT_BOT_NAME


def test_disclosure_is_length_capped() -> None:
    assert len(ensure_disclosure("A" * 200)) <= 60


def test_config_enforces_disclosure_on_construction() -> None:
    assert "Recording" in Config(bot_name="Silent Observer").bot_name


# --- URL validation --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://meet.google.com/abc-defg-hij",
        "https://meet.google.com/abc-defg-hij?authuser=0",
        "https://meet.google.com/lookup/my-standup",
        "  https://meet.google.com/abc-defg-hij  ",
    ],
)
def test_valid_meet_urls(url: str) -> None:
    assert validate_meet_url(url).startswith("https://meet.google.com/")


def test_scheme_is_added_when_missing() -> None:
    assert (
        validate_meet_url("meet.google.com/abc-defg-hij")
        == "https://meet.google.com/abc-defg-hij"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://zoom.us/j/123456",
        "https://meet.google.com/",
        "https://meet.google.com/toolongcode",
        "http://meet.google.com/abc-defg-hij",
        "not a url",
        "",
    ],
)
def test_invalid_meet_urls(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_meet_url(url)


# --- validation ------------------------------------------------------------


def test_valid_config_passes(config: Config) -> None:
    config.validate()


def test_missing_deepgram_key_is_reported() -> None:
    config = Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        analysis_enabled=False,
    )
    with pytest.raises(ConfigError, match="DEEPGRAM_API_KEY"):
        config.validate()


def test_missing_llm_key_is_reported_only_when_analysis_is_on() -> None:
    base = dict(
        meet_url="https://meet.google.com/abc-defg-hij",
        deepgram_api_key="dg",
    )
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Config(**base, analysis_enabled=True).validate()

    Config(**base, analysis_enabled=False).validate()


def test_openai_provider_asks_for_the_openai_key() -> None:
    config = Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        deepgram_api_key="dg",
        llm_provider="openai",
    )
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        config.validate()


def test_all_problems_are_reported_together() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config(sample_rate=12_345).validate()

    message = str(excinfo.value)
    assert "meet_url is required" in message
    assert "DEEPGRAM_API_KEY" in message
    assert "sample_rate" in message


def test_require_meeting_false_skips_meeting_checks() -> None:
    Config(anthropic_api_key="sk").validate(require_meeting=False)


# --- defaults and overrides ------------------------------------------------


def test_default_model_follows_the_provider() -> None:
    assert Config().llm_model == DEFAULT_ANTHROPIC_MODEL
    assert Config(llm_provider="openai").llm_model == DEFAULT_OPENAI_MODEL


def test_explicit_model_is_kept() -> None:
    assert Config(llm_model="claude-sonnet-5").llm_model == "claude-sonnet-5"


def test_llm_api_key_follows_the_provider() -> None:
    config = Config(anthropic_api_key="a", openai_api_key="o")
    assert config.llm_api_key == "a"
    assert config.override(llm_provider="openai").llm_api_key == "o"


def test_override_ignores_none_values(config: Config) -> None:
    assert config.override(headless=None, bot_name=None) is config


def test_override_applies_falsy_values(config: Config) -> None:
    assert config.override(headless=False).headless is False


def test_override_rejects_unknown_fields(config: Config) -> None:
    with pytest.raises(ConfigError, match="Unknown config field"):
        config.override(nonexistent=1)


def test_bridge_url(config: Config) -> None:
    assert config.bridge_url == "ws://127.0.0.1:18765"


def test_redacted_hides_secrets(config: Config) -> None:
    redacted = config.redacted()
    assert redacted["deepgram_api_key"] == "<set:11 chars>"
    assert redacted["anthropic_api_key"] == "<set:11 chars>"
    assert redacted["openai_api_key"] == "<unset>"
    assert "dg-test-key" not in str(redacted)


# --- environment loading ---------------------------------------------------


def test_load_config_from_a_mapping() -> None:
    config = load_config(
        env={
            "MEET_URL": "https://meet.google.com/abc-defg-hij",
            "BOT_DISPLAY_NAME": "QA Notetaker",
            "DEEPGRAM_API_KEY": "dg",
            "ANTHROPIC_API_KEY": "sk",
            "HEADLESS": "false",
            "BRIDGE_PORT": "9999",
            "PER_PARTICIPANT_AUDIO": "yes",
            "OUTPUT_DIR": "somewhere",
            "LOG_LEVEL": "debug",
        }
    )
    assert config.meet_url == "https://meet.google.com/abc-defg-hij"
    assert config.bot_name == "QA Notetaker"
    assert config.headless is False
    assert config.bridge_port == 9999
    assert config.per_participant_audio is True
    assert config.output_dir == Path("somewhere")
    assert config.log_level == "DEBUG"
    config.validate()


def test_load_config_uses_defaults_for_an_empty_environment() -> None:
    config = load_config(env={})
    assert config.bot_name == DEFAULT_BOT_NAME
    assert config.headless is True
    assert config.bridge_port == 8765
    assert config.analysis_enabled is True


def test_load_config_rejects_a_bad_provider() -> None:
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load_config(env={"LLM_PROVIDER": "gemini"})


def test_load_config_rejects_a_non_integer() -> None:
    with pytest.raises(ConfigError, match="BRIDGE_PORT"):
        load_config(env={"BRIDGE_PORT": "eight"})


def test_blank_env_values_fall_back_to_defaults() -> None:
    config = load_config(env={"BOT_DISPLAY_NAME": "   ", "BRIDGE_PORT": "  "})
    assert config.bot_name == DEFAULT_BOT_NAME
    assert config.bridge_port == 8765


def test_chrome_profile_dir_defaults_to_none() -> None:
    """Anonymous joining stays the default; signing in is opt-in."""
    config = Config(meet_url="https://meet.google.com/abc-defg-hij")
    assert config.chrome_profile_dir is None


def test_chrome_profile_dir_loads_from_the_environment(tmp_path) -> None:
    config = load_config(
        env={
            "MEET_URL": "https://meet.google.com/abc-defg-hij",
            "DEEPGRAM_API_KEY": "dg",
            "ANTHROPIC_API_KEY": "sk",
            "CHROME_PROFILE_DIR": str(tmp_path / "profile"),
        }
    )
    assert config.chrome_profile_dir == tmp_path / "profile"


def test_the_signed_in_profile_is_gitignored() -> None:
    """The profile holds live Google session cookies.

    Committing it would hand over the bot's account, so the ignore rule is
    part of the contract, not an editor convenience.
    """
    from pathlib import Path

    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert "chrome-profile/" in ignore
