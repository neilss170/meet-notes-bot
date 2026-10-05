"""The Accounts modal's password control, driven in a real browser.

``test_service.py`` proves ``POST /api/users/{name}/password`` changes a
password, and ``test_auth.py`` proves the hash underneath it. Neither can
prove the part that is easy to get wrong here: the modal rebuilds itself from
scratch on every change, so the editor has to survive being drawn, a rejected
password has to leave what was typed alone instead of being wiped by a
redraw, and a successful change has to say so - the account list looks
identical afterwards, so without a word on screen the Save button appears to
do nothing at all.

The last test is the one that matters most: it signs out and signs back in
with the new password, which is the only assertion that distinguishes "the
page sent the request" from "the password actually changed".

Skips itself if Playwright or its Chromium build is unavailable, so a
checkout without browsers still runs the rest of the suite green. Nothing
here reaches the network.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.service.app import create_app
from meetbot.service.auth import Role
from meetbot.service.jobs import free_port

pytest.importorskip("playwright.async_api", reason="Playwright is not installed")
uvicorn = pytest.importorskip("uvicorn", reason="uvicorn is not installed")

PASSWORD = "a-long-enough-password"
NEW_PASSWORD = "an-even-better-password"


@pytest.fixture
def served(tmp_path: Path):
    """The real app on a real port, with an admin and one member account."""
    config = Config(
        meet_url="",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg-test-key",
        anthropic_api_key="sk-ant-test",
        service_state_dir=tmp_path / "state",
        # The startup checks open a real Deepgram stream and call the LLM.
        preflight_on_start=False,
        # Reads the real microphone state and raises real notifications.
        call_alerts=False,
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
    app.state.users.add("priya", PASSWORD, Role.MEMBER)

    try:
        yield {"url": f"http://127.0.0.1:{port}", "app": app}
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def _signed_in_page(playwright_api, served, username: str = "neil"):
    """A browser page with the Accounts modal open, and its console errors."""
    try:
        browser = await playwright_api.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser"
        pytest.skip(f"Chromium is not available: {exc}")
    errors: list[str] = []
    context = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )
    await _sign_in(page, served, username, PASSWORD)
    await page.click("#admin-open")
    await page.wait_for_selector(".user-row", timeout=30_000)
    return browser, page, errors


async def _sign_in(page, served, username: str, password: str) -> None:
    await page.goto(f"{served['url']}/login")
    await page.fill("input[name=username]", username)
    await page.fill("input[name=password]", password)
    await page.click("button[type=submit]")


class TestChangingAPasswordFromTheModal:
    async def test_the_editor_opens_on_the_row_it_belongs_to(self, served) -> None:
        """One editor, under the account whose button was pressed.

        The rows are rebuilt from a list, so an editor keyed on anything but
        the username would open under the wrong account - or under all of
        them at once.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                assert await page.locator(".pwrow").count() == 0
                await page.click('[data-pw="priya"]')
                await page.wait_for_selector(".pwrow", timeout=10_000)
                assert await page.locator(".pwrow").count() == 1

                # The editor sits immediately after priya's row, not neil's.
                owner = await page.evaluate(
                    "document.querySelector('.pwrow')"
                    ".previousElementSibling.querySelector('strong').textContent"
                )
                assert owner == "priya"
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_a_second_press_puts_the_editor_away(self, served) -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('[data-pw="priya"]')
                await page.wait_for_selector(".pwrow", timeout=10_000)
                await page.click('[data-pw="priya"]')
                await page.wait_for_function(
                    "document.querySelectorAll('.pwrow').length === 0",
                    timeout=10_000,
                )
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

    async def test_a_saved_password_is_the_one_that_works(self, served) -> None:
        """The assertion the rest of this module exists to support.

        Signing back in is the only check that separates a page which sent
        the request from a password which actually changed.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('[data-pw="priya"]')
                await page.fill("#pw-new", NEW_PASSWORD)
                await page.click("#pw-save")

                # Said out loud, because the list looks the same either way.
                await page.wait_for_function(
                    "!document.querySelector('#admin-note')?.hidden",
                    timeout=20_000,
                )
                note = await page.text_content("#admin-note")
                assert "priya" in note

                # And the editor is gone once the change landed.
                assert await page.locator(".pwrow").count() == 0
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

        assert served["app"].state.users.authenticate("priya", NEW_PASSWORD)
        assert served["app"].state.users.authenticate("priya", PASSWORD) is None

        # Proven through the front door as well: the login form takes it.
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium is not available: {exc}")
            try:
                page = await browser.new_page()
                await _sign_in(page, served, "priya", NEW_PASSWORD)
                # The app shell, not the Accounts button: priya is a member,
                # and that button is deliberately hidden for members.
                await page.wait_for_selector(".brand", timeout=40_000)
            finally:
                await browser.close()

    async def test_changing_your_own_password_keeps_you_signed_in(
        self, served
    ) -> None:
        """Sessions are signed over the username, not the password.

        So the admin doing this to their own account should not be thrown out
        mid-change. Worth pinning down: the opposite behaviour would be a
        reasonable design, and this records which one is true.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('[data-pw="neil"]')
                await page.fill("#pw-new", NEW_PASSWORD)
                await page.click("#pw-save")
                await page.wait_for_function(
                    "!document.querySelector('#admin-note')?.hidden",
                    timeout=20_000,
                )
                # Still on the page, and still able to make admin calls -
                # asserted on who is listed rather than how many, because an
                # empty store also gets a bootstrap admin of its own.
                listed = await page.eval_on_selector_all(
                    ".user-row strong", "els => els.map((e) => e.textContent)"
                )
                assert {"neil", "priya"} <= set(listed)
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()

        assert served["app"].state.users.authenticate("neil", NEW_PASSWORD)

    async def test_a_rejected_password_keeps_what_was_typed(self, served) -> None:
        """The redraw-wipes-the-field bug, in the one case that triggers it.

        The server refuses anything under eight characters. Showing that
        error by rebuilding the modal would clear the field and lose the
        account being edited, so the error path deliberately does not redraw.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('[data-pw="priya"]')
                await page.fill("#pw-new", "short")
                await page.click("#pw-save")

                await page.wait_for_function(
                    "!document.querySelector('#admin-error')?.hidden",
                    timeout=20_000,
                )
                message = await page.text_content("#admin-error")
                assert "8 characters" in message

                # The field and the editor are both still there.
                assert await page.input_value("#pw-new") == "short"
                assert await page.locator(".pwrow").count() == 1

                # And nothing changed underneath.
                assert served["app"].state.users.authenticate("priya", PASSWORD)

                # Correcting it in place works, without reopening anything.
                await page.fill("#pw-new", NEW_PASSWORD)
                await page.click("#pw-save")
                await page.wait_for_function(
                    "!document.querySelector('#admin-note')?.hidden",
                    timeout=20_000,
                )
                # The stale error goes when the retry succeeds, or the modal
                # would show a refusal and a confirmation side by side.
                assert await page.locator("#admin-error").is_hidden()

                # The refusal is this test's subject, and Chromium logs every
                # non-2xx response to the console, so that one 400 is not a
                # page error. Anything else still is.
                unrelated = [e for e in errors if "400" not in e]
                assert not unrelated, f"the page logged errors: {unrelated}"
            finally:
                await browser.close()

        assert served["app"].state.users.authenticate("priya", NEW_PASSWORD)

    async def test_the_password_is_never_put_in_the_address_bar(
        self, served
    ) -> None:
        """A GET form would leave the new password in history and in logs."""
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser, page, errors = await _signed_in_page(pw, served)
            try:
                await page.click('[data-pw="priya"]')
                field_type = await page.get_attribute("#pw-new", "type")
                assert field_type == "password", "the new password was on screen"

                await page.fill("#pw-new", NEW_PASSWORD)
                await page.click("#pw-save")
                await page.wait_for_function(
                    "!document.querySelector('#admin-note')?.hidden",
                    timeout=20_000,
                )
                assert NEW_PASSWORD not in page.url
                assert not errors, f"the page logged errors: {errors}"
            finally:
                await browser.close()
