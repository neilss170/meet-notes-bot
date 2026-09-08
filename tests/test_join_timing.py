"""``first_visible`` must treat its timeout as a total budget.

Applying it per candidate is not a small inefficiency: with four name-field
candidates at 10s each, a signed-in join spent 40 seconds waiting for a field
that cannot exist, and the join button had a 75-second worst case. Measured
end to end, the bot took ~52 seconds to knock.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from meetbot.join import (
    TERMINAL_STATE_TIMEOUT_MS,
    any_visible,
    first_visible,
)
from meetbot.selectors import (
    IN_CALL_MARKERS,
    JOIN_DENIED_MARKERS,
    MEETING_ENDED_MARKERS,
)


class _Locator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self):
        return self

    async def wait_for(self, *, state: str, timeout: float) -> None:
        if self._visible:
            return
        # Mimic Playwright: block for the slice, then time out.
        await asyncio.sleep(timeout / 1000.0)
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError(f"timeout {timeout}ms")


class _Page:
    """A page where only ``visible`` matches, and everything else times out."""

    def __init__(self, visible: str | None = None) -> None:
        self.visible = visible
        self.calls: list[str] = []

    def locator(self, selector: str) -> _Locator:
        self.calls.append(selector)
        return _Locator(selector == self.visible)


pytest.importorskip("playwright.async_api")

MANY = [f"sel-{i}" for i in range(8)]


async def test_the_budget_is_total_not_per_candidate() -> None:
    """Eight candidates must not cost eight timeouts."""
    page = _Page(visible=None)
    started = time.monotonic()
    result = await first_visible(page, MANY, timeout_ms=1000)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 2.0, (
        f"{elapsed:.1f}s for a 1s budget over {len(MANY)} candidates - the "
        "timeout is being applied per candidate again"
    )


async def test_a_present_candidate_returns_promptly() -> None:
    page = _Page(visible="sel-0")
    started = time.monotonic()
    assert await first_visible(page, MANY, timeout_ms=5000) is not None
    assert time.monotonic() - started < 0.5


async def test_preference_order_still_decides_the_winner() -> None:
    """A later candidate must not win just because it was polled sooner."""
    page = _Page(visible="sel-5")
    assert await first_visible(page, MANY, timeout_ms=3000) is not None
    # Everything ahead of the match is tried first, in order.
    assert page.calls[:6] == MANY[:6]


async def test_a_broken_selector_is_reported_once_not_every_round() -> None:
    """Rounds repeat; a warning about a bad selector should not."""
    from playwright.async_api import Error as PlaywrightError

    class _BadPage(_Page):
        def locator(self, selector):
            self.calls.append(selector)
            if selector == "sel-1":
                raise PlaywrightError("bad selector")
            return _Locator(False)

    page = _BadPage(visible=None)
    assert await first_visible(page, MANY, timeout_ms=900) is None
    assert page.calls.count("sel-1") == 1, "broken selector retried every round"


class _SettlingLocator(_Locator):
    """A match that only resolves once the page has settled.

    Meet's refusal page is heavy: a ``:text()`` query against it fails for
    the first few hundred milliseconds and succeeds afterwards. Since
    ``first_visible`` polls in fixed slices, what rescues a slow match is a
    budget long enough to come back and ask again - not a longer single wait.
    """

    def __init__(self, visible: bool, settles_at: float) -> None:
        super().__init__(visible)
        self._settles_at = settles_at

    async def wait_for(self, *, state: str, timeout: float) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        remaining = self._settles_at - time.monotonic()
        if self._visible and remaining <= 0:
            return
        await asyncio.sleep(min(timeout / 1000.0, max(remaining, 0.0)))
        if self._visible and time.monotonic() >= self._settles_at:
            return
        raise PlaywrightTimeoutError(f"timeout {timeout}ms")


class _SettlingPage(_Page):
    def __init__(self, visible: str, settle_s: float) -> None:
        super().__init__(visible)
        self._settles_at = time.monotonic() + settle_s

    def locator(self, selector: str):
        return _SettlingLocator(selector == self.visible, self._settles_at)


class TestTerminalStateBudget:
    """Why a failed join must not be blamed on the selectors.

    Meet refused a live join with "You can't join this video call". The text
    was on the page and the marker matched it - but the refusal check ran at
    the 400ms default, and the query needed longer than that to resolve. The
    bot reported "Meet's DOM may have changed - update selectors.JOIN_BUTTON"
    and sent the operator to fix selectors that were entirely correct, in the
    middle of a live meeting.
    """

    #: Roughly how long the :text() query took to start resolving against the
    #: real refusal page.
    SETTLE_S = 0.6

    async def test_the_default_budget_is_too_short_to_see_a_refusal(self) -> None:
        page = _SettlingPage(JOIN_DENIED_MARKERS[-1], self.SETTLE_S)
        assert await any_visible(page, JOIN_DENIED_MARKERS) is False

    @pytest.mark.parametrize(
        "markers",
        [JOIN_DENIED_MARKERS, MEETING_ENDED_MARKERS, IN_CALL_MARKERS],
        ids=["denied", "ended", "in_call"],
    )
    async def test_the_terminal_state_budget_sees_a_slow_match(self, markers) -> None:
        """Every marker list, not just the one that bit us.

        The budget is spent in rounds across candidates, so a longer list has
        fewer attempts at the match - which is what would break if someone
        added markers without revisiting the timeout.
        """
        page = _SettlingPage(markers[-1], self.SETTLE_S)
        found = await any_visible(
            page, markers, timeout_ms=TERMINAL_STATE_TIMEOUT_MS
        )
        assert found is True, (
            f"{len(markers)} candidates sharing {TERMINAL_STATE_TIMEOUT_MS}ms "
            "could not catch a match that settles in "
            f"{self.SETTLE_S * 1000:.0f}ms"
        )
