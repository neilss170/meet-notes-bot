"""The checks that must happen before a meeting, not during one.

Every wasted call in this project began the same way: something was already
broken - an expired Google session, a stream Deepgram would refuse - and
nobody found out until people were waiting in a meeting. These tests are
about that: the checks have to be honest about a broken setup, and specific
about what to do, or they are worse than no checks at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.preflight import (
    CheckResult,
    Preflight,
    check_config,
    check_llm,
    run_preflight,
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        meet_url="",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg-key",
        openai_api_key="llm-key",
        llm_provider="openai",
        service_state_dir=tmp_path / "state",
    )


class TestPreflightVerdict:
    def test_a_blocking_failure_stops_recording(self) -> None:
        report = Preflight([
            CheckResult("Google sign-in", ok=False, detail="signed out"),
            CheckResult("Deepgram", ok=True, detail="fine"),
        ])
        assert report.can_record is False
        assert report.ok is False
        assert [r.name for r in report.blockers] == ["Google sign-in"]

    def test_a_non_blocking_failure_still_allows_recording(self) -> None:
        """No summary is a degraded meeting; no transcript is a wasted one.

        The analysis can be re-run offline afterwards, so a broken LLM key
        must not stop the bot from capturing the call.
        """
        report = Preflight([
            CheckResult("Deepgram", ok=True, detail="fine"),
            CheckResult("AI notes", ok=False, detail="bad key", blocking=False),
        ])
        assert report.can_record is True
        assert report.ok is False
        assert [r.name for r in report.warnings] == ["AI notes"]

    def test_everything_passing_is_ready(self) -> None:
        report = Preflight([CheckResult("Deepgram", ok=True, detail="fine")])
        assert report.ok and report.can_record
        assert report.blockers == [] and report.warnings == []

    def test_survives_the_json_round_trip(self) -> None:
        """The UI reads this; a missing field silently empties the banner."""
        report = Preflight([
            CheckResult("Google sign-in", ok=False, detail="signed out",
                        fix="python -m meetbot login"),
        ])
        payload = report.to_dict()
        assert payload["can_record"] is False
        assert payload["checks"][0]["fix"] == "python -m meetbot login"


class TestFixInstructions:
    """A failure the operator cannot act on is only half a check."""

    def test_a_missing_profile_says_how_to_sign_in(self, config: Config) -> None:
        from meetbot.preflight import check_google_session

        result = asyncio.run(check_google_session(config))
        assert result.ok is False
        assert "login" in result.fix

    def test_a_missing_profile_directory_is_named(self, tmp_path: Path) -> None:
        from meetbot.preflight import check_google_session

        missing = tmp_path / "never-signed-in"
        result = asyncio.run(
            check_google_session(
                Config(meet_url="", chrome_profile_dir=missing, deepgram_api_key="k")
            )
        )
        assert result.ok is False
        assert "login" in result.fix

    def test_a_missing_llm_key_names_the_variable(self, tmp_path: Path) -> None:
        result = check_llm(
            Config(meet_url="", llm_provider="openai", openai_api_key="",
                   deepgram_api_key="k")
        )
        assert result.ok is False
        assert "OPENAI_API_KEY" in result.detail
        assert result.blocking is False, "a missing summary must not block recording"

    def test_disabled_analysis_is_not_a_failure(self, tmp_path: Path) -> None:
        result = check_llm(
            Config(meet_url="", analysis_enabled=False, deepgram_api_key="k")
        )
        assert result.ok is True

    def test_a_missing_deepgram_key_is_blocking(self, tmp_path: Path) -> None:
        from meetbot.preflight import check_deepgram

        result = asyncio.run(
            check_deepgram(Config(meet_url="", deepgram_api_key=""))
        )
        assert result.ok is False and result.blocking is True
        assert "DEEPGRAM_API_KEY" in result.detail


class TestOfflineMode:
    def test_skips_the_network_checks(self, config: Config) -> None:
        report = asyncio.run(run_preflight(config, offline=True))
        names = [r.name for r in report.results]
        assert "Deepgram" in names
        assert report.can_record is True, "offline must not report a false failure"
        assert "Google sign-in" not in names

    def test_config_is_still_validated_offline(self, tmp_path: Path) -> None:
        broken = Config(meet_url="", sample_rate=12_345, deepgram_api_key="k")
        report = asyncio.run(run_preflight(broken, offline=True))
        assert report.can_record is False
        assert check_config(broken).ok is False


class TestConfigCheck:
    def test_accepts_a_workable_configuration(self, config: Config) -> None:
        assert check_config(config).ok is True

    def test_rejects_an_unsupported_sample_rate(self, tmp_path: Path) -> None:
        result = check_config(Config(meet_url="", sample_rate=12_345,
                                     deepgram_api_key="k"))
        assert result.ok is False
        assert "12345" in result.detail
        assert result.fix
