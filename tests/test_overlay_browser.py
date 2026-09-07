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


# --------------------------------------------------------------------------- #
# Caption extraction
# --------------------------------------------------------------------------- #

#: A caption panel shaped the way Meet renders one: a labelled region whose
#: children each pair a speaker name with the text recognised so far. The
#: extractor reads rendered text rather than class names precisely so that it
#: survives Google's obfuscated-build churn, which is what this exercises.
_CAPTION_DOM = """
<div role="region" aria-label="Captions">
  <div><div class="zs7s8d jxFHg">Neil Sharma</div><div>Morning everyone, thanks for joining.</div></div>
  <div><div class="zs7s8d jxFHg">Priya Menon</div><div>I finished the pipeline yesterday.</div></div>
</div>
"""


async def test_caption_extractor_reads_names_and_text(browser_page):
    """The one piece of caption scraping that depends on Meet's real DOM."""
    from meetbot import selectors

    await browser_page.set_content(_CAPTION_DOM)
    entries = await browser_page.eval_on_selector_all(
        selectors.CAPTION_REGION[0], selectors.CAPTION_ENTRY_EXTRACTOR
    )
    assert [e["name"] for e in entries] == ["Neil Sharma", "Priya Menon"]
    assert entries[0]["text"].startswith("Morning everyone")
    assert entries[1]["text"].startswith("I finished the pipeline")


async def test_caption_extractor_survives_an_empty_panel(browser_page):
    """A panel with no captions yet must yield nothing, not a stray entry."""
    from meetbot import selectors

    await browser_page.set_content(
        '<div role="region" aria-label="Captions"></div>'
    )
    entries = await browser_page.eval_on_selector_all(
        selectors.CAPTION_REGION[0], selectors.CAPTION_ENTRY_EXTRACTOR
    )
    assert entries == []


async def test_caption_extraction_feeds_attribution_end_to_end(browser_page):
    """Scraped DOM -> CaptionWatcher -> a real name for a timed utterance."""
    from meetbot import selectors
    from meetbot.captions import CaptionWatcher

    watcher = CaptionWatcher()
    # Meet rewrites a caption in place as its recogniser refines the phrase,
    # so each poll sees longer text. Polling identical content would mean the
    # speaker is only observed once, which is not what a live panel does.
    growing = [
        (1.0, "Morning everyone,"),
        (2.0, "Morning everyone, thanks"),
        (3.0, "Morning everyone, thanks for joining."),
    ]
    for now, text in growing:
        await browser_page.set_content(
            _CAPTION_DOM.replace("Morning everyone, thanks for joining.", text)
        )
        entries = await browser_page.eval_on_selector_all(
            selectors.CAPTION_REGION[0], selectors.CAPTION_ENTRY_EXTRACTOR
        )
        watcher.poll([(e["name"], e["text"]) for e in entries], now)

    # Priya's caption then grows, which is how Meet marks her as talking now.
    await browser_page.set_content(
        _CAPTION_DOM.replace(
            "I finished the pipeline yesterday.",
            "I finished the pipeline yesterday and it runs end to end.",
        )
    )
    entries = await browser_page.eval_on_selector_all(
        selectors.CAPTION_REGION[0], selectors.CAPTION_ENTRY_EXTRACTOR
    )
    watcher.poll([(e["name"], e["text"]) for e in entries], 6.0)

    assert watcher.resolve(1.0, 3.0) == "Neil Sharma"
    assert watcher.resolve(5.0, 6.0) == "Priya Menon"
