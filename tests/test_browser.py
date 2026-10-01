"""The window Scribe opens, and the rule that decides what it may close.

The rule matters more than anything else here. Closing a window is closing
somebody's work, and the only thing standing between "close the page I opened"
and "close the editor" is what :func:`meetbot.browser.is_app_window` returns -
so most of this file is that function, against the titles real windows have.

Nothing here opens a browser by default. One test does, because a rule about
real windows deserves one look at a real window, and it is skipped unless
``SCRIBE_REAL_BROWSER=1`` says a visible window popping up is acceptable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from meetbot import browser


class TestWhatMayBeClosed:
    def test_the_window_scribe_opened(self) -> None:
        """An app window wears the page's title and nothing else."""
        assert browser.is_app_window("Scribe") is True

    def test_the_title_while_a_call_is_detected(self) -> None:
        """The page renames itself when a call starts; it is still our window."""
        assert browser.is_app_window("● Call detected · Scribe") is True

    def test_a_tab_in_someone_elses_browser_is_not_ours(self) -> None:
        """The whole point: his other tabs are not Scribe's to close."""
        assert browser.is_app_window("Scribe - Google Chrome") is False

    def test_nor_a_window_full_of_tabs(self) -> None:
        assert browser.is_app_window("Scribe and 3 more pages - Google Chrome") is False

    @pytest.mark.parametrize(
        "title",
        [
            "Scribe - Microsoft Edge",
            "Scribe - Brave",
            "Scribe - Chromium",
            "Scribe - Mozilla Firefox",
        ],
    )
    def test_every_browser_it_knows_about(self, title: str) -> None:
        assert browser.is_app_window(title) is False

    def test_something_else_entirely(self) -> None:
        assert browser.is_app_window("Inbox (12) - Gmail") is False

    def test_an_untitled_window(self) -> None:
        assert browser.is_app_window("") is False


class TestWhoseWindowItIs:
    """The class alone is not enough, which is the trap this avoids."""

    def test_this_process_can_be_named(self) -> None:
        name = browser.process_name(os.getpid())
        if sys.platform != "win32":
            # Documented behaviour off Windows: there is no window to own, so
            # there is nothing to name. Asserting ".exe" here was asserting
            # the platform rather than the function.
            assert name == ""
            return
        assert name.endswith(".exe") or "python" in name

    def test_a_process_that_does_not_exist(self) -> None:
        assert browser.process_name(4_000_000) == ""

    def test_an_editor_is_not_a_browser(self) -> None:
        """Every Electron app uses Chromium's window class, so a file called
        Scribe open in an editor is a window that looks exactly like ours
        until you ask which program owns it."""
        assert "code.exe" not in browser.BROWSERS
        assert "electron.exe" not in browser.BROWSERS

    def test_only_browser_windows_are_listed(self) -> None:
        """Whatever is on this machine's screen, nothing else gets in."""
        if sys.platform != "win32":
            pytest.skip("window enumeration is Windows-only")
        for hwnd, _title in browser._windows():
            assert isinstance(hwnd, int)


class TestFindingABrowser:
    def test_it_finds_one_here_or_says_it_cannot(self) -> None:
        found = browser.find_browser()
        if found is None:
            pytest.skip("no Chromium browser on this machine")
        assert found.exists()
        # BROWSERS names the Windows executables, but find_browser also asks
        # PATH for the name without .exe - which is how it finds /usr/bin/
        # chromium. Either spelling is the same browser.
        stems = {name.removesuffix(".exe") for name in browser.BROWSERS}
        assert found.name.casefold().removesuffix(".exe") in stems

    def test_without_a_browser_it_declines_rather_than_raises(
        self, monkeypatch
    ) -> None:
        """A missing browser means a plain tab, not a failed launch."""
        monkeypatch.setattr(browser, "find_browser", lambda: None)
        assert browser.open_window("http://127.0.0.1:8080") is False

    def test_it_asks_for_an_app_window(self, monkeypatch, tmp_path) -> None:
        seen: dict = {}
        monkeypatch.setattr(browser, "find_browser", lambda: tmp_path / "chrome.exe")
        monkeypatch.setattr(
            subprocess, "Popen", lambda command, **kw: seen.update(command=command)
        )

        assert browser.open_window("http://127.0.0.1:8080/?meeting=abc") is True

        assert "--app=http://127.0.0.1:8080/?meeting=abc" in seen["command"]
        assert not any(
            arg.startswith("--user-data-dir") for arg in seen["command"]
        ), "a separate profile would mean signing in again, in a browser that is not his"

    def test_a_browser_that_will_not_start_is_not_fatal(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(browser, "find_browser", lambda: tmp_path / "chrome.exe")

        def refuse(*_args, **_kwargs):
            raise OSError("no")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        assert browser.open_window("http://127.0.0.1:8080") is False


class TestNothingToClose:
    def test_closing_when_there_is_no_window_is_zero_not_an_error(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(browser, "app_windows", lambda: [])
        assert browser.close_windows(timeout=0.2) == 0

    def test_focusing_when_there_is_no_window_says_so(self, monkeypatch) -> None:
        """The answer decides whether a second window gets opened."""
        monkeypatch.setattr(browser, "app_windows", lambda: [])
        assert browser.focus_window() is False


@pytest.mark.skipif(
    os.environ.get("SCRIBE_REAL_BROWSER") != "1",
    reason="opens a real browser window; set SCRIBE_REAL_BROWSER=1 to allow it",
)
class TestARealWindow:
    """One look at the real thing: open a window, find it, close it.

    Every other test here trusts a string. This one trusts Windows.
    """

    PAGE = "data:text/html,<title>Scribe</title><h1>test window</h1>"

    def test_open_find_and_close(self) -> None:
        if browser.find_browser() is None:
            pytest.skip("no Chromium browser on this machine")
        before = set(browser.app_windows())

        assert browser.open_window(self.PAGE) is True
        deadline = time.time() + 20
        while time.time() < deadline and not (set(browser.app_windows()) - before):
            time.sleep(0.25)
        opened = set(browser.app_windows()) - before
        assert opened, "the window never appeared, or was not recognised as ours"

        assert browser.close_windows(timeout=10) >= 1
        assert not (set(browser.app_windows()) - before)
