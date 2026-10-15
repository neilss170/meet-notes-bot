"""Ending a recording nobody remembered to stop.

A recording on this machine runs until somebody says stop, and the moment
people forget is exactly the end of the call: the meeting is over, the next
one has started, and Scribe is still writing down the room. So it watches for
three signs that the meeting is over:

- **The call app let go of the microphone.** Meet, Zoom, Teams and the rest
  hold it for the length of a call and release it when you leave. Only a
  call seen *during* this recording counts, so a recording started with no
  call running is not ended by there still being no call.
- **Fifteen minutes in which nothing was heard** on either side.
- **The computer slept.** Audio devices do not survive sleep, and a meeting
  is rarely still going when the lid opens again.

None of them stops at once. Each starts a ten-second countdown the page shows
with a way to cancel it - a muted stretch of a long meeting, or a call app
that briefly drops the microphone, must not cost somebody their recording.
Cancelling also says "not for this reason": the same sign has to happen again
from scratch before it can count.

This module only decides. The service feeds it what it sees once a second and
does the stopping, which keeps every rule here testable with a fake clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: How long the page has to say "keep recording".
COUNTDOWN_S = 10.0

#: Nothing heard on either channel for this long ends the meeting.
SILENCE_S = 15 * 60.0

#: The call app must have let go of the microphone for this long. Some apps
#: release and re-open it when the audio device changes mid-call.
CALL_GONE_S = 6.0

#: A gap this long between two of the service's one-second ticks means the
#: machine was asleep in between.
SLEEP_GAP_S = 30.0

CALL_ENDED = "call_ended"
SILENCE = "silence"
SLEPT = "sleep"


@dataclass
class _Watch:
    """What has been seen of one recording."""

    call_seen: bool = False
    gone_since: float | None = None
    #: Silence is counted from here at the earliest - the moment a silence
    #: countdown was cancelled - so cancelling buys another full stretch.
    quiet_from: float | None = None
    reason: str = ""
    deadline: float = 0.0


@dataclass
class AutoStop:
    """Decides when each live recording should end by itself.

    Attributes:
        countdown_s: Seconds between deciding to stop and stopping.
        silence_s: Seconds of silence that end a meeting.
        call_gone_s: Seconds the call app must have released the microphone.
        clock: Wall-clock seconds; replaced in tests.
    """

    countdown_s: float = COUNTDOWN_S
    silence_s: float = SILENCE_S
    call_gone_s: float = CALL_GONE_S
    clock: Callable[[], float] = time.time
    _watches: dict[str, _Watch] = field(default_factory=dict)

    def observe(
        self,
        job_id: str,
        *,
        call_on_mic: bool | None,
        quiet_s: float | None,
    ) -> str:
        """Take one look at a running recording.

        Args:
            job_id: The recording.
            call_on_mic: Whether another app holds the microphone right now,
                or ``None`` when that cannot be known (call alerts off).
            quiet_s: Seconds since anything was heard, or ``None`` when the
                recording cannot say.

        Returns:
            The reason a countdown started on this look, or ``""``.
        """
        now = self.clock()
        watch = self._watches.setdefault(job_id, _Watch())
        if watch.reason:
            return ""
        if call_on_mic:
            watch.call_seen = True
            watch.gone_since = None
        elif call_on_mic is False and watch.call_seen:
            if watch.gone_since is None:
                watch.gone_since = now
            elif now - watch.gone_since >= self.call_gone_s:
                return self._start(watch, CALL_ENDED, now)
        if quiet_s is not None:
            quiet = quiet_s
            if watch.quiet_from is not None:
                quiet = min(quiet, now - watch.quiet_from)
            if quiet >= self.silence_s:
                return self._start(watch, SILENCE, now)
        return ""

    def slept(self, job_id: str) -> str:
        """The machine just woke up with this recording running."""
        watch = self._watches.setdefault(job_id, _Watch())
        if watch.reason:
            return ""
        return self._start(watch, SLEPT, self.clock())

    def _start(self, watch: _Watch, reason: str, now: float) -> str:
        watch.reason = reason
        watch.deadline = now + self.countdown_s
        return reason

    def due(self) -> list[tuple[str, str]]:
        """Recordings whose countdown has run out, as ``(job_id, reason)``.

        Each is returned once and forgotten: the caller stops it.
        """
        now = self.clock()
        ended = [
            (job_id, watch.reason)
            for job_id, watch in self._watches.items()
            if watch.reason and now >= watch.deadline
        ]
        for job_id, _ in ended:
            self._watches.pop(job_id, None)
        return ended

    def cancel(self, job_id: str) -> bool:
        """Keep recording. Returns whether a countdown was running."""
        watch = self._watches.get(job_id)
        if watch is None or not watch.reason:
            return False
        if watch.reason == CALL_ENDED:
            # Only a call that starts again, and ends again, counts now.
            watch.call_seen = False
        elif watch.reason == SILENCE:
            watch.quiet_from = self.clock()
        watch.gone_since = None
        watch.reason = ""
        watch.deadline = 0.0
        return True

    def pending(self, job_id: str) -> dict[str, Any] | None:
        """The countdown running for a recording, as the page shows it."""
        watch = self._watches.get(job_id)
        if watch is None or not watch.reason:
            return None
        left = max(0.0, watch.deadline - self.clock())
        return {"reason": watch.reason, "seconds_left": round(left, 1)}

    def keep_only(self, job_ids: set[str]) -> None:
        """Forget recordings that are no longer running."""
        for job_id in list(self._watches):
            if job_id not in job_ids:
                del self._watches[job_id]


__all__ = [
    "CALL_ENDED",
    "CALL_GONE_S",
    "COUNTDOWN_S",
    "SILENCE",
    "SILENCE_S",
    "SLEEP_GAP_S",
    "SLEPT",
    "AutoStop",
]
