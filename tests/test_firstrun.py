"""Tests for `meetbot setup` - what a fresh install has to get right.

Every test here is about a way the old seven-step README let somebody down:
keys overwritten, keys asked for that were not needed, or a perfectly good
install reported as broken.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.firstrun import (
    DEFAULTS_SOURCE,
    ENV_NAME,
    TEMPLATE_NAME,
    Setup,
    blanks_in,
    browser_installed,
    browsers_dir,
    ensure_env_file,
    log_setup,
    run_setup,
)


def _load(_env_path: Path) -> Config:
    """Stand-in for load_config: nothing here should read the real .env."""
    return Config()


# --- the .env file --------------------------------------------------------


def test_a_fresh_clone_gets_an_env_file_from_the_template(tmp_path: Path) -> None:
    (tmp_path / TEMPLATE_NAME).write_text("DEEPGRAM_API_KEY=\n", encoding="utf-8")

    path, source = ensure_env_file(tmp_path)

    assert path == tmp_path / ENV_NAME
    assert source == TEMPLATE_NAME
    assert path.read_text(encoding="utf-8") == "DEEPGRAM_API_KEY=\n"


def test_an_existing_env_is_never_overwritten(tmp_path: Path) -> None:
    """The one failure here that cannot be undone.

    `.env` holds live API keys. Re-running setup - which the docs tell people
    to do after every upgrade - must not be a way to lose them, so there is
    no branch in which an existing file is written to.
    """
    (tmp_path / TEMPLATE_NAME).write_text("DEEPGRAM_API_KEY=\n", encoding="utf-8")
    mine = tmp_path / ENV_NAME
    mine.write_text("DEEPGRAM_API_KEY=the-real-one\n", encoding="utf-8")

    path, source = ensure_env_file(tmp_path)

    assert source == "", "an existing file was reported as freshly written"
    assert path.read_text(encoding="utf-8") == "DEEPGRAM_API_KEY=the-real-one\n"


def test_an_install_without_the_template_still_gets_somewhere_to_put_keys(
    tmp_path: Path,
) -> None:
    """A wheel from a registry has the package but not the committed template.

    Reporting it as copied from `.env.example` was a small lie that made the
    first thing setup printed untrue.
    """
    path, source = ensure_env_file(tmp_path)

    assert source == DEFAULTS_SOURCE
    body = path.read_text(encoding="utf-8")
    assert "DEEPGRAM_API_KEY=" in body
    assert "ANTHROPIC_API_KEY=" in body


# --- what is still missing ------------------------------------------------


def test_it_names_both_required_keys_when_nothing_is_set() -> None:
    blanks = blanks_in(Config())

    assert [blank.key for blank in blanks] == [
        "DEEPGRAM_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    for blank in blanks:
        assert blank.where.startswith("http"), "say where to get it, not just that it is missing"
        assert blank.needed_for


def test_a_key_already_in_the_environment_counts_as_filled_in() -> None:
    """Which is how this runs on a server, where there may be no .env at all."""
    assert blanks_in(
        Config(deepgram_api_key="dg-test", anthropic_api_key="sk-ant-test")
    ) == ()


def test_turning_the_write_up_off_needs_no_llm_key() -> None:
    """Transcripts work without an LLM, so asking for a key would be wrong."""
    blanks = blanks_in(
        Config(deepgram_api_key="dg-test", analysis_enabled=False)
    )

    assert blanks == ()


def test_the_openai_provider_is_asked_for_the_openai_key() -> None:
    blanks = blanks_in(
        Config(
            deepgram_api_key="dg-test",
            llm_provider="openai",
            anthropic_api_key="sk-ant-set-but-irrelevant",
        )
    )

    assert [blank.key for blank in blanks] == ["OPENAI_API_KEY"]


# --- the browser ----------------------------------------------------------


def test_the_browser_is_found_by_looking_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rather than by starting Playwright to ask it.

    Asking means starting the driver and tearing it straight back down, which
    on Windows prints "Task was destroyed but it is pending!" and an
    unretrieved TargetClosedError to stderr - two asyncio stack traces in the
    middle of a first run, which look exactly like the first run failing.
    """
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert browsers_dir() == tmp_path
    assert browser_installed() is False

    (tmp_path / "chromium-1234").mkdir()
    assert browser_installed() is True


def test_a_missing_browser_does_not_make_the_install_unfinished(
    tmp_path: Path,
) -> None:
    """Recording this machine never opens a browser.

    Treating Chromium as required is how `check` used to report a working
    local-capture install as "Not ready" and send people off to fix something
    they had no use for.
    """
    report = Setup(
        env_path=tmp_path / ENV_NAME, source="", blanks=(), browser=False
    )

    assert report.ready is True


# --- the whole command ----------------------------------------------------


def test_the_template_is_seeded_before_the_keys_are_read(tmp_path: Path) -> None:
    """Order matters: read first and a first run reports the wrong blanks.

    `created` is derived from `source`, so the two cannot disagree about
    whether this run wrote the file.
    """
    (tmp_path / TEMPLATE_NAME).write_text("DEEPGRAM_API_KEY=\n", encoding="utf-8")
    seen: list[Path] = []

    def load(env_path: Path) -> Config:
        seen.append(env_path)
        assert env_path.exists(), "config was read before .env was written"
        return Config()

    report = run_setup(tmp_path, load)

    assert seen == [tmp_path / ENV_NAME]
    assert report.created is True
    assert report.source == TEMPLATE_NAME
    assert report.ready is False


def test_a_second_run_reports_the_file_it_left_alone(tmp_path: Path) -> None:
    (tmp_path / ENV_NAME).write_text("DEEPGRAM_API_KEY=dg\n", encoding="utf-8")

    report = run_setup(
        tmp_path, lambda _p: Config(deepgram_api_key="dg", analysis_enabled=False)
    )

    assert report.created is False
    assert report.ready is True


def test_what_is_missing_is_logged_with_the_place_to_get_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="meetbot.firstrun"):
        log_setup(run_setup(tmp_path, _load))

    body = "\n".join(record.getMessage() for record in caplog.records)
    assert "DEEPGRAM_API_KEY" in body
    assert "console.deepgram.com" in body
    assert "run setup again" in body


def test_a_finished_install_is_pointed_at_check(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Setup answers "is this installed"; check answers "will it work"."""
    (tmp_path / ENV_NAME).write_text("x\n", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="meetbot.firstrun"):
        log_setup(
            run_setup(
                tmp_path,
                lambda _p: Config(
                    deepgram_api_key="dg", anthropic_api_key="sk-ant"
                ),
            )
        )

    body = "\n".join(record.getMessage() for record in caplog.records)
    assert "meetbot check" in body
