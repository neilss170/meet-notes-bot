"""The profile-sharing bug, reproduced against a real browser.

test_profile.py covers the copying itself. What no fake can express is the
Chromium behaviour that makes copying necessary: two browsers opening one
profile directory do not conflict loudly - the second is silently handed a
*blank* profile. In this project that meant a bot sent to a second concurrent
meeting joined signed-out, and Meet refuses anonymous guests outright, so the
failure surfaced as "You can't join this video call" and looked like a broken
join selector.

The trigger is specific and worth stating, because it is easy to write a test
here that passes for the wrong reason: Chromium claims the cookie store
*lazily*, on first use. A second browser launched while the first is merely
open still reads the session fine; it is locked out only once the first has
actually touched cookies. Measured, over three trials each:

    first browser untouched         -> second signed in 3/3
    first navigated to a data: URL  -> second signed in 3/3
    first made an https request     -> second signed in 0/3

So the first browser here must navigate to an https origin that sends the
cookie - which is exactly what joining Meet does, and nothing less will
reproduce it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from meetbot.profile import clone_profile, discard_profile

pytest.importorskip("playwright.async_api", reason="Playwright is not installed")

from playwright.async_api import BrowserContext, async_playwright  # noqa: E402

pytestmark = pytest.mark.asyncio

# A *persistent* cookie, like the Google session `meetbot login` leaves
# behind. Without an expiry this is a session cookie, dropped on close, and
# the test would fail for an unrelated reason.
_COOKIE = {"name": "SESSION", "value": "signed-in-as-bot",
           "domain": "example.com", "path": "/"}
_ORIGIN = "https://example.com"
_PAGE = f"{_ORIGIN}/meeting"


async def _sign_in(pw: object, profile: Path) -> None:
    """Write a signed-in session into ``profile``, as ``meetbot login`` does."""
    context = await pw.chromium.launch_persistent_context(str(profile), headless=True)
    await context.add_cookies([{**_COOKIE, "expires": time.time() + 86_400}])
    await context.close()


async def _session_of(context: BrowserContext) -> list[str]:
    return [c["value"] for c in await context.cookies(_ORIGIN)]


async def _join_a_meeting(context: BrowserContext) -> None:
    """Load an https page that sends the session cookie, as Meet does.

    Served by route interception so the suite stays offline.
    """
    await context.route(
        "**/*",
        lambda route: asyncio.ensure_future(
            route.fulfill(status=200, content_type="text/html", body="<h1>call</h1>")
        ),
    )
    await (await context.new_page()).goto(_PAGE)


async def test_a_second_meeting_on_the_same_profile_is_signed_out(
    tmp_path: Path,
) -> None:
    """The bug. If this ever stops failing, the clone may be unnecessary."""
    master = tmp_path / "master"
    async with async_playwright() as pw:
        await _sign_in(pw, master)
        first = await pw.chromium.launch_persistent_context(str(master), headless=True)
        try:
            await _join_a_meeting(first)
            second = await pw.chromium.launch_persistent_context(
                str(master), headless=True
            )
            try:
                # Chromium raises nothing here; it just hands over an empty
                # profile, and the bot walks into the call as a guest.
                assert await _session_of(second) == []
            finally:
                await second.close()
        finally:
            await first.close()


async def test_concurrent_meetings_stay_signed_in_on_their_own_clones(
    tmp_path: Path,
) -> None:
    """The fix, under the conditions that break the shared profile."""
    master = tmp_path / "master"
    async with async_playwright() as pw:
        await _sign_in(pw, master)
        clones = [clone_profile(master), clone_profile(master)]
        contexts = [
            await pw.chromium.launch_persistent_context(str(c), headless=True)
            for c in clones
        ]
        try:
            await _join_a_meeting(contexts[0])
            for context in contexts:
                assert await _session_of(context) == ["signed-in-as-bot"]
        finally:
            for context in contexts:
                await context.close()
            for clone in clones:
                discard_profile(clone)

        # And a later meeting is unaffected by either of those two.
        third = clone_profile(master)
        context = await pw.chromium.launch_persistent_context(str(third), headless=True)
        try:
            assert await _session_of(context) == ["signed-in-as-bot"]
        finally:
            await context.close()
            discard_profile(third)
