"""Google Meet DOM selectors, centralised.

.. warning::

   **These selectors WILL break.** Google ships UI changes to Meet with no
   notice and no versioning, and the class names are obfuscated build output.
   This module is the single place to fix them - nothing outside of it should
   contain a Meet CSS selector or an aria-label string.

   Every entry is a *list of candidates*, tried in order by
   :func:`meetbot.join.first_visible`. Ordering is deliberate:

   1. ``aria-label`` / role-based selectors first - these track the
      accessibility tree, which Google changes least often.
   2. Text-content selectors next - stable in meaning, but English-only.
   3. Structural / ``jsname`` selectors last - the most brittle, kept only as
      a final fallback.

   When Meet breaks, run with ``--no-headless --log-level DEBUG``, inspect the
   failing stage in the browser, and add a new candidate at the *front* of the
   relevant list. Leave the old candidates in place: Google rolls changes out
   gradually, so several variants are often live at once.

   Last verified against the Meet UI: 2026-09.
"""

from __future__ import annotations

from typing import Final

#: Playwright selectors for the "enter your name" field on the guest pre-join
#: screen. Only shown when not signed in to a Google account.
NAME_INPUT: Final[list[str]] = [
    'input[aria-label="Your name"]',
    'input[placeholder="Your name"]',
    'input[aria-label*="name" i]',
    'input[type="text"][maxlength]',
]

#: The primary join button. Meet uses different labels depending on whether
#: the meeting requires host approval ("Ask to join") or not ("Join now").
JOIN_BUTTON: Final[list[str]] = [
    'button:has-text("Ask to join")',
    'button:has-text("Join now")',
    '[role="button"]:has-text("Ask to join")',
    '[role="button"]:has-text("Join now")',
    'button[jsname="Qx7uuf"]',
]

#: Microphone toggle on the pre-join (green room) screen. The aria-label
#: reflects the *action*, so "Turn off microphone" means the mic is currently
#: ON - that is the one we want to click.
MIC_TOGGLE_ON: Final[list[str]] = [
    '[aria-label="Turn off microphone"]',
    '[aria-label*="Turn off microphone" i]',
    'div[role="button"][data-is-muted="false"][aria-label*="microphone" i]',
]

#: Camera toggle on the pre-join screen; same "label describes the action"
#: convention as :data:`MIC_TOGGLE_ON`.
CAMERA_TOGGLE_ON: Final[list[str]] = [
    '[aria-label="Turn off camera"]',
    '[aria-label*="Turn off camera" i]',
    'div[role="button"][data-is-muted="false"][aria-label*="camera" i]',
]

#: Elements that only exist once we are actually inside the call. Presence of
#: any of them is our "admitted" signal.
IN_CALL_MARKERS: Final[list[str]] = [
    '[aria-label="Leave call"]',
    'button[aria-label*="Leave call" i]',
    '[data-tooltip-id="tt-c11"]',
    'div[jsname="A5il2e"]',  # meeting details / call controls container
]

#: Shown while the host has not yet admitted us.
WAITING_FOR_HOST_MARKERS: Final[list[str]] = [
    'text="Asking to be let in"',
    'text="Asking to join"',
    ':text("Asking to be let in")',
    ':text("You\'ll join the call when someone lets you in")',
]

#: Terminal states: the host denied entry, removed us, or the call ended.
#: Matching any of these means "stop, do not retry".
MEETING_ENDED_MARKERS: Final[list[str]] = [
    ':text("You\'ve been removed from the meeting")',
    ':text("You were removed from the meeting")',
    ':text("Your meeting has ended")',
    ':text("The meeting has ended")',
    ':text("You left the meeting")',
    ':text("Return to home screen")',
    'button:has-text("Rejoin")',
]

#: Shown when the host actively denies the join request.
JOIN_DENIED_MARKERS: Final[list[str]] = [
    ':text("Someone in the call denied your request to join")',
    ':text("No one responded to your request to join")',
    ':text("You can\'t join this video call")',
]

#: Dismissable interstitials that sit between us and the pre-join screen
#: (cookie banners, "Got it" tips, device permission nags).
DISMISS_BUTTONS: Final[list[str]] = [
    'button:has-text("Got it")',
    'button:has-text("Dismiss")',
    'button:has-text("Continue without microphone and camera")',
    'button:has-text("Continue without microphone")',
    'button:has-text("Accept all")',
    '[aria-label="Close"]',
]

#: The "people" button carries the live participant count in its label or in a
#: sibling badge, e.g. ``aria-label="People (3)"``.
PARTICIPANT_COUNT: Final[list[str]] = [
    'button[aria-label^="People"]',
    '[aria-label*="Show everyone" i]',
    'div[jsname="EdSHqe"]',
]

#: Attributes on the People control that may carry the participant count,
#: e.g. ``aria-label="People (3)"``. Read in order; the first that yields a
#: number wins, with the element's text content as the final fallback.
PARTICIPANT_COUNT_ATTRIBUTES: Final[list[str]] = [
    "aria-label",
    "data-participant-count",
]

#: Roster rows in the People panel; the participant's name is the text content.
PARTICIPANT_LIST_ITEM: Final[list[str]] = [
    'div[role="list"] div[role="listitem"]',
    'div[aria-label="Participants"] div[role="listitem"]',
]

#: Leave-call button, used for a clean exit at the end of a run.
LEAVE_BUTTON: Final[list[str]] = [
    '[aria-label="Leave call"]',
    'button[aria-label*="Leave call" i]',
]

#: UNVERIFIED against a live call - written from Meet's known accessibility
#: conventions, not from inspecting real markup. Starts screen-sharing (used
#: to present the live-caption canvas). Confirm/extend against real DOM
#: before relying on this; per this module's own rule, put a new working
#: candidate at the *front* of the list, don't replace what's here. Clicked
#: exactly once per meeting, unlike the old per-message chat selectors this
#: replaced, so a wrong guess here is far cheaper to hit and fix.
PRESENT_NOW_BUTTON: Final[list[str]] = [
    # VERIFIED 2026-09 against a live call: Meet labels this "Share screen",
    # not "Present now". The older guesses below are kept per this module's
    # rule - Google rolls changes out gradually and both may be live at once.
    '[aria-label="Share screen"]',
    'button[aria-label*="Share screen" i]',
    '[aria-label="Present now"]',
    'button[aria-label*="Present now" i]',
    'button[aria-label^="Present" i]',
]

#: UNVERIFIED. The "A tab" / "Entire screen" / "A window" choice Meet shows
#: after "Present now" - only relevant if `getDisplayMedia` is NOT patched
#: (i.e. a fallback path); when it is patched, this dialog never appears
#: because our stream is returned before Meet's real picker would show.
PRESENT_SOURCE_TAB: Final[list[str]] = [
    'button:has-text("A tab")',
    '[aria-label*="tab" i][role="button"]',
]

#: Generic (not Meet-specific) query used only to *diagnose* a failed
#: selector match: every labeled control on the page, so the real available
#: labels can be logged and used to fix the list above with real data
#: instead of another guess. A single-item list, like every other entry in
#: this module, so it passes the same shape checks - not a fallback chain.
ALL_LABELED_ELEMENTS: Final[list[str]] = ['button[aria-label], [role="button"][aria-label]']

#: Companion to :data:`ALL_LABELED_ELEMENTS`: the page-side function that
#: extracts each matched element's label attribute. Kept here, not inline at
#: the call site, for the same reason every other Meet-DOM-shaped string is.
LABEL_ATTRIBUTE_EXTRACTOR: Final[str] = "els => els.map(e => e.getAttribute('aria-label'))"

__all__ = [
    "NAME_INPUT",
    "JOIN_BUTTON",
    "MIC_TOGGLE_ON",
    "CAMERA_TOGGLE_ON",
    "IN_CALL_MARKERS",
    "WAITING_FOR_HOST_MARKERS",
    "MEETING_ENDED_MARKERS",
    "JOIN_DENIED_MARKERS",
    "DISMISS_BUTTONS",
    "PARTICIPANT_COUNT",
    "PARTICIPANT_COUNT_ATTRIBUTES",
    "PARTICIPANT_LIST_ITEM",
    "LEAVE_BUTTON",
    "PRESENT_NOW_BUTTON",
    "PRESENT_SOURCE_TAB",
    "ALL_LABELED_ELEMENTS",
]
