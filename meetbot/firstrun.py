"""Getting from a fresh clone to a working install, in one command.

Setting this up used to be seven steps in the README, and three of them were
wrong for almost everybody who followed them. The dependencies were installed
twice from two files that had drifted apart, so which extras you ended up
with depended on the order you ran them in. Signing a Google account in was
step five of seven, ahead of anything that actually records, even though the
default way to record never touches Google. And the one step worth running -
``check`` - came last, so a missing key was discovered at the end of a
ten-minute install rather than the start.

What is left is this: copy the template, say which keys are still blank and
where to get them, and hand over to ``check``. Nothing here reaches the
network or installs anything, so it is safe to run repeatedly and finishes in
under a second.

The division of labour with :mod:`meetbot.preflight` is that this module
answers "is this installation finished", which is a question about files on
disk, and preflight answers "will a recording work", which needs the network.
Running setup twice is how you confirm the first run took.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meetbot.config import Config

logger = logging.getLogger(__name__)

#: The committed template and the file it seeds. Keeping the names here means
#: the setup command and the docs cannot disagree about them.
TEMPLATE_NAME: Final[str] = ".env.example"
ENV_NAME: Final[str] = ".env"

#: How a new ``.env`` was arrived at, for reporting. A wheel installed from a
#: registry has the package but not the repository's committed files, so the
#: template is not always there to copy.
DEFAULTS_SOURCE: Final[str] = "the built-in defaults"

#: What to write when there is no template to copy.
_BARE_ENV: Final[str] = """\
# Scribe configuration. docs/configuration.md lists everything else that can
# go in here; these two are the ones nothing works without.
DEEPGRAM_API_KEY=
ANTHROPIC_API_KEY=
"""


@dataclass(frozen=True)
class Blank:
    """One setting that has to be filled in before Scribe can work.

    Attributes:
        key: The environment variable, as it appears in ``.env``.
        needed_for: What stops working while it is blank, in a few words.
        where: Where to go to get a value for it.
    """

    key: str
    needed_for: str
    where: str


@dataclass(frozen=True)
class Setup:
    """What one run of ``meetbot setup`` found.

    Attributes:
        env_path: The ``.env`` that was created or already existed.
        source: Where a newly written ``.env`` came from - the template, or
            :data:`DEFAULTS_SOURCE` when there was none to copy. Empty when
            the file was already there, which is the second-run case: an
            existing ``.env`` is never overwritten, because it holds the keys.
        blanks: Settings still needing a value.
        browser: Whether Playwright's Chromium is installed. Only bot mode
            needs it, so this never makes an installation unfinished.
    """

    env_path: Path
    source: str
    blanks: tuple[Blank, ...]
    browser: bool

    @property
    def created(self) -> bool:
        """Whether this run wrote the file."""
        return bool(self.source)

    @property
    def ready(self) -> bool:
        """Whether there is nothing left for a person to do by hand.

        The browser is deliberately not part of this. Recording this machine
        is the default path and does not involve a browser at all, so an
        install without Chromium is finished for most people - treating it as
        unfinished was how ``check`` used to report a good setup as
        "Not ready".
        """
        return not self.blanks


def ensure_env_file(directory: Path) -> tuple[Path, str]:
    """Seed ``.env``, and say where its contents came from.

    An existing ``.env`` is never touched. It holds API keys, and there is no
    version of "helpfully refreshing the template" worth the chance of
    overwriting them.

    Returns:
        The path to ``.env``, and what it was written from -
        :data:`TEMPLATE_NAME`, :data:`DEFAULTS_SOURCE`, or an empty string
        when the file was already there.
    """
    env_path = directory / ENV_NAME
    if env_path.exists():
        return env_path, ""

    template = directory / TEMPLATE_NAME
    if template.exists():
        shutil.copyfile(template, env_path)
        return env_path, TEMPLATE_NAME

    env_path.write_text(_BARE_ENV, encoding="utf-8")
    return env_path, DEFAULTS_SOURCE


def blanks_in(config: Config) -> tuple[Blank, ...]:
    """Which required settings are still empty, in the order to fix them.

    Takes resolved configuration rather than reading the file, so a key
    exported into the environment counts as filled in - which is how this
    runs on a server, where there may be no ``.env`` at all.
    """
    found: list[Blank] = []

    if not config.deepgram_api_key:
        found.append(
            Blank(
                "DEEPGRAM_API_KEY",
                "transcribing anything at all",
                "https://console.deepgram.com/ -> API Keys",
            )
        )

    # The summary is the only thing an LLM key buys, and it can be turned
    # off, so a deliberate ANALYSIS_ENABLED=false is a finished install.
    if config.analysis_enabled and not config.llm_api_key:
        if config.llm_provider == "anthropic":
            found.append(
                Blank(
                    "ANTHROPIC_API_KEY",
                    "writing the notes up after a meeting",
                    "https://console.anthropic.com/settings/keys",
                )
            )
        else:
            found.append(
                Blank(
                    "OPENAI_API_KEY",
                    "writing the notes up after a meeting",
                    "https://platform.openai.com/api-keys, or any "
                    "OpenAI-compatible endpoint via LLM_BASE_URL",
                )
            )

    return tuple(found)


def browsers_dir() -> Path:
    """Where ``playwright install`` puts its browsers on this platform."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    # "0" is Playwright's own flag for "inside the package", not a path. It is
    # rare and unpicking it means reaching into Playwright's internals, so it
    # falls through to the default and at worst prints a hint that is not
    # needed - see browser_installed.
    if override and override != "0":
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def browser_installed() -> bool:
    """Whether Playwright has a Chromium to drive.

    This looks on disk instead of asking Playwright, which sounds like the
    lazy way round and is not. Asking means starting the driver - a Node
    subprocess and an event loop - and tearing it straight back down, which
    on Windows prints "Task was destroyed but it is pending!" and an
    unretrieved ``TargetClosedError`` to stderr. Two asyncio stack traces in
    the middle of a first-run setup look exactly like the setup failing.

    Being wrong here is cheap in one direction only: a false negative prints
    an install line somebody does not need, and ``check`` launches the
    browser for real anyway.
    """
    return any(browsers_dir().glob("chromium-*"))


def run_setup(directory: Path, load: Callable[[Path], Config]) -> Setup:
    """Do the whole of ``meetbot setup`` and report what is left.

    Args:
        directory: Where ``.env`` belongs.
        load: How to resolve configuration from a given ``.env``. Passed in
            rather than imported so the order here is unambiguous: the
            template is seeded *first* and read back *second*, so a first run
            reports the blanks in the new file rather than in a bare
            environment.
    """
    env_path, source = ensure_env_file(directory)
    return Setup(
        env_path=env_path,
        source=source,
        blanks=blanks_in(load(env_path)),
        browser=browser_installed(),
    )


def log_setup(report: Setup) -> None:
    """Write the outcome out in the order a reader needs to act on it."""
    if report.created:
        logger.info("Created %s from %s", report.env_path, report.source)
    else:
        logger.info("Using the existing %s", report.env_path)

    for blank in report.blanks:
        logger.error("  MISSING  %-20s needed for %s", blank.key, blank.needed_for)
        logger.warning("           %-20s %s", "", blank.where)

    if not report.browser:
        logger.info(
            "Chromium is not installed. Only bot mode needs it; to enable "
            "that: playwright install chromium"
        )

    logger.info("")
    if report.ready:
        logger.info("Setup is done. Confirm it works: python -m meetbot check")
    else:
        logger.error(
            "Add the %s above to %s, then run setup again.",
            "key" if len(report.blanks) == 1 else "keys",
            report.env_path.name,
        )


__all__ = [
    "Blank",
    "DEFAULTS_SOURCE",
    "ENV_NAME",
    "Setup",
    "TEMPLATE_NAME",
    "blanks_in",
    "browser_installed",
    "browsers_dir",
    "ensure_env_file",
    "log_setup",
    "run_setup",
]
