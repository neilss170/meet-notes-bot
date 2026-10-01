"""Tests for the log file's size cap.

The tray runs windowless, so the file is the only record of what happened -
and it is also unattended. Left alone it grew without limit: on the machine
this was written for, `scribe-server.log` had reached 1.8 MB in a few weeks
of ordinary use, with nothing in the code to stop it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from meetbot import logging_setup
from meetbot.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Put the root logger back, and close files so Windows can delete them.

    An open handler on a tmp_path file survives the test and makes the
    directory undeletable on Windows, which fails later tests rather than
    this one.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_the_shipped_cap_is_what_the_docs_promise() -> None:
    """docs/deploying.md tells people 2 MB and three older copies."""
    assert logging_setup._LOG_MAX_BYTES == 2 * 1024 * 1024
    assert logging_setup._LOG_BACKUPS == 3


def test_the_log_file_stops_growing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is held, and old copies are discarded rather than accumulating.

    The limit is shrunk for the test so this takes milliseconds rather than
    writing two real megabytes; the sizes above are what actually ships.
    """
    monkeypatch.setattr(logging_setup, "_LOG_MAX_BYTES", 2_000)
    monkeypatch.setattr(logging_setup, "_LOG_BACKUPS", 3)

    log_file = tmp_path / "scribe-server.log"
    configure_logging("INFO", log_file=log_file)
    logger = logging.getLogger("meetbot.test")

    for index in range(400):
        logger.info("a log line that takes up a bit of room %04d %s", index, "x" * 80)

    assert log_file.exists()
    assert log_file.stat().st_size <= 2_000 + 500, "the active file blew past the cap"

    backups = sorted(tmp_path.glob("scribe-server.log.*"))
    assert backups, "nothing rotated, so the cap was never reached"
    assert len(backups) <= 3, f"kept {len(backups)} old copies, wanted at most 3"

    # The point of the cap: total disk use is bounded no matter how long it runs.
    total = sum(path.stat().st_size for path in tmp_path.iterdir())
    assert total <= 4 * 2_000 + 2_000, f"bounded by the cap, not by uptime (got {total})"


def test_the_newest_lines_are_the_ones_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation must not discard the thing you came to the log to read."""
    monkeypatch.setattr(logging_setup, "_LOG_MAX_BYTES", 2_000)

    log_file = tmp_path / "scribe-server.log"
    configure_logging("INFO", log_file=log_file)
    logger = logging.getLogger("meetbot.test")

    for index in range(400):
        logger.info("line %04d %s", index, "y" * 80)
    logger.info("the thing that went wrong just now")

    assert "the thing that went wrong just now" in log_file.read_text(
        encoding="utf-8", errors="replace"
    )


def test_no_file_handler_without_a_file() -> None:
    """Console-only stays console-only - the CLI relies on this.

    Only the handlers configure_logging installed are counted. pytest's own
    logging plugin puts a FileHandler on the root logger too, so asking
    whether any exist answers a different question from the one here.
    """
    configure_logging("INFO")

    ours = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_meetbot_handler", False)
    ]
    assert [type(handler).__name__ for handler in ours] == ["StreamHandler"]
