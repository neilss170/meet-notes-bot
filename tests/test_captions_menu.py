"""Turning captions on when Meet has hidden the control in a menu.

Real speaker names come only from Meet's caption panel, so a captions button
the bot cannot find means every line in the transcript reads "Speaker 0".

That is what a live call produced. Meet's toolbar that day held only:

    Audio settings, Backgrounds and effects, Chat with everyone, Leave call,
    More options, Reframe, Turn on camera, Turn on microphone, Video settings

No captions control at all - it had moved into the overflow menu.
"""

from __future__ import annotations

import pytest

from meetbot import selectors
from meetbot.join import MeetSession

pytest.importorskip("playwright.async_api")

#: What Meet actually rendered on the toolbar during that call.
TOOLBAR_ONLY = [
    "Audio settings", "Backgrounds and effects", "Chat with everyone",
    "Leave call", "More options", "Reframe", "Turn on camera",
    "Turn on microphone", "Video settings",
]


class _Element:
    def __init__(self, page: "_Page", name: str) -> None:
        self._page = page
        self.name = name
        self.clicked = 0

    @property
    def first(self):
        return self

    async def wait_for(self, *, state: str, timeout: float) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        if self.name not in self._page.visible():
            raise PlaywrightTimeoutError(f"{self.name} not visible")

    async def click(self, timeout: float = 0) -> None:
        self.clicked += 1
        self._page.clicks.append(self.name)
        if self.name == "More options":
            self._page.menu_open = True


class _Keyboard:
    def __init__(self, page: "_Page") -> None:
        self._page = page

    async def press(self, key: str) -> None:
        if key == "Escape":
            self._page.menu_open = False


class _Page:
    """A Meet toolbar with no captions button, and a menu that may hold one."""

    def __init__(self, *, captions_in_menu: bool) -> None:
        self.captions_in_menu = captions_in_menu
        self.menu_open = False
        self.clicks: list[str] = []
        self.keyboard = _Keyboard(self)

    def is_closed(self) -> bool:
        return False

    def visible(self) -> set[str]:
        showing = set(TOOLBAR_ONLY)
        if self.menu_open and self.captions_in_menu:
            showing.add("Captions")
        return showing

    def locator(self, selector: str) -> _Element:
        # Crude but faithful enough: map a selector to the control it targets.
        if "More options" in selector or "more options" in selector:
            return _Element(self, "More options")
        if "captions" in selector.lower():
            return _Element(self, "Captions")
        return _Element(self, "nothing")

    async def evaluate(self, *args, **kwargs):
        return []

    async def eval_on_selector_all(self, *args, **kwargs):
        # _log_labelled_controls reads the page to report real labels.
        return sorted(self.visible())


def _session(page: _Page) -> MeetSession:
    from meetbot.config import Config

    session = MeetSession(Config(meet_url="https://meet.google.com/abc-defg-hij"))
    session._page = page
    return session


class TestCaptionsInTheOverflowMenu:
    async def test_finds_the_control_the_toolbar_does_not_show(self) -> None:
        page = _Page(captions_in_menu=True)
        found = await _session(page)._captions_in_overflow_menu()
        assert found is not None, "captions were in the menu and were missed"
        assert "More options" in page.clicks, "the menu was never opened"

    async def test_the_toolbar_alone_really_does_not_have_it(self) -> None:
        """Guards the premise: this is why the menu has to be opened at all."""
        page = _Page(captions_in_menu=True)
        assert "Captions" not in page.visible()

    async def test_closes_the_menu_when_there_is_nothing_in_it(self) -> None:
        """A menu left open covers the controls tried next."""
        page = _Page(captions_in_menu=False)
        assert await _session(page)._captions_in_overflow_menu() is None
        assert page.menu_open is False

    async def test_a_closed_page_is_not_an_error(self) -> None:
        session = _session(_Page(captions_in_menu=True))
        session._page = None
        assert await session._captions_in_overflow_menu() is None


class TestCaptionSelectors:
    def test_the_menu_item_is_matched_on_the_word_not_an_exact_label(self) -> None:
        """Meet words this differently by release and by captions state."""
        assert selectors.CAPTIONS_MENU_ITEM
        for selector in selectors.CAPTIONS_MENU_ITEM:
            assert "captions" in selector.lower()
            assert "Turn on captions" not in selector, (
                "an exact label breaks the moment Meet rewords it"
            )

    def test_the_overflow_button_is_covered(self) -> None:
        assert any(
            "More options" in s or "more options" in s
            for s in selectors.MORE_OPTIONS_BUTTON
        )
