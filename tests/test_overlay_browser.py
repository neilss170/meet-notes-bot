"""Caption-overlay tests that need a real browser.

Everything else in this suite is pure unit testing, but the overlay's whole
job is to produce a *video stream*, and the one bug that actually broke it in
practice - a canvas capture stream that emits no frames - is invisible to any
test that does not run a real compositor. So this module launches Chromium.

It skips itself if Playwright or its Chromium build is unavailable, so a
checkout without browsers installed still runs the rest of the suite green.
"""

from __future__ import annotations

import pytest

from meetbot.capture import load_inject_script

pytest.importorskip("playwright.async_api", reason="Playwright is not installed")

# A real https origin: navigator.mediaDevices only exists in a secure context,
# so about:blank cannot exercise the getDisplayMedia patch at all.
_ORIGIN = "https://meet.google.com/**"
_URL = "https://meet.google.com/test-room"
_STUB_PAGE = "<html><body>stub</body></html>"


class _Overlay:
    """A page with inject.js installed and the caption canvas being shared."""

    def __init__(self, page: object) -> None:
        self.page = page

    async def frames(self) -> int:
        return await self.page.evaluate(
            "() => window.__V.getVideoPlaybackQuality().totalVideoFrames"
        )


@pytest.fixture
async def browser_page():
    """Chromium with inject.js installed, nothing shared yet."""
    async for page in _make_page():
        yield page


async def _make_page():
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
        except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser"
            pytest.skip(f"Chromium is not available: {exc}")
        try:
            context = await browser.new_context()
            await context.add_init_script(
                "window.__MEETBOT_CONFIG__ = {bridgeUrl: 'ws://127.0.0.1:59999'};"
            )
            await context.add_init_script(load_inject_script())
            page = await context.new_page()
            await page.route(
                _ORIGIN,
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body=_STUB_PAGE
                ),
            )
            await page.goto(_URL)
            yield page
        finally:
            await browser.close()


@pytest.fixture
async def overlay():
    """Chromium with inject.js loaded, already sharing the caption canvas."""
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
        except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser"
            pytest.skip(f"Chromium is not available: {exc}")
        try:
            context = await browser.new_context()
            # Point the bridge at a dead port: the overlay must not depend on it.
            await context.add_init_script(
                "window.__MEETBOT_CONFIG__ = {bridgeUrl: 'ws://127.0.0.1:59999'};"
            )
            await context.add_init_script(load_inject_script())
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.route(
                _ORIGIN,
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body=_STUB_PAGE
                ),
            )
            await page.goto(_URL)
            # Meet clicking "Share screen" - happens before anyone has spoken,
            # which is exactly the ordering runner.py uses.
            await page.evaluate(
                """async () => {
                    const stream = await navigator.mediaDevices.getDisplayMedia({video: true});
                    const video = document.createElement('video');
                    video.autoplay = video.muted = video.playsInline = true;
                    document.body.appendChild(video);
                    video.srcObject = stream;
                    window.__V = video;
                    window.__S = stream;
                    await video.play();
                }"""
            )
            yield _Overlay(page)
            assert errors == [], f"uncaught page errors: {errors}"
        finally:
            await browser.close()


async def test_overlay_shared_flag_reports_a_real_share(overlay):
    """The flag join.py polls is false until Meet actually asks for a stream.

    ``start_caption_overlay`` treats this - not "a click succeeded" - as the
    success signal, so that a wrong share selector is reported instead of
    silently leaving the captions invisible for the whole meeting.
    """
    assert await overlay.page.evaluate("() => window.__meetbot.overlayShared()") is True
    assert (await overlay.page.evaluate("() => window.__meetbot.stats()"))["overlayShared"] is True


async def test_overlay_shared_flag_is_false_before_any_share(browser_page):
    """A fresh page has not shared anything yet."""
    assert await browser_page.evaluate("() => window.__meetbot.overlayShared()") is False


async def test_get_display_media_returns_the_caption_canvas(overlay):
    """Meet's screen-share request is answered with our 720p canvas stream."""
    settings = await overlay.page.evaluate(
        "() => window.__S.getVideoTracks()[0].getSettings()"
    )
    assert (settings["width"], settings["height"]) == (1280, 720)
    assert await overlay.page.evaluate(
        "() => window.__S.getVideoTracks()[0].readyState"
    ) == "live"


async def test_frames_keep_flowing_while_nobody_is_speaking(overlay):
    """Regression: the shared tile must not freeze during silence.

    A canvas capture stream only emits a frame when the canvas is painted; it
    does not resample a static canvas. The overlay used to paint only when an
    utterance arrived, so between utterances the stream stalled at zero frames
    and participants saw a black tile. A render loop keeps frames flowing.
    """
    before = await overlay.frames()
    await overlay.page.wait_for_timeout(2500)
    after = await overlay.frames()
    assert after - before >= 3, (
        f"stream stalled during silence: {before} -> {after} frames. "
        "captionOverlay.startRenderLoop() is not painting."
    )


async def test_captions_render_and_stream_stays_live(overlay):
    """Pushed utterances reach the canvas without disturbing the stream."""
    await overlay.page.evaluate(
        """() => {
            window.__meetbot.updateCaption('Neil Sharma', 'First utterance.');
            window.__meetbot.updateCaption('Speaker 2', 'A much longer second utterance that has to wrap.');
        }"""
    )
    await overlay.page.wait_for_timeout(1200)
    assert await overlay.frames() > 0
    assert await overlay.page.evaluate(
        "() => window.__S.getVideoTracks()[0].readyState"
    ) == "live"
    # The canvas is deliberately never added to the document; capturing works
    # on a detached canvas and attaching it would show up in Meet's own UI.
    assert await overlay.page.evaluate("() => document.querySelectorAll('canvas').length") == 0


async def test_render_loop_stops_when_sharing_stops(overlay):
    """Ending the share tears the timer down instead of painting forever."""
    await overlay.page.evaluate(
        """() => {
            const track = window.__S.getVideoTracks()[0];
            track.stop();
            track.dispatchEvent(new Event('ended'));
        }"""
    )
    await overlay.page.wait_for_timeout(300)
    settled = await overlay.frames()
    await overlay.page.wait_for_timeout(1500)
    assert await overlay.frames() == settled, "frames still arriving after share ended"
