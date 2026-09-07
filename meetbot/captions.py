"""Speaker attribution from Google Meet's own live captions.

Why this module exists
----------------------

Nothing in a Meet page links a WebRTC audio track to a participant name: Meet
attaches remote ``<audio>`` elements directly to ``<body>``, so an audio-first
pipeline can only ever produce generic ``Speaker 0`` / ``Speaker 1`` labels
(see the speaker-labelling note in the README).

Meet's *caption* panel is a different surface entirely. Google does speaker
attribution server-side and renders the resulting name next to each caption
line, so the names we cannot derive are sitting in the DOM already.

What this module deliberately does NOT do
-----------------------------------------

It does not use the caption *text*. Meet mutates a caption element in place as
its recogniser refines a phrase, then scrolls it away - reconstructing a
faithful transcript from that stream means fighting duplicates, truncations
and re-writes, and the result is still less accurate than Deepgram's.

So captions are used for one thing only: **who was speaking, and when**. That
reduces the problem from "reassemble mutating text" to "record intervals",
which is robust against exactly the mutation behaviour that makes caption
scraping fragile. The words still come from Deepgram; only the name comes from
here.

Privacy
-------

Turning captions on is a per-viewer Meet setting: it changes what the bot's
own browser renders and does not enable captions, recording, or anything else
for other participants. Caption text is read in-page and discarded - only
speaker names and time intervals leave this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)

#: An observation older than this is dropped, so a long meeting does not grow
#: the timeline without bound. Comfortably longer than any single utterance.
DEFAULT_HISTORY_S = 900.0

#: A caption entry seen at time T is treated as covering the window from the
#: previous sighting up to T. If the gap is longer than this the speaker
#: probably paused, so the window is clamped rather than bridging silence.
MAX_INTERVAL_GAP_S = 4.0

#: Below this much overlap (in seconds) an attribution is not worth making -
#: it usually means the caption arrived for a neighbouring utterance.
MIN_OVERLAP_S = 0.35

#: The winner must also cover at least this fraction of the utterance. An
#: absolute floor alone is not enough: a 0.5s brush against the end of a 5.4s
#: utterance clears 0.35s while covering only 9% of what was said, and that
#: was observed handing a whole sentence to the wrong person. Requiring a
#: share of the utterance scales the test with its length.
MIN_COVERAGE = 0.35

#: Width given to a first sighting. A caption is noticed at a poll, but the
#: speech that produced it happened at some point since the previous poll, so
#: a single observation covers a window ending at "now" rather than a zero
#: length instant - which would overlap nothing and never resolve.
NOMINAL_WIDTH_S = 1.5

#: Strings Meet puts where a name goes that are not names.
_NOT_A_NAME = frozenset(
    {
        "you",
        "unknown",
        "unknown speaker",
        "transcript",
        "captions",
        "jump to bottom",
    }
)

_WHITESPACE = re.compile(r"\s+")
_TRAILING_ROLE = re.compile(r"\s*\((?:you|host|guest|presenting)\)\s*$", re.I)
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def normalise_name(raw: str) -> str:
    """Tidy a name as scraped, or return ``""`` if it is not a usable name.

    Meet suffixes the local participant's own row with "(You)" and pads
    entries with newlines; the panel occasionally yields UI strings or bare
    timestamps rather than names, which must not become participants.
    """
    name = _WHITESPACE.sub(" ", raw or "").strip()
    if not name:
        return ""
    name = _TRAILING_ROLE.sub("", name).strip(" :​")
    if not name or name.lower() in _NOT_A_NAME:
        return ""
    # Rejects "11:32" and similar: a name has at least one letter.
    if not _HAS_LETTER.search(name):
        return ""
    return name


@dataclass
class _Interval:
    """A window during which one speaker was captioned as talking."""

    speaker: str
    start: float
    end: float

    def overlap(self, start: float, end: float) -> float:
        """Seconds of overlap with ``[start, end]``; never negative."""
        return max(0.0, min(self.end, end) - max(self.start, start))


@dataclass
class SpeakerTimeline:
    """Who was speaking, and when, on the meeting clock.

    Fed by polling Meet's caption panel; queried by the audio pipeline to put
    a real name on an utterance whose timing it already knows.

    Both sides must use the same clock - seconds since the audio bridge
    started - or the overlap arithmetic is meaningless.
    """

    history_s: float = DEFAULT_HISTORY_S
    max_gap_s: float = MAX_INTERVAL_GAP_S
    min_overlap_s: float = MIN_OVERLAP_S
    min_coverage: float = MIN_COVERAGE
    nominal_width_s: float = NOMINAL_WIDTH_S
    _intervals: list[_Interval] = field(default_factory=list)
    _last_seen: dict[str, float] = field(default_factory=dict)

    @property
    def speakers(self) -> set[str]:
        """Every name observed so far."""
        return {interval.speaker for interval in self._intervals}

    def observe(self, speaker: str, now: float) -> None:
        """Record that ``speaker`` was captioned as talking at ``now``.

        Consecutive sightings of one speaker are merged into a single
        interval, so polling ten times over five seconds yields one
        five-second interval rather than ten point observations.
        """
        name = normalise_name(speaker)
        if not name:
            return

        previous = self._last_seen.get(name)
        self._last_seen[name] = now
        continues = previous is not None and now - previous <= self.max_gap_s

        if continues:
            for interval in reversed(self._intervals):
                if interval.speaker == name:
                    interval.end = max(interval.end, now)
                    self._prune(now)
                    return

        # Backdated to the previous sighting when that was recent, otherwise
        # by the nominal width: either way the interval spans the period the
        # speech actually happened in, not the instant we polled.
        if continues and previous is not None:
            start = previous
        else:
            start = max(0.0, now - self.nominal_width_s)
        self._intervals.append(_Interval(name, start, now))
        self._prune(now)

    def resolve(self, start: float, end: float) -> str | None:
        """The name that best matches ``[start, end]``, or ``None``.

        Returns ``None`` rather than guessing when nothing overlaps the
        window: a generic ``Speaker 0`` is a better answer than a wrong name.
        """
        if end < start:
            start, end = end, start
        totals: dict[str, float] = {}
        for interval in self._intervals:
            overlap = interval.overlap(start, end)
            if overlap > 0:
                totals[interval.speaker] = totals.get(interval.speaker, 0.0) + overlap
        if not totals:
            return None
        best, seconds = max(totals.items(), key=lambda item: item[1])
        if seconds < self.min_overlap_s:
            return None
        # An absolute floor says nothing about how much of the utterance the
        # caption actually accounts for. Without this, a brief overlap at the
        # edge of a long utterance wins it outright and hands the whole
        # sentence to whoever spoke next.
        duration = end - start
        if duration > 0 and (seconds / duration) < self.min_coverage:
            return None
        return best

    def _prune(self, now: float) -> None:
        """Drop intervals that ended before the history window."""
        cutoff = now - self.history_s
        if cutoff <= 0:
            return
        self._intervals = [i for i in self._intervals if i.end >= cutoff]
        self._last_seen = {
            name: seen for name, seen in self._last_seen.items() if seen >= cutoff
        }


@dataclass
class CaptionWatcher:
    """Turns polled caption snapshots into "who is speaking now" observations.

    The panel keeps recent entries on screen after their speaker has stopped,
    so simply observing every visible name would mark someone who spoke ten
    seconds ago as still talking. What distinguishes the live speaker is that
    Meet **rewrites their caption in place** as the recogniser refines it.

    That mutation is the thing which makes caption scraping hard if you want
    the text - and it is exactly the signal we want here. An entry whose text
    changed since the previous poll belongs to whoever is talking right now;
    an unchanged entry is history.
    """

    timeline: SpeakerTimeline = field(default_factory=SpeakerTimeline)
    _last_text: dict[str, str] = field(default_factory=dict)

    def poll(self, entries: Sequence[tuple[str, str]], now: float) -> list[str]:
        """Record activity from one caption snapshot.

        Args:
            entries: ``(speaker, text)`` pairs as currently rendered.
            now: Meeting-clock seconds, matching ``Utterance.start``.

        Returns:
            The speakers judged to be talking at ``now``.
        """
        active: list[str] = []
        for raw_name, text in entries:
            name = normalise_name(raw_name)
            if not name:
                continue
            previous = self._last_text.get(name)
            if previous is not None and previous == text:
                continue  # unchanged: already-finished speech still on screen
            self._last_text[name] = text
            # A name appearing for the first time is new speech too, so a
            # short utterance that is complete before the first poll still
            # registers rather than being lost.
            self.timeline.observe(name, now)
            active.append(name)
        return active

    def resolve(self, start: float, end: float) -> str | None:
        """Delegate to the timeline; the shape ``AudioBridge`` expects."""
        return self.timeline.resolve(start, end)


@dataclass
class SpeakerRegistry:
    """Learns which real name belongs to each diarization id.

    Caption attribution works per utterance: it needs a caption whose time
    window overlaps the speech. Plenty of utterances have no such overlap -
    a short reply between polls, speech while the panel was scrolling - and
    those fall back to "Speaker 0".

    Deepgram's diarization ids are stable within a stream though, so the
    problem only has to be solved once per person. When captions do name an
    utterance, the id that produced it can be remembered, and every other
    utterance carrying that id - before and after - takes the same name. One
    confident match names the whole meeting.

    Evidence is counted rather than taken on faith. Diarization drifts and
    captions can land on the wrong side of a boundary, and a wrong name
    applied to every one of a speaker's utterances is far worse than the
    generic label it replaced. A name has to win a majority and clear
    ``min_votes`` before it is used.
    """

    min_votes: int = 2
    _votes: dict[str, dict[str, int]] = field(default_factory=dict)

    def learn(self, diarization_id: str, name: str) -> None:
        """Record one piece of evidence that ``diarization_id`` is ``name``."""
        clean = normalise_name(name)
        if not clean or not diarization_id:
            return
        tally = self._votes.setdefault(diarization_id, {})
        tally[clean] = tally.get(clean, 0) + 1

    def name_for(self, diarization_id: str) -> str | None:
        """The name for ``diarization_id``, or ``None`` if not yet confident."""
        tally = self._votes.get(diarization_id)
        if not tally:
            return None
        name, votes = max(tally.items(), key=lambda item: item[1])
        return name if votes >= self.min_votes else None

    def mapping(self) -> dict[str, str]:
        """Every confident id -> name pair, for relabelling a transcript."""
        resolved = {}
        for diarization_id in self._votes:
            name = self.name_for(diarization_id)
            if name is not None:
                resolved[diarization_id] = name
        return resolved


def anonymise(name: str, mapping: dict[str, str]) -> str:
    """Map a real name to a stable pseudonym, extending ``mapping`` in place.

    Keeps personal names out of text sent to a third-party LLM while
    preserving who-said-what structure, so the summary can still tell
    speakers apart. The mapping is per-run and never persisted.
    """
    existing = mapping.get(name)
    if existing is not None:
        return existing
    index = len(mapping)
    suffix = chr(ord("A") + index) if index < 26 else str(index + 1)
    pseudonym = f"Speaker {suffix}"
    mapping[name] = pseudonym
    return pseudonym


__all__ = [
    "DEFAULT_HISTORY_S",
    "MAX_INTERVAL_GAP_S",
    "MIN_COVERAGE",
    "MIN_OVERLAP_S",
    "NOMINAL_WIDTH_S",
    "CaptionWatcher",
    "SpeakerRegistry",
    "SpeakerTimeline",
    "anonymise",
    "normalise_name",
]
