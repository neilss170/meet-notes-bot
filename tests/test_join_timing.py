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

from meetbot.join import first_visible


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
