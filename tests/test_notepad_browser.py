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
    # Nothing opens by itself any more - the page waits on its home screen -
    # so open the newest meeting the way a person would.
    await page.wait_for_selector(".mtg", timeout=40_000)
    await page.click(".mtg .mtg-time")
    await page.wait_for_selector("#notepad", timeout=40_000)
    return browser, page, errors


async def _open_ask(page, scope: str = "meeting") -> None:
    """Ask, the way a person reaches it from anywhere: Ctrl+J."""
    await page.keyboard.press("Control+j")
    await page.wait_for_selector("#chat-input", timeout=10_000)
    await page.click(f'#ask-scope [data-scope="{scope}"]')


async def _open_transcript(page) -> None:
    """The pill at the foot of a meeting opens its transcript."""
    await page.click("#rec-pill")
    await page.wait_for_selector("#drawer #tx-lines", timeout=10_000)


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
                # Templates live behind "/" in Ask now: pick one, and the
                # notes are rewritten in its shape.
                await _open_ask(page)
                await page.fill("#chat-input", "/stand")
                await page.wait_for_selector("#slash .slash-item.on", timeout=10_000)
                assert "Stand" in await page.text_content("#slash .slash-item.on")
                await page.press("#chat-input", "Enter")
                await page.wait_for_function(
                    "document.querySelector('#enhanced')?.textContent"
                    ".includes('Canned enhancement')",
                    timeout=60_000,
                )
                assert (served["dir"] / "enhanced.md").exists()
                # Task items render as checkboxes rather than raw "- [ ]".
                assert await page.locator("#enhanced .md-task").count() == 1
                # The notes are the spine; enhancing must not consume them.
                await page.keyboard.press("Escape")
                await page.click('#note-mode [data-mode="mine"]')
                assert await page.input_value("#notepad") == typed
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
                await _open_ask(page)
                await page.fill("#chat-input", "Was the budget discussed?")
                await page.click("#chat-go")
                await page.wait_for_function(
                    "document.querySelector('#chat-feed')?.textContent"
                    ".includes('That did not come up')",
                    timeout=60_000,
                )
                body = await page.text_content("#chat-feed")
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
                await _open_transcript(page)
                chip = '#tx-speakers .sb-chip[data-id="Speaker 1"]'
                await page.wait_for_selector(chip)
                await page.click(chip)
                assert await page.locator("#sb-edit").count() == 1, (
                    "the name box should open in one place, not two"
                )
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
                await _open_transcript(page)
                await page.wait_for_selector(".live-line")
                assert "two weeks behind" in await page.text_content("#drawer")
                await page.click("#drawer-close")
                assert await page.input_value("#notepad") == typed
                # Opening the transcript flushes the notes rather than waiting
                # for the debounce, so it is also a save point.
                await page.wait_for_function(
                    "document.querySelector('#savestate')?.textContent === 'Saved'",
                    timeout=10_000,
                )
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

    async def test_a_meeting_is_renamed_from_the_list(self, served) -> None:
        """The list has no edit buttons: click the name, type, press Enter."""
        from meetbot.transcript.store import read_title
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click("#home")
                await page.click(".mtg .mtg-name")
                await page.fill(".mtg-edit", "Planning, renamed")
                await page.press(".mtg-edit", "Enter")
                await page.wait_for_function(
                    "document.querySelector('.mtg-name')"
                    "?.textContent.includes('Planning, renamed')",
                    timeout=20_000,
                )
                assert await page.is_visible("#home-view"), "renaming opened the meeting"
                assert read_title(served["dir"] / "transcript.jsonl") == "Planning, renamed"
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
                # Not Start recording: it is rightly disabled on a machine with
                # no audio devices, and a disabled button cannot take focus.
                await page.click("#home")
                await page.wait_for_selector("#bot-toggle")
                resting = await page.evaluate(
                    "(() => { const s = getComputedStyle(document.querySelector('#bot-toggle'));"
                    " return s.boxShadow + '|' + s.outlineStyle; })()"
                )

                # Chromium only applies the :focus-visible heuristic to a
                # page it considers focused. bring_to_front wins that for a
                # moment, but a headless browser with no compositor behind
                # it does not keep it - which is why the check for it is
                # down at the read rather than up here, where it passed and
                # proved nothing.
                await page.bring_to_front()

                # Arrive by keyboard: :focus-visible deliberately ignores a
                # click, so clicking would prove nothing.
                await page.focus("#bot-toggle")
                await page.keyboard.press("Shift+Tab")
                await page.keyboard.press("Tab")
                # The shadow is transitioned, so an immediate read catches the
                # old value half way to the new one.
                await page.wait_for_timeout(500)

                focused = await page.evaluate(
                    """() => {
                      const el = document.querySelector('#bot-toggle');
                      return {
                        active: document.activeElement === el,
                        page_focused: document.hasFocus(),
                        visible: el.matches(':focus-visible'),
                        shadow: getComputedStyle(el).boxShadow + '|' +
                          getComputedStyle(el).outlineStyle,
                      };
                    }"""
                )
                if not focused["page_focused"]:
                    # No window focus means no :focus-visible and no Tab
                    # navigation either, so there is nothing on this runner
                    # to measure rather than something broken. It still runs
                    # everywhere the product is used.
                    pytest.skip(
                        "the page cannot hold window focus here, so "
                        f":focus-visible never engages: {focused}"
                    )

                # Said separately from the assertion below so a failure names
                # which assumption broke rather than only the symptom.
                assert focused["active"] is True, (
                    "Tab did not land back on the bot link, so the rest "
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
                count = """() => {
                  // Only what is on screen: the hidden view is in the DOM too.
                  const shown = (root) =>
                    [...document.querySelectorAll(root + ' .btn.primary')]
                      .filter((el) => el.offsetParent !== null).length;
                  return {
                    side: shown('.side'), home: shown('#home-view'),
                    meeting: shown('#meeting-view'), ask: shown('#ask'),
                  };
                }"""
                in_meeting = await page.evaluate(count)
                await _open_ask(page)
                with_ask = await page.evaluate(count)
                await page.keyboard.press("Escape")
                await page.click("#home")
                await page.wait_for_selector("#record")
                at_home = await page.evaluate(count)
                for counts in (in_meeting, with_ask, at_home):
                    for region, n in counts.items():
                        assert n <= 1, f"{region} has {n} accented buttons: {counts}"
                assert at_home["home"] == 1, "home lost its one action: Start recording"
                assert in_meeting["meeting"] == 1, "a meeting with no write-up offers one"
                assert with_ask["ask"] == 1, "Ask has one action: Ask"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()


class TestTheHomeScreen:
    """Opening Scribe shows nothing in particular until you ask for something.

    It used to open the newest meeting by itself, so sitting idle meant
    looking at the last call's notes. Now the page waits on its home screen;
    a meeting opens when it is clicked or when a recording starts.
    """

    async def test_signing_in_opens_no_meeting(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium is not available: {exc}")
            page = await (await browser.new_context()).new_page()
            try:
                await page.goto(f"{served['url']}/login")
                await page.fill("input[name=username]", "neil")
                await page.fill("input[name=password]", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_selector(".mtg", timeout=40_000)
                # Two polls' worth: long enough for the old behaviour to
                # have opened the newest meeting.
                await page.wait_for_timeout(4500)
                assert await page.is_visible("#home-view")
                assert await page.locator("#notepad").count() == 0
                assert await page.locator(".mtg.on").count() == 0

                await page.click(".mtg .mtg-time")
                await page.wait_for_selector("#notepad", timeout=20_000)
                assert await page.is_hidden("#home-view")
                await page.click("#home")
                await page.wait_for_selector("#home-view", timeout=10_000)
                await page.wait_for_timeout(2500)
                assert await page.locator("#notepad").count() == 0, (
                    "the next poll reopened a meeting after going home"
                )
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
                await page.wait_for_selector(".mtg.on", state="attached", timeout=30_000)
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


class TestTheWriteUp:
    """After the call: your words in ink, the model's in grey, and a line
    you rewrite becomes yours."""

    async def test_your_words_are_ink_and_an_edited_line_becomes_yours(self, served) -> None:
        from playwright.async_api import async_playwright

        from meetbot.transcript.notes import Notepad

        pad = Notepad(served["dir"])
        pad.write_notes("budget approved?")
        pad.write_enhanced("## Summary\n\n- budget approved?\n- The model added this line.\n")
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium is not available: {exc}")
            errors: list[str] = []
            page = await (await browser.new_context()).new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            try:
                await page.goto(f"{served['url']}/login")
                await page.fill("input[name=username]", "neil")
                await page.fill("input[name=password]", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_selector(".mtg", timeout=40_000)
                await page.click(".mtg .mtg-time")
                await page.wait_for_selector("#enhanced li", timeout=30_000)

                lines = page.locator("#enhanced li")
                assert "mine" in (await lines.nth(0).get_attribute("class") or "")
                assert "mine" not in (await lines.nth(1).get_attribute("class") or "")
                ink = "el => getComputedStyle(el).color"
                assert await lines.nth(0).evaluate(ink) != await lines.nth(1).evaluate(ink), (
                    "your line and the model's look the same"
                )

                await lines.nth(1).click()
                await page.keyboard.press("Control+a")
                await page.keyboard.type("Finance signs off anything over 40k.")
                await page.keyboard.press("Escape")
                # Escape cancels; nothing changed on disk.
                assert "Finance signs off" not in pad.read_enhanced()

                await lines.nth(1).click()
                await page.keyboard.press("Control+a")
                await page.keyboard.type("Finance signs off anything over 40k.")
                await page.locator("#enhanced li").nth(1).evaluate("el => el.blur()")
                await page.wait_for_function(
                    "document.querySelector('#savestate')?.textContent === 'Saved'",
                    timeout=10_000,
                )
                assert "- Finance signs off anything over 40k." in pad.read_enhanced()
                assert pad.edited_lines == ["- Finance signs off anything over 40k."]
                edited = page.locator("#enhanced li").nth(1)
                assert "mine" in (await edited.get_attribute("class") or "")
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()


class TestStoppingByItself:
    async def test_the_countdown_shows_and_keep_recording_cancels_it(
        self, served, tmp_path
    ) -> None:
        from playwright.async_api import async_playwright

        directory = tmp_path / "recordings" / "live-call"
        store = TranscriptStore(directory / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url="Live call", bot_name="Scribe (local capture)"))
        store.close()
        job = MeetingJob(
            id="live-call", meet_url="Live call", kind="local", title="Live call",
            owner="neil", status=JobStatus.IN_CALL, created_at=time.time(),
            output_dir=directory,
        )
        auto = served["app"].state.auto_stop
        auto.countdown_s = 600  # long enough that nothing really stops here
        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                # Registered once the page is open, so it does not open itself
                # before the helper has opened the meeting it expects.
                served["app"].state.manager._jobs[job.id] = job
                auto.slept("live-call")
                toast = page.locator('#toast-root [data-key="auto-stop"]')
                await toast.wait_for(timeout=30_000)
                text = await toast.text_content()
                assert "Stopping" in text and "asleep" in text
                await toast.locator("button").click()
                await page.wait_for_selector(
                    '#toast-root [data-key="auto-stop"]', state="detached", timeout=10_000
                )
                assert auto.pending("live-call") is None
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()
