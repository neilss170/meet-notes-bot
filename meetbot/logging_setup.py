"""Logging configuration for meetbot.

One place to configure handlers so the CLI, the bridge and the browser-console
forwarder all share a format. Nothing in this package calls :func:`print`.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(filename)s:%(lineno)d]: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

#: How much log to keep, and in how many files. The tray runs windowless
#: and logs at DEBUG, so the file is the only record of what happened - but
#: it is also unattended, and left alone it grows without limit. Two
#: megabytes is a few weeks of ordinary use, and three of them is enough
#: history to explain a recording that went wrong last week.
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUPS = 3

#: Third-party loggers that are far too chatty at DEBUG for our purposes.
_NOISY_LOGGERS = ("websockets", "urllib3", "httpx", "httpcore", "anthropic", "openai")


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Install console (and optionally file) handlers on the root logger.

    Safe to call more than once: existing handlers installed by this function
    are replaced rather than duplicated.

    Args:
        level: Root log level name, e.g. ``"INFO"`` or ``"DEBUG"``.
        log_file: When given, also write DEBUG-level logs to this path. The
            parent directory is created if needed. The file is rotated at
            :data:`_LOG_MAX_BYTES` and :data:`_LOG_BACKUPS` older copies are
            kept, so an unattended tray cannot fill the disk.
    """
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_meetbot_handler", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(min(numeric_level, logging.DEBUG if log_file else numeric_level))

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(numeric_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    console._meetbot_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        file_handler._meetbot_handler = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    # Keep dependency chatter out of our console at DEBUG, but let it through
    # to the file handler where it is genuinely useful for post-mortems.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric_level, logging.INFO))
