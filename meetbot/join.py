"""Playwright-driven Google Meet guest join flow.

Everything here targets the *guest* path: no Google account, no stored
credentials, just a display name on the pre-join screen. That is both a
reliability decision (automated logins trip Google's security checks) and a
security one (there are no credentials to store or leak).

Every Meet selector used by this module lives in :mod:`meetbot.selectors`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Sequence

from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from meetbot import selectors
from meetbot.capture import load_inject_script
from meetbot.config import Config, validate_meet_url
from meetbot.profile import clone_profile, discard_profile

logger = logging.getLogger(__name__)
browser_logger = logging.getLogger("meetbot.browser")

#: Chromium flags needed for unattended WebRTC capture.
#:
#: * fake UI/device: getUserMedia resolves without a hardware mic or a
#:   permission prompt (we mute both tracks before joining regardless).
#: * autoplay policy: lets the capture AudioContext start without a gesture.
#: * mute-audio: silences local playback only - our tap sits upstream of the
#:   output device, so captured audio is unaffected.
_CHROMIUM_ARGS: tuple[str, ...] = (
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    # Chrome (~142+) blocks a public-origin page like meet.google.com from
    # opening a raw socket to a private address (our loopback bridge) behind
    # a "Local Network Access" permission prompt that never appears in an
    # automated context - every capture-socket connection just fails with
    # net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS. Verified fix, tested
    # against this exact block: disable the feature outright.
    "--disable-features=LocalNetworkAccessChecks",
)

#: How often to re-check the meeting state while the call is running.
_POLL_INTERVAL_S = 5.0

#: Per-selector probe timeout. Deliberately short: these run in a loop over a
#: list of candidates and a slow probe multiplies across every fallback.
_PROBE_TIMEOUT_MS = 1500

#: How long a single candidate is given inside one polling round. Short
#: enough that an absent selector does not eat the whole budget, long enough
#: that a match still lands on the first round in practice.
_PROBE_SLICE_MS = 250.0

#: Budget for the "why did the join fail" checks. These run only after the
#: join-button search has already given up, so nobody is waiting on a fast
#: answer - and at the 400ms default, shared across candidates, the refusal
#: text lost the race and Meet's own "You can't join this video call" was
#: reported as a broken selector.
TERMINAL_STATE_TIMEOUT_MS: Final[int] = 3_000

#: How long to wait for Meet to actually call getDisplayMedia after the share
#: controls have been clicked, before giving up and reporting the overlay as
#: not started.
_OVERLAY_SHARE_TIMEOUT_S = 6.0
#: Short first wait: if Meet skips its own source menu the request lands
#: almost immediately, and we should not click a menu that is not there.
_OVERLAY_MENU_PROBE_S = 1.0

#: The guest-name field only exists on an anonymous join. A signed-in browser
#: never shows one, so this is a short bet, not a long wait.
_NAME_FIELD_TIMEOUT_MS = 4000

#: Captions can take a moment to appear in the controls after admission, so
#: the toggle is retried rather than attempted once.
_CAPTION_TOGGLE_ATTEMPTS = 4


class JoinOutcome(str, Enum):
    """Why :meth:`MeetSession.join` finished the way it did."""

    ADMITTED = "admitted"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    MEETING_ENDED = "meeting_ended"


class EndReason(str, Enum):
    """Why the bot stopped monitoring a call it had joined."""

    MEETING_ENDED = "meeting_ended"
    REMOVED = "removed"
    ALONE = "alone"
    MAX_DURATION = "max_duration"
    STOPPED = "stopped"
    BROWSER_LOST = "browser_lost"


class JoinError(RuntimeError):
    """Raised when the bot could not get into the meeting."""

    def __init__(self, outcome: JoinOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass
class MeetingState:
    """A point-in-time reading of the call, from the meeting watchdog."""

    in_call: bool
    participant_count: int | None
    ended: bool


async def first_visible(
    page: Page, candidates: Sequence[str], *, timeout_ms: int = _PROBE_TIMEOUT_MS
) -> Locator | None:
    """Return the first visible locator among ``candidates``, or ``None``.

    ``timeout_ms`` is the budget for the whole search, not for each candidate.
    That distinction is worth stating because getting it wrong is expensive:
    waiting the full timeout on every candidate in turn made a signed-in join
    spend 40 seconds looking for a guest-name field that could never appear
    (4 candidates x 10s), and gave the join button a 75-second worst case.
    Measured end to end, a join took ~52 seconds before the bot even knocked.

    Candidates are polled in rounds, in order, with a short slice each, so the
    preference ordering in :mod:`meetbot.selectors` still decides who wins
    while a genuinely absent element costs the budget once rather than once
    per candidate. Missing selectors are an expected outcome, not an error -
    Meet's DOM differs by meeting type, account state and rollout.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    broken: set[str] = set()
    while True:
        for selector in candidates:
            if selector in broken:
                continue
            remaining_ms = (deadline - time.monotonic()) * 1000.0
            if remaining_ms <= 0:
                return None
            try:
                locator = page.locator(selector).first
                await locator.wait_for(
                    state="visible",
                    timeout=max(50.0, min(_PROBE_SLICE_MS, remaining_ms)),
                )
                logger.debug("Matched selector %r", selector)
                return locator
            except PlaywrightTimeoutError:
                continue
            except PlaywrightError as exc:
                # A malformed or unsupported selector should be loud - it is a
                # bug in selectors.py, not a transient page state. Recorded so
                # it is reported once rather than every round.
                logger.warning("Selector %r could not be evaluated: %s", selector, exc)
                broken.add(selector)
                continue
        if time.monotonic() >= deadline:
            return None


async def any_visible(
    page: Page, candidates: Sequence[str], *, timeout_ms: int = 400
) -> bool:
    """Whether any of ``candidates`` is currently visible."""
    return await first_visible(page, candidates, timeout_ms=timeout_ms) is not None


class MeetSession:
    """Owns the browser, the Meet page, and the meeting lifecycle.

    Usage::

        async with MeetSession(config) as session:
            await session.join()
            reason = await session.wait_until_meeting_ends()
    """

    def __init__(
        self,
        config: Config,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """Create a session. No browser is launched until :meth:`start`.

        Args:
            config: Runtime configuration.
            on_event: Optional sink for lifecycle events, used by the CLI to
                write them into the transcript alongside the utterances.
        """
        self._config = config
        self._on_event = on_event
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._stop_requested = asyncio.Event()
        self._joined_at: float | None = None
        #: Private copy of the signed-in profile, removed in close().
        self._profile_clone: Path | None = None

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "MeetSession":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _emit(self, kind: str, **details: Any) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, details)
            except Exception:
                logger.exception("Lifecycle event sink raised for event %r", kind)

    @property
    def page(self) -> Page:
        """The Meet page. Raises if :meth:`start` has not run."""
        if self._page is None:
            raise RuntimeError("MeetSession.start() has not been called")
        return self._page

    async def start(self) -> None:
        """Launch Chromium, install the capture hook, and open the Meet URL."""
        url = validate_meet_url(self._config.meet_url)

        logger.info(
            "Launching Chromium (headless=%s, channel=%s)",
            self._config.headless,
            self._config.browser_channel or "bundled",
        )
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self._config.headless,
            "args": list(_CHROMIUM_ARGS),
        }
        if self._config.browser_channel:
            launch_kwargs["channel"] = self._config.browser_channel

        context_kwargs: dict[str, Any] = {
            "permissions": ["microphone", "camera"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "UTC",
        }

        if self._config.chrome_profile_dir is not None:
            # A persistent profile keeps a signed-in Google session between
            # runs. Meet increasingly refuses anonymous guests outright -
            # replacing the pre-join screen with "You can't join this video
            # call", without ever asking the host - and no selector can work
            # around that. Signing in is the fix.
            #
            # launch_persistent_context owns both the browser and the context,
            # so there is no separate browser object to track here.
            master = self._config.chrome_profile_dir.expanduser()
            master.mkdir(parents=True, exist_ok=True)
            # Chromium cannot share one profile directory between two live
            # browsers, and gives the second a blank one rather than an
            # error - so a second concurrent meeting would join signed-out.
            # Each run works on its own copy; see meetbot.profile.
            self._profile_clone = await asyncio.to_thread(clone_profile, master)
            logger.info(
                "Using a private copy of the signed-in Chromium profile at %s", master
            )
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._profile_clone), **launch_kwargs, **context_kwargs
            )
            self._browser = None
        else:
            logger.info(
                "No CHROME_PROFILE_DIR set: joining as an anonymous guest. "
                "Meetings that do not admit anonymous guests will refuse this "
                "bot outright - run 'python -m meetbot login' to sign in."
            )
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(**context_kwargs)
        # Grant explicitly for the Meet origin too: Chromium's fake-UI flag
        # covers the prompt, but Meet also queries the Permissions API.
        with contextlib.suppress(PlaywrightError):
            await self._context.grant_permissions(
                ["microphone", "camera"], origin="https://meet.google.com"
            )

        await self._install_capture_hook()

        self._page = await self._context.new_page()
        self._page.on("console", self._forward_console)
        self._page.on(
            "pageerror",
            lambda err: browser_logger.warning("Uncaught page error: %s", err),
        )

        logger.info("Opening %s", url)
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            raise JoinError(
                JoinOutcome.TIMED_OUT,
                f"Timed out loading {url} after 60s: {exc}",
            ) from exc

    async def _install_capture_hook(self) -> None:
        """Add the config and capture init scripts to the browser context.

        Order matters: the config script must run first so ``inject.js`` sees
        ``window.__MEETBOT_CONFIG__`` when it initialises.
        """
        assert self._context is not None
        hook_config = {
            "bridgeUrl": self._config.bridge_url,
            "sampleRate": self._config.sample_rate,
            "perParticipant": self._config.per_participant_audio,
        }
        await self._context.add_init_script(
            f"window.__MEETBOT_CONFIG__ = {json.dumps(hook_config)};"
        )
        await self._context.add_init_script(load_inject_script())
        logger.debug("Capture hook installed with config %s", hook_config)

    def _forward_console(self, message: ConsoleMessage) -> None:
        """Re-emit structured browser logs through Python's logging."""
        text = message.text
        if not text.startswith("[meetbot]"):
            if message.type in ("error",):
                browser_logger.debug("console error: %s", text)
            return
        payload_text = text[len("[meetbot]") :].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            browser_logger.info("%s", payload_text)
            return
        level = {
            "error": logging.ERROR,
            "warn": logging.WARNING,
            "info": logging.INFO,
        }.get(str(payload.get("level")), logging.DEBUG)
        extras = {
            k: v for k, v in payload.items() if k not in ("tag", "level", "message")
        }
        browser_logger.log(
            level, "%s%s", payload.get("message"), f" {extras}" if extras else ""
        )

    async def close(self) -> None:
        """Tear down the browser and Playwright, tolerating partial startup."""
        if self._page is not None and not self._page.is_closed():
            with contextlib.suppress(PlaywrightError):
                await self._page.evaluate(
                    "() => window.__meetbot && window.__meetbot.stop()"
                )
        for closer, name in (
            (self._context, "context"),
            (self._browser, "browser"),
        ):
            if closer is None:
                continue
            try:
                await closer.close()
            except PlaywrightError as exc:
                logger.warning("Error closing %s: %s", name, exc)
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        # After the browser has released it, not before.
        discard_profile(self._profile_clone)
        self._profile_clone = None
        logger.info("Browser closed")

    def request_stop(self) -> None:
        """Ask :meth:`wait_until_meeting_ends` to return at the next poll."""
        self._stop_requested.set()

    # -- join flow ---------------------------------------------------------

    async def join(self) -> JoinOutcome:
        """Run the full guest join flow.

        Dismisses interstitials, sets the disclosing display name, mutes mic
        and camera, clicks join, then waits for admission.

        Returns:
            :attr:`JoinOutcome.ADMITTED` on success.

        Raises:
            JoinError: If the host denied entry, the meeting had already ended,
                or admission did not arrive within ``admission_timeout_s``.
        """
        page = self.page
        await self._dismiss_interstitials()
        await self._set_display_name()
        await self._disable_devices()

        join_button = await first_visible(page, selectors.JOIN_BUTTON, timeout_ms=15_000)
        if join_button is None:
            if await any_visible(
                page, selectors.IN_CALL_MARKERS, timeout_ms=TERMINAL_STATE_TIMEOUT_MS
            ):
                logger.info("Already in the call; no join button needed")
                self._joined_at = time.monotonic()
                self._emit("joined", method="direct")
                return JoinOutcome.ADMITTED
            # A missing join button usually is not a broken selector: far more
            # often Meet has refused this bot outright and replaced the
            # pre-join screen with a rejection page, which HAS no join button
            # to find. Reporting a DOM change there sends the operator off to
            # fix selectors that are fine, so the terminal states are checked
            # first and named for what they actually are.
            if await any_visible(
                page, selectors.JOIN_DENIED_MARKERS, timeout_ms=TERMINAL_STATE_TIMEOUT_MS
            ):
                raise JoinError(
                    JoinOutcome.DENIED,
                    "Meet refused the join outright ('You can't join this video "
                    "call'). This is not a selector problem: the meeting is not "
                    "accepting this participant. Usually the meeting does not "
                    "allow anonymous guests - it was created by a Workspace or "
                    "school account that blocks them, or it requires everyone "
                    "to be signed in. Try a meeting started from a personal "
                    "Google account, or sign the bot's browser in.",
                )
            if await any_visible(
                page, selectors.MEETING_ENDED_MARKERS, timeout_ms=TERMINAL_STATE_TIMEOUT_MS
            ):
                raise JoinError(
                    JoinOutcome.MEETING_ENDED,
                    "The meeting had already ended (or the code is no longer "
                    "active) before the bot could join.",
                )
            await self._log_labelled_controls("JOIN_BUTTON")
            raise JoinError(
                JoinOutcome.TIMED_OUT,
                "Could not find a join button on the pre-join screen, and the "
                "page is not showing a refusal or ended-meeting notice either. "
                "Meet's DOM may have changed - update selectors.JOIN_BUTTON "
                "(run with --no-headless to inspect).",
            )

        label = (await join_button.inner_text()).strip() or "<unlabelled>"
        logger.info("Clicking join control: %r", label)
        await join_button.click()
        self._emit("join_clicked", button_label=label)

        outcome = await self._await_admission()
        if outcome is not JoinOutcome.ADMITTED:
            raise JoinError(outcome, self._admission_failure_message(outcome))

        self._joined_at = time.monotonic()
        logger.info("Admitted to the meeting as %r", self._config.bot_name)
        self._emit("joined", display_name=self._config.bot_name)
        # Tell the page it is safe to start resolving real participant names:
        # before admission there is only a lobby/preview UI, which has no
        # real name tiles and was observed to feed the resolver lobby status
        # text ("Please wait until...") instead.
        with contextlib.suppress(PlaywrightError):
            await self.page.evaluate("window.__MEETBOT_ADMITTED__ = true;")
        return outcome

    def _admission_failure_message(self, outcome: JoinOutcome) -> str:
        if outcome is JoinOutcome.DENIED:
            return (
                "The host denied the join request. Ask a participant to admit "
                f"{self._config.bot_name!r}, or use a meeting that allows "
                "guests in directly."
            )
        if outcome is JoinOutcome.MEETING_ENDED:
            return "The meeting had already ended before the bot was admitted."
        return (
            "Never admitted to the meeting after "
            f"{self._config.admission_timeout_s}s. Nobody let the bot in, or "
            "Meet's waiting-room DOM has changed."
        )

    async def _dismiss_interstitials(self) -> None:
        """Click through cookie banners and one-off tips, best effort.

        Two passes: some dialogs only render after the one in front of them is
        dismissed.
        """
        for _ in range(2):
            dismissed = False
            for selector in selectors.DISMISS_BUTTONS:
                try:
                    locator = self.page.locator(selector).first
                    await locator.wait_for(state="visible", timeout=800)
                    await locator.click(timeout=2000)
                    logger.debug("Dismissed interstitial: %r", selector)
                    dismissed = True
                except PlaywrightTimeoutError:
                    continue
                except PlaywrightError as exc:
                    logger.debug("Could not click %r: %s", selector, exc)
                    continue
            if not dismissed:
                return

    async def _set_display_name(self) -> None:
        """Type the (disclosing) display name into the guest name field.

        Absence of the field is not fatal: some meetings drop straight to the
        join screen. It is logged at WARNING because the bot would then join
        under a name that does not announce recording.
        """
        name_input = await first_visible(
            self.page, selectors.NAME_INPUT, timeout_ms=_NAME_FIELD_TIMEOUT_MS
        )
        if name_input is None:
            logger.warning(
                "No guest name field found. The bot may join under a name that "
                "does not disclose recording - verify before relying on this."
            )
            self._emit("name_field_missing")
            return
        try:
            await name_input.fill(self._config.bot_name)
            logger.info("Display name set to %r", self._config.bot_name)
        except PlaywrightError as exc:
            logger.warning("Could not set the display name: %s", exc)

    #: How many times to retry muting a device that is still verified "on"
    #: after an attempt. A single try was observed to race the pre-join
    #: screen settling and occasionally leave the mic hot; a bot joining
    #: with a live mic is not acceptable, so this keeps trying rather than
    #: warning once and joining anyway.
    _MUTE_ATTEMPTS = 4

    async def _disable_devices(self) -> None:
        """Turn the bot's own microphone and camera off before joining.

        Meet labels these buttons by the action they perform, so a visible
        "Turn off microphone" means the mic is currently live. Retries both
        the button click and Meet's keyboard shortcut (Ctrl+D / Ctrl+E) up to
        :data:`_MUTE_ATTEMPTS` times each, re-verifying after every attempt,
        before giving up and surfacing it.
        """
        still_on: list[str] = []
        for name, candidates, shortcut in (
            ("microphone", selectors.MIC_TOGGLE_ON, "Control+d"),
            ("camera", selectors.CAMERA_TOGGLE_ON, "Control+e"),
        ):
            muted = False
            for attempt in range(1, self._MUTE_ATTEMPTS + 1):
                # An "on" toggle only exists while the device is live; its
                # disappearance is the actual, verified signal that muting
                # took effect - not just that a click didn't error.
                if not await any_visible(self.page, candidates, timeout_ms=800):
                    muted = True
                    break

                toggle = await first_visible(self.page, candidates, timeout_ms=2000)
                clicked = False
                if toggle is not None:
                    try:
                        await toggle.click(timeout=3000)
                        clicked = True
                    except PlaywrightError as exc:
                        logger.warning(
                            "Could not click the %s toggle (attempt %d/%d): %s",
                            name,
                            attempt,
                            self._MUTE_ATTEMPTS,
                            exc,
                        )
                if not clicked:
                    logger.info(
                        "Falling back to the %s keyboard shortcut "
                        "(attempt %d/%d)",
                        name,
                        attempt,
                        self._MUTE_ATTEMPTS,
                    )
                    with contextlib.suppress(PlaywrightError):
                        await self.page.keyboard.press(shortcut)

                # Give the UI a moment to reflect the change before the next
                # verification pass - clicking and re-checking in the same
                # instant is exactly the race that caused the original bug.
                await asyncio.sleep(0.4)

            if muted:
                logger.info("Turned %s off", name)
            else:
                still_on.append(name)

        if still_on:
            logger.warning(
                "Could not verify these devices are off after %d attempt(s): "
                "%s. Check selectors.MIC_TOGGLE_ON / CAMERA_TOGGLE_ON.",
                self._MUTE_ATTEMPTS,
                ", ".join(still_on),
            )
            self._emit("device_mute_uncertain", devices=still_on)
        else:
            self._emit("devices_muted")

    async def _await_admission(self) -> JoinOutcome:
        """Poll until admitted, denied, or the admission timeout expires."""
        deadline = time.monotonic() + self._config.admission_timeout_s
        announced_waiting = False

        while time.monotonic() < deadline:
            if self._stop_requested.is_set():
                return JoinOutcome.TIMED_OUT
            if await any_visible(self.page, selectors.IN_CALL_MARKERS):
                return JoinOutcome.ADMITTED
            if await any_visible(self.page, selectors.JOIN_DENIED_MARKERS):
                return JoinOutcome.DENIED
            if await any_visible(self.page, selectors.MEETING_ENDED_MARKERS):
                return JoinOutcome.MEETING_ENDED
            if not announced_waiting and await any_visible(
                self.page, selectors.WAITING_FOR_HOST_MARKERS
            ):
                announced_waiting = True
                remaining = int(deadline - time.monotonic())
                logger.info(
                    "Waiting for a host to admit the bot (up to %ds remaining)",
                    remaining,
                )
                self._emit("waiting_for_admission", timeout_s=remaining)
            await asyncio.sleep(2.0)

        return JoinOutcome.TIMED_OUT

    # -- in-call monitoring -------------------------------------------------

    async def participant_count(self) -> int | None:
        """Best-effort participant count from the People control.

        Returns ``None`` when the count cannot be read, which callers must
        treat as "unknown" rather than "zero" - the alone-detection heuristic
        depends on that distinction.
        """
        for selector in selectors.PARTICIPANT_COUNT:
            try:
                locator = self.page.locator(selector).first
                if not await locator.is_visible(timeout=600):
                    continue
                values = [
                    await locator.get_attribute(attribute) or ""
                    for attribute in selectors.PARTICIPANT_COUNT_ATTRIBUTES
                ]
                values.append(await locator.inner_text())
            except PlaywrightError:
                continue
            for value in values:
                digits = "".join(ch if ch.isdigit() else " " for ch in value).split()
                if digits:
                    return int(digits[0])
        return None

    async def read_state(self) -> MeetingState:
        """Take one reading of the meeting's current state."""
        ended = await any_visible(self.page, selectors.MEETING_ENDED_MARKERS)
        in_call = False if ended else await any_visible(
            self.page, selectors.IN_CALL_MARKERS
        )
        count = None if ended else await self.participant_count()
        return MeetingState(in_call=in_call, participant_count=count, ended=ended)

    async def wait_until_meeting_ends(
        self, *, on_poll: Callable[[MeetingState], Awaitable[None] | None] | None = None
    ) -> EndReason:
        """Block until the call ends, the bot is removed, or a limit is hit.

        Detection is deliberately layered, because Meet gives no single
        reliable "the call is over" signal:

        1. An explicit end/removal banner in the DOM.
        2. Loss of the in-call markers for two consecutive polls (a removal
           that redirects before the banner renders).
        3. The bot being the only participant left for ``alone_timeout_s``.
        4. ``max_meeting_duration_s`` as a backstop.

        Args:
            on_poll: Optional callback invoked with each state reading, for
                health checks.

        Returns:
            The reason monitoring stopped.
        """
        started = self._joined_at if self._joined_at is not None else time.monotonic()
        alone_since: float | None = None
        missing_markers = 0

        while True:
            if self._stop_requested.is_set():
                logger.info("Stop requested; leaving the meeting")
                return EndReason.STOPPED

            elapsed = time.monotonic() - started
            if elapsed >= self._config.max_meeting_duration_s:
                logger.warning(
                    "Maximum meeting duration (%ds) reached; leaving",
                    self._config.max_meeting_duration_s,
                )
                return EndReason.MAX_DURATION

            if self._page is None or self._page.is_closed():
                logger.error("Browser page disappeared; ending the run")
                return EndReason.BROWSER_LOST

            try:
                state = await self.read_state()
            except PlaywrightError as exc:
                logger.error("Could not read meeting state: %s", exc)
                return EndReason.BROWSER_LOST

            if on_poll is not None:
                result = on_poll(state)
                if asyncio.iscoroutine(result):
                    await result

            if state.ended:
                logger.info("Meeting ended or the bot was removed")
                return EndReason.MEETING_ENDED

            if not state.in_call:
                missing_markers += 1
                logger.warning(
                    "In-call markers not found (%d consecutive poll(s))",
                    missing_markers,
                )
                if missing_markers >= 2:
                    logger.info("No longer in the call; assuming removal")
                    return EndReason.REMOVED
            else:
                missing_markers = 0

            # A count of 1 means the bot is the only one left.
            if state.participant_count is not None and state.participant_count <= 1:
                alone_since = alone_since or time.monotonic()
                alone_for = time.monotonic() - alone_since
                if alone_for >= self._config.alone_timeout_s:
                    logger.info(
                        "Alone in the meeting for %.0fs; leaving", alone_for
                    )
                    return EndReason.ALONE
            else:
                alone_since = None

            await asyncio.sleep(_POLL_INTERVAL_S)

    async def start_caption_overlay(self) -> bool:
        """Start screen-sharing the live-caption canvas. Call once, after
        admission.

        This is the one Meet-DOM-dependent step in the whole caption
        overlay: clicking "Present now" triggers Meet's own call to
        ``getDisplayMedia``, which ``inject.js`` has patched to return the
        caption canvas instead of prompting a real screen picker. Everything
        after this point (updating what the canvas shows) is a page
        function call, not a DOM interaction, so it cannot be broken by a
        Meet UI change the way per-message chat posting was.

        Returns:
            Whether the share was started.
        """
        if self._page is None or self._page.is_closed():
            return False
        try:
            present_button = await first_visible(
                self._page, selectors.PRESENT_NOW_BUTTON, timeout_ms=3000
            )
            if present_button is None:
                logger.warning(
                    "No 'Present now' control found; captions will not be "
                    "visible in the call. Check selectors.PRESENT_NOW_BUTTON."
                )
                await self._log_labelled_controls("PRESENT_NOW_BUTTON")
                return False
            await present_button.click(timeout=3000)

            # Patching getDisplayMedia suppresses the *browser's* source
            # picker, but not Meet's own "entire screen / window / tab" menu,
            # which opens first and still has to be clicked through. If Meet
            # asked for the stream straight away, skip the menu entirely.
            if not await self._wait_for_overlay_share(_OVERLAY_MENU_PROBE_S):
                source = await first_visible(
                    self._page, selectors.PRESENT_SOURCE_TAB, timeout_ms=2000
                )
                if source is not None:
                    await source.click(timeout=2000)

            # The click sequence is not the success signal - the page hook
            # recording that Meet actually requested a display stream is.
            # Without this check a wrong selector reports success and the
            # captions are silently invisible for the whole meeting.
            if not await self._wait_for_overlay_share(_OVERLAY_SHARE_TIMEOUT_S):
                logger.warning(
                    "Share controls were clicked but Meet never requested a "
                    "display stream, so captions are NOT visible in the call. "
                    "Check selectors.PRESENT_SOURCE_TAB."
                )
                await self._log_labelled_controls("PRESENT_SOURCE_TAB")
                return False

            logger.info("Caption overlay sharing started")
            return True
        except PlaywrightError as exc:
            logger.warning("Could not start the caption overlay: %s", exc)
            return False

    async def enable_captions(self) -> bool:
        """Turn Meet's live captions on for this browser only.

        Captions are what make real speaker names available at all: nothing
        links an audio track to a participant, but Meet labels each caption
        entry with the name its own server-side attribution produced.

        This is a per-viewer setting. It changes what this browser renders
        and does not turn on captions, transcription or recording for anyone
        else in the call.

        Returns:
            Whether captions ended up on.
        """
        if self._page is None or self._page.is_closed():
            return False
        for attempt in range(1, _CAPTION_TOGGLE_ATTEMPTS + 1):
            try:
                if await any_visible(self._page, selectors.CAPTIONS_TOGGLE_ON):
                    logger.info("Live captions are on")
                    return True
                toggle = await first_visible(
                    self._page, selectors.CAPTIONS_TOGGLE_OFF, timeout_ms=2000
                )
                if toggle is None:
                    logger.debug(
                        "No captions control visible (attempt %d/%d)",
                        attempt,
                        _CAPTION_TOGGLE_ATTEMPTS,
                    )
                    await asyncio.sleep(0.5)
                    continue
                await toggle.click(timeout=3000)
                await asyncio.sleep(0.6)
            except PlaywrightError as exc:
                logger.debug("Captions toggle attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(0.5)

        # Checked once more rather than trusting the last click: the "on"
        # marker appearing is the only real confirmation.
        if await any_visible(self._page, selectors.CAPTIONS_TOGGLE_ON):
            logger.info("Live captions are on")
            return True
        logger.warning(
            "Could not turn on live captions; speaker names will stay generic. "
            "Check selectors.CAPTIONS_TOGGLE_OFF."
        )
        await self._log_labelled_controls("CAPTIONS_TOGGLE_OFF")
        return False

    async def read_caption_entries(self) -> list[tuple[str, str]]:
        """Visible ``(speaker, text)`` caption entries, most recent last.

        The text is returned only so the caller can tell which entry Meet is
        actively rewriting - a caption whose text changed between polls is
        the speaker who is talking *now*, as opposed to one still on screen
        from a moment ago. :class:`meetbot.captions.CaptionWatcher` does that
        comparison; the words themselves are never transcribed from here.

        Never raises - a caption panel that cannot be read is a missing
        feature, not a failed run.
        """
        if self._page is None or self._page.is_closed():
            return []
        for selector in selectors.CAPTION_REGION:
            try:
                entries = await self._page.eval_on_selector_all(
                    selector, selectors.CAPTION_ENTRY_EXTRACTOR
                )
            except PlaywrightError as exc:
                logger.debug("Caption region %r unreadable: %s", selector, exc)
                continue
            pairs = [
                (str(entry.get("name", "")), str(entry.get("text", "")))
                for entry in entries or []
                if isinstance(entry, dict)
            ]
            if pairs:
                return pairs
        return []

    async def _overlay_is_shared(self) -> bool:
        """Whether the page hook has handed Meet the caption stream."""
        if self._page is None or self._page.is_closed():
            return False
        try:
            return bool(
                await self._page.evaluate(
                    "() => !!(window.__meetbot && window.__meetbot.overlayShared())"
                )
            )
        except PlaywrightError:
            return False

    async def _wait_for_overlay_share(self, timeout_s: float) -> bool:
        """Poll :meth:`_overlay_is_shared` until it is true or time runs out."""
        deadline = time.monotonic() + timeout_s
        while True:
            if await self._overlay_is_shared():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _log_labelled_controls(self, selector_name: str) -> None:
        """Dump every labelled control on the page, to fix a selector with real data.

        Best effort and never raises: this only ever runs on a path that has
        already failed, and must not turn a warning into a crash.
        """
        if self._page is None or self._page.is_closed():
            return
        with contextlib.suppress(PlaywrightError):
            labels = await self._page.eval_on_selector_all(
                selectors.ALL_LABELED_ELEMENTS[0],
                selectors.LABEL_ATTRIBUTE_EXTRACTOR,
            )
            real_labels = sorted({label for label in labels if label})
            logger.warning(
                "Every labeled control on the page right now, to fix %s with "
                "real data: %s",
                selector_name,
                real_labels,
            )

    async def update_caption(self, speaker: str, text: str) -> bool:
        """Push one finalised utterance onto the live-caption canvas.

        A page function call, not a DOM interaction - failures here mean the
        page is gone, not that a selector broke.
        """
        if self._page is None or self._page.is_closed():
            return False
        try:
            await self._page.evaluate(
                "([speaker, text]) => window.__meetbot.updateCaption(speaker, text)",
                [speaker, text],
            )
            return True
        except PlaywrightError as exc:
            logger.warning("Could not update the caption overlay: %s", exc)
            return False

    async def leave(self) -> None:
        """Click "Leave call" if it is still available. Best effort."""
        if self._page is None or self._page.is_closed():
            return
        leave_button = await first_visible(
            self._page, selectors.LEAVE_BUTTON, timeout_ms=2000
        )
        if leave_button is None:
            logger.debug("No leave button available; the call is already over")
            return
        # Escalating fallbacks, because failing to leave is not cosmetic: the
        # bot stays visible in someone else's meeting until they remove it.
        # Observed live - the button resolves but a normal click times out,
        # because while presenting, Meet floats a bar and tooltips over the
        # call controls and Playwright waits forever for the element to stop
        # being obscured. force= skips that actionability wait; the DOM click
        # is a last resort that cannot be intercepted at all.
        attempts = (
            ("click", lambda: leave_button.click(timeout=3000)),
            ("force click", lambda: leave_button.click(timeout=3000, force=True)),
            ("dom click", lambda: leave_button.evaluate("el => el.click()")),
        )
        for label, attempt in attempts:
            try:
                await attempt()
            except PlaywrightError as exc:
                logger.debug("Leave via %s failed: %s", label, exc)
                continue
            logger.info("Left the call (%s)", label)
            self._emit("left_call")
            return
        logger.warning(
            "Could not click leave; closing the browser instead. The bot may "
            "linger in the participant list for a moment."
        )

    async def read_participant_names(self) -> list[str]:
        """Real display names in the call, excluding the bot itself.

        Reads Meet's own People roster, which is the only place a page script
        can see real names: Meet attaches remote audio elements directly to
        ``<body>``, with no DOM link to the tile showing whose audio it is,
        so a name cannot be derived from a track. See the note on speaker
        labelling in the README.

        Returns an empty list if the roster cannot be read - never raises.
        """
        if self._page is None or self._page.is_closed():
            return []
        names: list[str] = []
        try:
            for selector in selectors.PARTICIPANT_LIST_ITEM:
                rows = self._page.locator(selector)
                count = await rows.count()
                if not count:
                    continue
                for index in range(count):
                    text = (await rows.nth(index).inner_text()).strip()
                    first_line = text.splitlines()[0].strip() if text else ""
                    if first_line and first_line != self._config.bot_name:
                        names.append(first_line)
                if names:
                    break
        except PlaywrightError as exc:
            logger.debug("Could not read the participant roster: %s", exc)
            return []
        return names

    async def capture_stats(self) -> dict[str, Any] | None:
        """Read the browser hook's capture stats, or ``None`` if unavailable."""
        if self._page is None or self._page.is_closed():
            return None
        try:
            return await self._page.evaluate(
                "() => window.__meetbot ? window.__meetbot.stats() : null"
            )
        except PlaywrightError as exc:
            logger.debug("Could not read capture stats: %s", exc)
            return None
