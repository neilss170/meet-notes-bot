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
        yield {
            "url": f"http://127.0.0.1:{port}",
            "dir": run_dir,
            "llm": llm,
            "app": app,
        }
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


class TestFindingAndNamingInTheBrowser:
    """Two things a list of meetings has to do once there are more than five."""

    @staticmethod
    def _second_meeting(served) -> None:
        """An older meeting, so the first one stays selected."""
        served["app"].state.manager._jobs["other-meeting"] = MeetingJob(
            id="other-meeting",
            meet_url="Budget planning",
            kind="local",
            title="Budget planning",
            owner="neil",
            status=JobStatus.FINISHED,
            created_at=time.time() - 1200,
            finished_at=time.time() - 1100,
        )

    async def test_the_search_box_narrows_the_list(self, served) -> None:
        from playwright.async_api import async_playwright

        self._second_meeting(served)
        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                # However many the fixture ends up listing - it registers one
                # meeting and the server restores another from disk.
                await page.wait_for_function(
                    "document.querySelectorAll('.mtg').length > 1", timeout=30_000
                )
                listed = await page.locator(".mtg").count()

                await page.fill("#mtg-find", "budget")
                await page.wait_for_function(
                    "document.querySelectorAll('.mtg').length === 1", timeout=10_000
                )
                assert "Budget planning" in await page.text_content("#meetings")

                await page.fill("#mtg-find", "nothing like this")
                await page.wait_for_function(
                    "document.querySelector('#meetings')"
                    ".textContent.includes('Nothing called')",
                    timeout=10_000,
                )

                # Clearing it brings everything back.
                await page.fill("#mtg-find", "")
                await page.wait_for_function(
                    f"document.querySelectorAll('.mtg').length === {listed}",
                    timeout=10_000,
                )
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_renaming_from_the_page_sticks_to_the_transcript(
        self, served
    ) -> None:
        from meetbot.transcript.store import read_title
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click("#m-title")
                await page.fill("#m-title-input", "Roadmap review")
                await page.press("#m-title-input", "Enter")

                await page.wait_for_function(
                    "document.querySelector('.mtg-name')"
                    ".textContent.includes('Roadmap review')",
                    timeout=20_000,
                )
                assert "Roadmap review" in await page.text_content("#m-title")
                # The transcript is the only thing here that outlives the
                # process, so that is where the name has to land.
                assert read_title(served["dir"] / "transcript.jsonl") == "Roadmap review"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_a_half_typed_name_survives_the_poll_loop(self, served) -> None:
        """The notepad bug wearing a different hat: the page rebuilds the
        header every time the elapsed clock ticks, which would take the input
        away mid-word."""
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click("#m-title")
                await page.fill("#m-title-input", "half typed na")
                await page.wait_for_timeout(5_000)
                assert await page.input_value("#m-title-input") == "half typed na"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()


class TestTheButtonsTellYouWhereYouAre:
    """Hover glow is invisible to anybody not holding a mouse."""

    async def test_a_keyboard_user_can_see_which_button_they_are_on(
        self, served
    ) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                resting = await page.evaluate(
                    "getComputedStyle(document.querySelector('#record')).boxShadow"
                )

                # Chromium only applies the :focus-visible heuristic to a
                # page it considers focused, and a headless browser with no
                # compositor behind it never gets focus at all - on a Linux
                # CI runner this reports false even after bring_to_front,
                # and then Tab does not move the selection either. There is
                # nothing here to measure rather than something broken.
                await page.bring_to_front()
                if not await page.evaluate("document.hasFocus()"):
                    pytest.skip(
                        "the page cannot take window focus here, so "
                        ":focus-visible never engages"
                    )

                # Arrive by keyboard: :focus-visible deliberately ignores a
                # click, so clicking would prove nothing.
                await page.focus("#record")
                await page.keyboard.press("Shift+Tab")
                await page.keyboard.press("Tab")
                # The shadow is transitioned, so an immediate read catches the
                # old value half way to the new one.
                await page.wait_for_timeout(500)

                focused = await page.evaluate(
                    """() => {
                      const el = document.querySelector('#record');
                      return {
                        active: document.activeElement === el,
                        page_focused: document.hasFocus(),
                        visible: el.matches(':focus-visible'),
                        shadow: getComputedStyle(el).boxShadow,
                      };
                    }"""
                )
                # Said separately from the assertion below so a failure names
                # which assumption broke rather than only the symptom.
                assert focused["active"] is True, (
                    "Tab did not land back on the record button, so the rest "
                    f"would measure the wrong element: {focused}"
                )
                assert focused["visible"] is True, (
                    f"keyboard focus did not register as visible: {focused}"
                )
                assert focused["shadow"] != resting, "focus looks the same as resting"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_one_accented_action_per_region(self, served) -> None:
        """An accent that means "press this" means nothing when everything
        wears it."""
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                counts = await page.evaluate(
                    """() => {
                      // Only what is on screen: the other mode's form is in
                      // the DOM the whole time, hidden behind the switch.
                      const shown = (root) =>
                        [...document.querySelectorAll(root + ' .btn.primary')]
                          .filter((el) => el.offsetParent !== null).length;
                      return {
                        side: shown('.side'),
                        main: shown('.main'),
                        rail: shown('.chat'),
                      };
                    }"""
                )
                for region, count in counts.items():
                    assert count <= 1, f"{region} has {count} accented buttons"
                assert counts["side"] == 1, "the side panel lost its primary action"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()


class TestArrivingFromTheIcon:
    """The notification area's "Latest notes" names the meeting to open."""

    async def test_a_nudge_moves_the_page_that_is_already_open(self, served) -> None:
        """With a window already open, "Latest notes" should move that page
        rather than open a second copy of the same Scribe."""
        from playwright.async_api import async_playwright

        TestFindingAndNamingInTheBrowser._second_meeting(served)
        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.wait_for_selector(".mtg.on", timeout=30_000)
                # What the icon does: leave the id where the next poll finds it.
                served["app"].state.focus = ("other-meeting", time.time())
                await page.wait_for_function(
                    "document.querySelector('.mtg.on')"
                    "?.textContent.includes('Budget planning')",
                    timeout=30_000,
                )
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_the_page_says_so_when_scribe_quits(self, served) -> None:
        """Quitting closes the window Scribe opened. A tab somebody opened
        themselves cannot be closed by script that did not open it, so that
        one has to say what happened instead of sitting there looking live."""
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                # The server goes away underneath it, as Quit does.
                await page.route("**/api/**", lambda route: route.abort())
                # Waited for as an element, not as page text: this page carries
                # its own script inside <body>, so body.textContent contains
                # every string the code can print before it prints any of them.
                await page.wait_for_selector("#stopped-reload", timeout=30_000)
                assert "Scribe has stopped" in await page.inner_text("#modal-root")
                assert await page.evaluate("state.stopped") is True
            finally:
                await browser.close()

    async def test_a_named_meeting_opens_instead_of_the_newest(self, served) -> None:
        """Without this the link lands on whatever is newest, which is never
        what somebody clicking "Latest notes" after a call has just been
        written up is looking at."""
        from playwright.async_api import async_playwright

        TestFindingAndNamingInTheBrowser._second_meeting(served)
        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.goto(f"{served['url']}/?meeting=other-meeting")
                await page.wait_for_function(
                    "document.querySelector('.mtg.on')"
                    "?.textContent.includes('Budget planning')",
                    timeout=30_000,
                )
                # And the address bar is clean, so a reload does not fight a
                # click on a different meeting.
                assert "meeting=" not in page.url
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()
