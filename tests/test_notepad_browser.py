"""Notepad tests that need a real browser.

The API tests in ``test_service.py`` prove the routes. They cannot prove the
thing that actually breaks a notepad: that the two-second poll loop leaves
what you are typing alone. That bug is invisible to any test which does not
run the page's own JavaScript on a real timer - an earlier version rebuilt the
pane whenever the elapsed clock ticked, which recreated the textarea and
deleted the sentence in progress.

So this module serves the real app and drives the real page. It skips itself
if Playwright or its Chromium build is unavailable, so a checkout without
browsers still runs the rest of the suite green. Nothing here reaches the
network: the LLM adapter is replaced with a canned one.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from meetbot.config import Config
from meetbot.service import app as app_module
from meetbot.service.app import create_app
from meetbot.service.auth import Role
from meetbot.service.jobs import JobStatus, MeetingJob, free_port
from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

pytest.importorskip("playwright.async_api", reason="Playwright is not installed")
uvicorn = pytest.importorskip("uvicorn", reason="uvicorn is not installed")

PASSWORD = "a-long-enough-password"
MEET_URL = "https://meet.google.com/abc-defg-hij"

ENHANCED = (
    "## Summary\n\nCanned enhancement produced from the notes.\n\n"
    "## Action items\n\n- [ ] Write the migration script - Priya (Friday)\n"
)


class _CannedLLM:
    """Stands in for a provider adapter. Records the prompts it was given."""

    model = "canned-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete_text(self, *, system: str, user: str, max_tokens: int = 16_000) -> str:
        self.prompts.append(user)
        if "QUESTION" in user:
            return "That did not come up."
        return ENHANCED

    def complete_json(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


@pytest.fixture
def served(tmp_path: Path, monkeypatch):
    """The real app on a real port, with one finished meeting on disk."""
    recordings = tmp_path / "recordings"
    run_dir = recordings / "run-abc-defg-hij"
    run_dir.mkdir(parents=True)
    store = TranscriptStore(run_dir / "transcript.jsonl")
    store.write_meta(MeetingMeta(meet_url=MEET_URL, bot_name="Scribe"))
    store.append(Utterance("Speaker 0", "We are two weeks behind.", 0.0, 3.0))
    store.append(Utterance("Speaker 1", "I'll take the migration script.", 4.0, 7.0))
    store.close()

    llm = _CannedLLM()
    monkeypatch.setattr(app_module, "build_client", lambda *a, **k: llm)

    config = Config(
        meet_url="",
        output_dir=recordings,
        deepgram_api_key="dg-test-key",
        anthropic_api_key="sk-ant-test",
        service_state_dir=tmp_path / "state",
        # The startup checks open a real Deepgram stream and call the LLM.
        preflight_on_start=False,
        # Reads the real microphone state and raises real notifications.
        call_alerts=False,
        # This fixture's meeting is already finished, and these tests are about
        # the notepad rather than the automatic write-up.
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
    job = MeetingJob(
        id="browser-test",
        meet_url=MEET_URL,
        owner="neil",
        status=JobStatus.FINISHED,
        created_at=time.time() - 600,
        finished_at=time.time() - 300,
        output_dir=run_dir,
    )
    job.duration_s = 7.0
    app.state.manager._jobs[job.id] = job

    try:
        yield {"url": f"http://127.0.0.1:{port}", "dir": run_dir, "llm": llm}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def _signed_in_page(playwright_api, served):
    """A browser page with the notepad open, and the console errors it saw."""
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
    return browser, page, errors


class TestNotepadInTheBrowser:
    async def test_typing_survives_the_poll_loop(self, served) -> None:
        """The regression this whole module exists for.

        The page polls every two seconds. Five seconds of holding still is
        more than two full cycles, so a poll that rebuilt the pane or refilled
        the textarea would have eaten the text by the time this asserts.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                typed = "api behind 2wks\npriya -> migration script fri"
                await page.fill("#notepad", typed)
                await page.wait_for_timeout(5_000)
                assert await page.input_value("#notepad") == typed
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_notes_autosave_without_being_asked(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.fill("#notepad", "budget approved?")
                await page.wait_for_function(
                    "document.querySelector('#savestate')?.textContent === 'Saved'",
                    timeout=30_000,
                )
                notes = served["dir"] / "notes.md"
                assert notes.read_text(encoding="utf-8") == "budget approved?"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_enhance_paints_its_result_and_keeps_the_original(
        self, served
    ) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                typed = "api behind\npriya -> script"
                await page.fill("#notepad", typed)
                await page.select_option("#template", "standup")
                await page.click("#enhance")
                await page.wait_for_function(
                    "document.querySelector('#enhanced')?.textContent"
                    ".includes('Canned enhancement')",
                    timeout=60_000,
                )
                # The notes are the spine; enhancing must not consume them.
                assert await page.input_value("#notepad") == typed
                assert (served["dir"] / "enhanced.md").exists()
                # Task items render as checkboxes rather than raw "- [ ]".
                assert await page.locator("#enhanced .md-task").count() == 1
                # The chosen template actually shaped the request.
                assert "by person" in served["llm"].prompts[-1].lower()
                assert typed in served["llm"].prompts[-1]
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_asking_a_question_renders_the_answer(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('button[data-tab="ask"]')
                await page.wait_for_selector("#ask-input")
                await page.fill("#ask-input", "Was the budget discussed?")
                await page.click("#ask-go")
                await page.wait_for_function(
                    "document.body.textContent.includes('That did not come up')",
                    timeout=60_000,
                )
                body = await page.text_content(".askwrap")
                assert "Was the budget discussed?" in body
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_naming_a_voice_relabels_the_transcript(self, served) -> None:
        """Diarization gives labels; one person who was there gives names."""
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('button[data-tab="transcript"]')
                await page.wait_for_selector('.sb-chip[data-id="Speaker 1"]')
                await page.click('.sb-chip[data-id="Speaker 1"]')
                await page.fill("#sb-edit", "Priya")
                await page.press("#sb-edit", "Enter")

                await page.wait_for_function(
                    "document.querySelector('#tx-lines')"
                    ".textContent.includes('Priya')",
                    timeout=30_000,
                )
                lines = await page.text_content("#tx-lines")
                assert "Speaker 1" not in lines, "the old label is still shown"
                assert "Speaker 0" in lines, "the other voice was renamed too"
                # Recorded as an event; the utterances themselves are untouched.
                raw = (served["dir"] / "transcript.jsonl").read_text(encoding="utf-8")
                assert "speaker_names" in raw
                assert '"Speaker 1"' in raw
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_switching_tabs_loses_nothing(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                typed = "keep me"
                await page.fill("#notepad", typed)
                await page.click('button[data-tab="transcript"]')
                await page.wait_for_selector(".live-line")
                assert "two weeks behind" in await page.text_content("#pane")
                await page.click('button[data-tab="notes"]')
                await page.wait_for_selector("#notepad")
                assert await page.input_value("#notepad") == typed
                # Leaving the notepad flushes it rather than waiting for the
                # debounce, so the tab switch is also a save point.
                assert (served["dir"] / "notes.md").read_text(
                    encoding="utf-8"
                ) == typed
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()
