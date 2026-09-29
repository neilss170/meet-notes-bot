"""The chat rail, citations and call alerts, driven in a real browser.

The API tests prove the routes. These prove what somebody actually sees: a
numbered citation that opens the moment it came from, a switch that turns
citations off and stays off, and a card that appears when a call starts and
records it in one click.

Skipped when Playwright or its Chromium build is unavailable. Nothing reaches
the network or an audio device: the model, the microphone and the recorder
are all fakes.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.service import app as app_module
from meetbot.service import jobs as jobs_module
from meetbot.service.app import create_app
from meetbot.service.auth import Role
from meetbot.service.jobs import free_port
from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

pytest.importorskip("playwright.async_api", reason="Playwright is not installed")
uvicorn = pytest.importorskip("uvicorn", reason="uvicorn is not installed")

PASSWORD = "a-long-enough-password"
WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000


class _CitingLLM:
    """Cites the budget line whenever it is in the excerpts, as a model would."""

    model = "citing-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete_text(self, *, system: str, user: str, max_tokens: int = 0) -> str:
        self.prompts.append(user)
        found = re.search(r"\[(S\d+)\][^\n]*capped at forty thousand", user)
        if found:
            return f"The marketing budget is capped at forty thousand [{found.group(1)}]."
        return "That did not come up."

    def complete_json(self, **_kwargs) -> dict:
        return {}


def _recording(directory: Path, title: str, lines: list[tuple[str, str]]) -> None:
    store = TranscriptStore(directory / "transcript.jsonl")
    store.write_meta(MeetingMeta(meet_url=title, bot_name="Scribe (local capture)"))
    for i, (speaker, text) in enumerate(lines):
        store.append(Utterance(speaker, text, i * 20.0, i * 20.0 + 5.0))
    store.close()


@pytest.fixture
def served(tmp_path: Path, monkeypatch):
    """The real app on a real port, with two past recordings on disk."""
    recordings = tmp_path / "recordings"
    _recording(
        recordings / "20260908-standup",
        "Standup",
        [
            ("Speaker 0", "The API work is two weeks behind."),
            ("Speaker 1", "I will take the migration script."),
        ],
    )
    _recording(
        recordings / "20260909-finance",
        "Finance sync",
        [
            ("Speaker 0", "Let's look at next quarter."),
            ("Speaker 1", "The marketing budget is capped at forty thousand."),
            ("Speaker 0", "Anything above that needs sign-off."),
        ],
    )

    llm = _CitingLLM()
    monkeypatch.setattr(app_module, "build_client", lambda *a, **k: llm)

    # The microphone. A test starts a call by adding a session here.
    from meetbot.capture import calls as calls_module

    sessions: list = []
    monkeypatch.setattr(calls_module, "read_mic_sessions", lambda: list(sessions))
    monkeypatch.setattr(calls_module, "visible_window_titles", lambda: [])
    monkeypatch.setattr(calls_module, "GRACE_S", 0.0)
    monkeypatch.setattr(app_module, "POLL_INTERVAL_S", 0.1)
    monkeypatch.setattr(app_module, "detection_available", lambda: (True, "watching"))
    monkeypatch.setattr(app_module, "show_toast", lambda prompt, base: True)

    # Recording this machine, without an audio device.
    import meetbot.capture.local as local_module

    monkeypatch.setattr(local_module, "capture_available", lambda: (True, "ok"))

    async def recording(config, *, on_output_dir=None, **_kwargs):
        directory = config.output_dir / "from-call"
        directory.mkdir(parents=True, exist_ok=True)
        if on_output_dir is not None:
            on_output_dir(directory)
        await asyncio.sleep(3600)

    monkeypatch.setattr(jobs_module, "run_local_meeting", recording)

    config = Config(
        meet_url="",
        output_dir=recordings,
        deepgram_api_key="dg-test-key",
        anthropic_api_key="sk-ant-test",
        service_state_dir=tmp_path / "state",
        preflight_on_start=False,
        call_alerts=True,
        # A recording stopped in one of these tests would otherwise be written
        # up mid-assertion by the canned model.
        auto_notes=False,
    )
    app = create_app(config)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not getattr(server, "started", False):
        if time.time() > deadline:
            server.should_exit = True
            pytest.skip("the test server did not start")
        time.sleep(0.05)
    app.state.users.add("neil", PASSWORD, Role.ADMIN)

    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "app": app,
            "llm": llm,
            "sessions": sessions,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _start_call(served) -> None:
    from meetbot.capture.calls import MicSession

    filetime = int(time.time() * 10_000_000) + _FILETIME_UNIX_EPOCH
    served["sessions"].append(MicSession(WHATSAPP, filetime, True))


async def _open(playwright_api, served, path: str = "/"):
    """A signed-in page, and the console errors it saw."""
    try:
        browser = await playwright_api.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser"
        pytest.skip(f"Chromium is not available: {exc}")
    errors: list[str] = []
    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    await page.goto(f"{served['url']}/login")
    await page.fill("input[name=username]", "neil")
    await page.fill("input[name=password]", PASSWORD)
    await page.click("button[type=submit]")
    await page.wait_for_selector("#notepad", timeout=40_000)
    if path != "/":
        await page.goto(f"{served['url']}{path}")
    return browser, page, errors


async def _ask_about_the_budget(page) -> None:
    await page.wait_for_selector("#chat-input")
    await page.fill("#chat-input", "What is the marketing budget?")
    await page.press("#chat-input", "Enter")
    await page.locator("#chat-feed .a .cite").first.wait_for(timeout=60_000)


class TestCitationsInTheBrowser:
    async def test_a_citation_opens_the_moment_it_came_from(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                await _ask_about_the_budget(page)
                pill = page.locator("#chat-feed .a .cite").first
                assert await pill.text_content() == "1"
                sources = await page.text_content("#chat-feed .sources")
                assert "capped at forty thousand" in sources
                assert "Finance sync" in sources

                await pill.click()
                card = page.locator(".citecard")
                await card.wait_for()
                text = await card.text_content()
                assert "Finance sync" in text
                assert "Let's look at next quarter." in text, "what led up to it"
                assert "Anything above that needs sign-off." in text, "what came after"

                await card.locator("[data-open]").click()
                cited = page.locator("#pane .live-line.cited")
                await cited.wait_for(timeout=30_000)
                assert "capped at forty thousand" in await cited.text_content()
                assert await page.text_content(".mtitle h2") == "Finance sync"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_citations_can_be_switched_off_and_stay_off(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                await _ask_about_the_budget(page)
                await page.click(".chat-head .switch")
                await page.wait_for_function(
                    "!document.querySelector('#chat-feed .cite')", timeout=10_000
                )
                answer = await page.text_content("#chat-feed .a")
                assert "forty thousand" in answer
                assert "[^1]" not in answer, "markers must not show as raw text"
                assert await page.locator("#chat-feed .sources").count() == 0

                await page.reload()
                await page.wait_for_selector("#chat-feed .a", timeout=40_000)
                assert not await page.is_checked(".chat-head .cite-switch")
                assert await page.locator("#chat-feed .cite").count() == 0
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()


class TestCallAlertsInTheBrowser:
    async def test_a_call_offers_to_record_and_one_click_does(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                _start_call(served)
                card = page.locator(".callcard")
                await card.wait_for(timeout=30_000)
                assert "WhatsApp call detected" in await card.text_content()
                await card.locator('[data-act="record"]').click()
                await page.wait_for_function(
                    "document.querySelector('.mtitle h2')?.textContent === 'WhatsApp call'",
                    timeout=30_000,
                )
                await page.wait_for_selector(".callcard", state="detached", timeout=10_000)
                assert served["app"].state.calls.pending() == []
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_the_notifications_record_link_starts_the_recording(
        self, served
    ) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                _start_call(served)
                calls = served["app"].state.calls
                for _ in range(100):
                    if calls.pending():
                        break
                    await asyncio.sleep(0.1)
                [prompt] = calls.pending()

                await page.goto(f"{served['url']}/?record={prompt.id}")
                await page.wait_for_function(
                    "document.querySelector('.mtitle h2')?.textContent === 'WhatsApp call'",
                    timeout=30_000,
                )
                assert "record=" not in page.url, "a reload must not record again"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_a_link_to_a_call_that_is_not_waiting_records_nothing(
        self, served
    ) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served, "/?record=not-a-real-call")
            try:
                await page.wait_for_function(
                    "document.querySelector('#error')?.textContent.includes('already ended')",
                    timeout=30_000,
                )
                manager = served["app"].state.manager
                assert not manager.recording_locally
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_muting_an_app_hides_the_card_and_is_listed(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                _start_call(served)
                card = page.locator(".callcard")
                await card.wait_for(timeout=30_000)
                await card.locator('[data-act="mute"]').click()
                await page.wait_for_selector(".callcard", state="detached", timeout=10_000)
                await page.click("#call-opts summary")
                await page.wait_for_function(
                    "document.querySelector('#muted-apps')?.textContent.includes('WhatsApp')",
                    timeout=10_000,
                )
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()


class TestLiveTranscriptInTheBrowser:
    async def test_a_live_transcript_listens_and_brings_new_words_in(
        self, served, tmp_path
    ) -> None:
        """What a recording in progress looks like from the transcript tab.

        A meter and "Listening..." at the foot while the meeting runs. A line
        already there when the tab opens is simply shown; a line that arrives
        afterwards comes in word by word, and the foot says "Registering".
        """
        from playwright.async_api import async_playwright

        from meetbot.service.jobs import JobStatus, MeetingJob

        directory = tmp_path / "recordings" / "live-call"
        store = TranscriptStore(directory / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url="Live call", bot_name="Scribe (local capture)"))
        store.append(Utterance("You", "Can everyone hear me?", 0.0, 2.0))
        job = MeetingJob(
            id="live-call",
            meet_url="Live call",
            kind="local",
            title="Live call",
            owner="neil",
            status=JobStatus.IN_CALL,
            created_at=time.time(),
            output_dir=directory,
        )
        served["app"].state.manager._jobs[job.id] = job

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                assert await page.text_content(".mtitle h2") == "Live call"
                await page.click('button[data-tab="transcript"]')
                await page.wait_for_selector("#tx-lines .live-line", timeout=30_000)
                await page.wait_for_selector("#tx-foot .thinking")
                assert "Listening" in await page.text_content("#tx-foot")
                assert await page.locator("#tx-lines .w-in").count() == 0, (
                    "opening a transcript should show it, not perform it"
                )

                # The mark is one glyph that moves rather than a sequence of
                # glyphs, so "is it animating" is "does the transform on the
                # ::before change" - the content is the same turtle all the
                # way through and proves nothing.
                poses = set()
                for _ in range(8):
                    poses.add(await page.evaluate(
                        "getComputedStyle(document.querySelector('#tx-foot .spark'), "
                        "'::before').transform"
                    ))
                    await page.wait_for_timeout(120)
                assert len(poses) > 2, f"the turtle should walk, saw {poses}"

                store.append(Utterance("Speaker 0", "Loud and clear, go ahead.", 3.0, 5.0))
                fresh = page.locator("#tx-lines .live-line").nth(1)
                await fresh.wait_for(timeout=30_000)
                assert "Registering" in await page.text_content("#tx-foot")
                assert await fresh.locator(".w-in").count() == 5
                assert (await fresh.locator(".x").text_content()).strip() == (
                    "Loud and clear, go ahead."
                )
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()
                store.close()
