"""Getting a meeting out of Scribe, driven in a real browser.

Every other test in this suite proves Scribe can produce notes. None of them
proved you could do anything with them: until now the write-up could only be
read in the window that made it, which is a short conversation with anybody
who wants it in a channel or a document.

These tests are in a browser rather than against the routes because there is
almost no server in this feature. The file is assembled in the page out of
what the page already holds, handed to the clipboard or to a download, and
never uploaded anywhere - so the only place the behaviour exists is the
browser, and the only way to prove it is to press the buttons.

Skipped when Playwright or its Chromium build is unavailable.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.service.app import create_app
from meetbot.service.auth import Role
from meetbot.service.jobs import JobStatus, MeetingJob, free_port
from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

pytest.importorskip("playwright.async_api", reason="Playwright is not installed")
uvicorn = pytest.importorskip("uvicorn", reason="uvicorn is not installed")

PASSWORD = "a-long-enough-password"

TYPED = "budget approved?\npriya -> migration fri"
ENHANCED = (
    "## Summary\n\nThe budget was settled and the migration was assigned.\n\n"
    "## Decisions\n\nThe marketing budget is capped at forty thousand.\n\n"
    "## Action items\n\n- [ ] Migration script - Priya (Friday)\n"
)
#: The artifacts on disk open with their own H1, which names the document
#: rather than the meeting. An export has to fold that away.
TRANSCRIPT_MD = (
    "# Meeting Transcript\n\n- recorded_by: Scribe\n\n"
    "**[00:00] Priya:** The marketing budget is capped at forty thousand.\n\n"
    "**[00:20] Sam:** Anything above that needs sign-off.\n"
)
SUMMARY_MD = "# Summary\n\nThe budget was capped and the migration was assigned.\n"


def _headings(body: str) -> list[str]:
    """Every ATX heading in a document, in order."""
    return [line.strip() for line in body.splitlines() if line.startswith("#")]


def _finished(recordings: Path, slug: str, *, full: bool) -> Path:
    """A finished recording on disk. ``full`` gives it every artifact."""
    run_dir = recordings / slug
    run_dir.mkdir(parents=True)
    store = TranscriptStore(run_dir / "transcript.jsonl")
    store.write_meta(MeetingMeta(meet_url=slug, bot_name="Scribe (local capture)"))
    store.append(Utterance("Priya", "The budget is capped at forty thousand.", 0.0, 4.0))
    store.close()
    (run_dir / "notes.md").write_text(TYPED, encoding="utf-8")
    if full:
        (run_dir / "enhanced.md").write_text(ENHANCED, encoding="utf-8")
        (run_dir / "transcript.md").write_text(TRANSCRIPT_MD, encoding="utf-8")
        (run_dir / "analysis.md").write_text(SUMMARY_MD, encoding="utf-8")
    return run_dir


@pytest.fixture
def served(tmp_path: Path):
    """The real app, with one fully written-up meeting and one bare one.

    Two meetings because half of what the sheet has to get right is *not*
    offering a download of something that was never written.
    """
    recordings = tmp_path / "recordings"
    full_dir = _finished(recordings, "20260929-finance", full=True)
    bare_dir = _finished(recordings, "20260928-standup", full=False)

    config = Config(
        meet_url="",
        output_dir=recordings,
        deepgram_api_key="dg-test-key",
        anthropic_api_key="sk-ant-test",
        service_state_dir=tmp_path / "state",
        # Opens a real Deepgram stream and calls the real model.
        preflight_on_start=False,
        # Reads the real microphone and raises real notifications.
        call_alerts=False,
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
    # Starting up, the service restores every directory under output_dir as a
    # job of its own - unowned, untitled and stamped with the directory's
    # mtime. Those would shadow the two below, so they go: this fixture wants
    # meetings with names it chose and an order it controls.
    app.state.manager._jobs.clear()
    now = time.time()
    for job_id, title, run_dir, age in (
        ("bare", "Standup", bare_dir, 7_200),
        ("full", "Finance sync", full_dir, 600),
    ):
        job = MeetingJob(
            id=job_id,
            meet_url="",
            owner="neil",
            status=JobStatus.FINISHED,
            created_at=now - age,
            finished_at=now - age + 300,
            output_dir=run_dir,
        )
        job.kind = "local"
        job.title = title
        job.duration_s = 304.0
        app.state.manager._jobs[job.id] = job

    try:
        yield {"url": f"http://127.0.0.1:{port}", "app": app}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def _open(playwright_api, served):
    """A signed-in page that may touch the clipboard, and the errors it saw."""
    try:
        browser = await playwright_api.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser"
        pytest.skip(f"Chromium is not available: {exc}")
    errors: list[str] = []
    context = await browser.new_context(
        viewport={"width": 1400, "height": 900},
        permissions=["clipboard-read", "clipboard-write"],
    )
    page = await context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    await page.goto(f"{served['url']}/login")
    await page.fill("input[name=username]", "neil")
    await page.fill("input[name=password]", PASSWORD)
    await page.click("button[type=submit]")
    # The newest meeting opens by itself; wait for its name rather than the
    # pane, so the notepad behind it has been fetched too.
    await page.wait_for_selector("#copy", timeout=40_000)
    await page.wait_for_function(
        "document.querySelector('.mtitle h2')"
        " && document.querySelector('.mtitle h2').textContent === 'Finance sync'",
        timeout=40_000,
    )
    # The notes are copied out of the notepad the page is holding, so there is
    # nothing to copy until that has arrived.
    await page.wait_for_function(
        "document.querySelector('#notepad')"
        " && document.querySelector('#notepad').value.includes('priya')",
        timeout=40_000,
    )
    return browser, page, errors


async def _clipboard(page) -> str:
    """What is on the clipboard, with Windows line endings folded away.

    The browser hands CRLF back whatever went in, and the hard line breaks
    these tests assert on are a claim about where the lines end.
    """
    body = await page.evaluate("navigator.clipboard.readText()")
    return body.replace(chr(13) + chr(10), chr(10))


class TestTakingAMeetingWithYou:
    async def test_one_click_puts_the_write_up_on_the_clipboard(self, served) -> None:
        """The whole point: read the notes, press one button, paste them.

        Also pins down what "the write-up" means, which is not obvious when
        there are two sets of notes. The enhanced version leads, because that
        is the one somebody else can read; what was typed during the call
        follows it under its own heading rather than being thrown away.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                await page.click("#copy")
                await page.wait_for_function(
                    "document.querySelector('#copy').textContent.trim() === 'Copied'",
                    timeout=10_000,
                )
                body = await _clipboard(page)

                assert body.startswith("# Finance sync"), body[:120]
                assert "recorded on this machine" in body, "it should say where it came from"
                assert "05:04" in body, "and how long it ran"
                # The write-up is the document, not a section of one. It
                # arrives with "## Summary" on it already, so a "## Notes"
                # above that would be a heading with nothing under it.
                assert "## Notes" not in body, "the write-up is not wrapped"
                assert "## Summary" in body, "the write-up's own heading"
                assert "capped at forty thousand" in body, "the enhanced notes"

                assert "## What I typed" in body
                assert "priya -> migration fri" in body, "the raw notes, kept"
                # Fragments on their own lines, not folded into one paragraph.
                assert "budget approved?  \n" in body, "hard line breaks"

                headings = _headings(body)
                assert headings[0] == "# Finance sync"
                assert len([h for h in headings if h.startswith("# ")]) == 1
                assert len(set(headings)) == len(headings), headings
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_everything_in_one_file_has_no_heading_twice(self, served) -> None:
        """The failure that only appears when the pieces are stacked.

        The write-up has a "## Summary" section because that is what the
        templates produce, and the summary artifact is also a summary - so the
        obvious naming gives one document two "## Summary" headings, and
        anybody skimming it or building a table of contents from it gets
        nonsense. The artifact takes the longer name inside Everything.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                await page.click("#export")
                await page.wait_for_selector('[data-copy="all"]')
                await page.click('[data-copy="all"]')
                await page.wait_for_function(
                    "document.querySelector('[data-copy=all]')"
                    ".textContent.trim() === 'Copied'",
                    timeout=10_000,
                )
                body = await _clipboard(page)

                headings = _headings(body)
                assert len(set(headings)) == len(headings), headings
                assert "## Summary" in headings, "the write-up keeps the plain one"
                assert "## Call summary" in headings, "the artifact takes the other"
                assert "## Transcript" in headings
                # And the artifacts' own H1s are folded away, not stacked.
                assert len([h for h in headings if h.startswith("# ")]) == 1, headings
                assert "the migration was assigned" in body, "the summary itself"
                assert "**[00:00] Priya:**" in body, "the transcript itself"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_the_transcript_downloads_as_a_named_file(self, served) -> None:
        """A download has to be recognisable a week later in a folder.

        And the file it saves has to read as one document: the artifact on
        disk opens with "# Meeting Transcript", which says nothing about
        which meeting, so the export replaces that heading rather than
        stacking a second one on top of it.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                await page.click("#export")
                await page.wait_for_selector('[data-file="transcript"]')

                async with page.expect_download() as caught:
                    await page.click('[data-file="transcript"]')
                download = await caught.value

                name = download.suggested_filename
                assert name.endswith("-finance-sync-transcript.md"), name
                assert name[:4].isdigit(), f"it should sort by date: {name}"

                saved = await download.path()
                body = Path(saved).read_text(encoding="utf-8")
                assert body.startswith("# Finance sync — Transcript"), body[:120]
                assert "# Meeting Transcript" not in body, "one heading, not two"
                assert "**[00:00] Priya:**" in body, "the lines themselves"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_it_offers_only_what_was_actually_written(self, served) -> None:
        """A button that returns a 404 is worse than no button.

        The bare meeting has notes and nothing else - no summary was ever
        written and the transcript was never rendered to Markdown - so those
        two rows must not be there at all.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _open(pw, served)
            try:
                await page.click(".mtg:has-text('Standup')")
                await page.wait_for_function(
                    "document.querySelector('.mtitle h2')"
                    " && document.querySelector('.mtitle h2').textContent === 'Standup'",
                    timeout=20_000,
                )
                await page.click("#export")
                await page.wait_for_selector(".exp-row")

                offered = await page.eval_on_selector_all(
                    ".exp-row [data-copy]", "els => els.map(e => e.dataset.copy)"
                )
                assert offered == ["notes", "all"], offered

                # And the one thing it does offer is the thing that is there.
                await page.click('[data-copy="notes"]')
                await page.wait_for_function(
                    "document.querySelector('[data-copy=notes]')"
                    ".textContent.trim() === 'Copied'",
                    timeout=10_000,
                )
                body = await _clipboard(page)
                assert body.startswith("# Standup"), body[:120]
                assert "## Notes" in body
                assert "## What I typed" not in body, "there is only one set of notes"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()
