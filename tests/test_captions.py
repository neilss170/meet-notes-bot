"""Caption-based speaker attribution.

The behaviour that matters here is not "does it find a name" but "does it
refuse to invent one". A wrong name on an utterance is worse than the generic
``Speaker 0`` it replaces, so most of these tests are about the cases where
attribution should decline.
"""

from __future__ import annotations

import pytest

from meetbot.captions import (
    CaptionWatcher,
    SpeakerTimeline,
    anonymise,
    normalise_name,
)


class TestNormaliseName:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Neil Sharma", "Neil Sharma"),
            ("  Neil   Sharma \n", "Neil Sharma"),
            ("Neil Sharma (You)", "Neil Sharma"),
            ("Neil Sharma (you)", "Neil Sharma"),
            ("Priya (Host)", "Priya"),
            ("Priya (presenting)", "Priya"),
            ("Neil Sharma:", "Neil Sharma"),
        ],
    )
    def test_keeps_real_names(self, raw: str, expected: str) -> None:
        assert normalise_name(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "You",
            "you",
            "Unknown",
            "Captions",
            "Jump to bottom",
            "11:32",  # a bare clock reading, seen in earlier DOM scraping
            "2026",
            "---",
        ],
    )
    def test_rejects_ui_strings_and_non_names(self, raw: str) -> None:
        """These are the strings that made DOM name-scraping fail before."""
        assert normalise_name(raw) == ""


class TestSpeakerTimeline:
    def test_resolves_the_speaker_who_overlaps_most(self) -> None:
        timeline = SpeakerTimeline()
        for t in (1.0, 2.0, 3.0):
            timeline.observe("Neil Sharma", t)
        for t in (8.0, 9.0):
            timeline.observe("Priya", t)
        assert timeline.resolve(1.0, 3.0) == "Neil Sharma"
        assert timeline.resolve(8.0, 9.0) == "Priya"

    def test_returns_none_when_nothing_overlaps(self) -> None:
        """Silence must stay generic rather than borrow a nearby name."""
        timeline = SpeakerTimeline()
        timeline.observe("Neil Sharma", 1.0)
        assert timeline.resolve(500.0, 502.0) is None

    def test_returns_none_on_a_sliver_of_overlap(self) -> None:
        """A brush against the edge of an interval is not an attribution."""
        timeline = SpeakerTimeline(min_overlap_s=0.5)
        timeline.observe("Neil Sharma", 10.0)  # interval ends at 10.0
        assert timeline.resolve(9.9, 12.0) is None

    def test_a_single_sighting_still_resolves(self) -> None:
        """Regression: a lone observation must not be a zero-width interval.

        A caption is noticed at a poll, but the speech happened at some point
        since the previous poll. Recording an instant would overlap nothing
        and could never be matched to an utterance.
        """
        timeline = SpeakerTimeline(nominal_width_s=1.5)
        timeline.observe("Priya", 10.0)
        assert timeline.resolve(9.0, 10.0) == "Priya"

    def test_merges_consecutive_sightings_into_one_interval(self) -> None:
        timeline = SpeakerTimeline()
        for t in (1.0, 1.5, 2.0, 2.5, 3.0):
            timeline.observe("Neil Sharma", t)
        assert timeline.resolve(1.0, 3.0) == "Neil Sharma"
        assert timeline.speakers == {"Neil Sharma"}

    def test_a_long_gap_starts_a_new_stretch_rather_than_bridging_silence(
        self,
    ) -> None:
        """Two separate turns must not merge into one interval across a pause."""
        timeline = SpeakerTimeline(max_gap_s=2.0, nominal_width_s=0.5)
        timeline.observe("Neil Sharma", 1.0)
        timeline.observe("Neil Sharma", 60.0)
        # The silence between the two turns belongs to neither.
        assert timeline.resolve(20.0, 40.0) is None

    def test_handles_reversed_windows(self) -> None:
        timeline = SpeakerTimeline()
        timeline.observe("Neil Sharma", 2.0)
        assert timeline.resolve(3.0, 1.0) == "Neil Sharma"

    def test_ignores_unusable_names(self) -> None:
        timeline = SpeakerTimeline()
        timeline.observe("You", 1.0)
        timeline.observe("11:32", 2.0)
        assert timeline.speakers == set()
        assert timeline.resolve(1.0, 2.0) is None

    def test_prunes_old_intervals(self) -> None:
        timeline = SpeakerTimeline(history_s=10.0)
        timeline.observe("Neil Sharma", 1.0)
        timeline.observe("Priya", 100.0)
        assert timeline.speakers == {"Priya"}


class TestCaptionWatcher:
    def test_an_unchanged_entry_is_not_a_new_observation(self) -> None:
        """The core of it: Meet leaves finished captions on screen.

        Observing every visible name each poll would mark someone who spoke
        ten seconds ago as still talking. Only a caption whose text *changed*
        belongs to the person speaking now.
        """
        watcher = CaptionWatcher()
        assert watcher.poll([("Neil Sharma", "Hello")], 1.0) == ["Neil Sharma"]
        assert watcher.poll([("Neil Sharma", "Hello there")], 2.0) == ["Neil Sharma"]
        assert watcher.poll([("Neil Sharma", "Hello there")], 3.0) == []
        assert watcher.poll([("Neil Sharma", "Hello there")], 9.0) == []

    def test_a_stale_entry_does_not_steal_a_later_utterance(self) -> None:
        """The bug this design exists to prevent."""
        watcher = CaptionWatcher()
        watcher.poll([("Neil Sharma", "Morning everyone")], 1.0)
        # Neil's caption lingers on screen while Priya starts talking.
        for t, priya in ((5.0, "Yes"), (6.0, "Yes I finished it")):
            watcher.poll([("Neil Sharma", "Morning everyone"), ("Priya", priya)], t)
        assert watcher.resolve(5.0, 6.0) == "Priya"

    def test_new_speakers_are_observed_on_first_sight(self) -> None:
        watcher = CaptionWatcher()
        active = watcher.poll([("Neil Sharma", "Hi"), ("Priya", "Hello")], 1.0)
        assert set(active) == {"Neil Sharma", "Priya"}

    def test_ignores_entries_without_a_usable_name(self) -> None:
        watcher = CaptionWatcher()
        assert watcher.poll([("You", "something"), ("", "text")], 1.0) == []
        assert watcher.timeline.speakers == set()

    def test_empty_snapshots_are_harmless(self) -> None:
        watcher = CaptionWatcher()
        assert watcher.poll([], 1.0) == []
        assert watcher.resolve(0.0, 5.0) is None

    def test_resolve_matches_a_realistic_conversation(self) -> None:
        watcher = CaptionWatcher()
        script = [
            (1.0, [("Neil Sharma", "Morning")]),
            (2.0, [("Neil Sharma", "Morning everyone")]),
            (3.0, [("Neil Sharma", "Morning everyone thanks for joining")]),
            (5.0, [("Neil Sharma", "Morning everyone thanks for joining"), ("Priya", "Yes")]),
            (6.0, [("Neil Sharma", "Morning everyone thanks for joining"), ("Priya", "Yes I finished the pipeline")]),
        ]
        for now, entries in script:
            watcher.poll(entries, now)
        assert watcher.resolve(1.0, 3.2) == "Neil Sharma"
        assert watcher.resolve(4.5, 6.1) == "Priya"
        assert watcher.resolve(30.0, 32.0) is None


class TestAnonymise:
    def test_is_stable_per_name(self) -> None:
        mapping: dict[str, str] = {}
        assert anonymise("Neil Sharma", mapping) == "Speaker A"
        assert anonymise("Priya", mapping) == "Speaker B"
        assert anonymise("Neil Sharma", mapping) == "Speaker A"

    def test_keeps_going_past_the_alphabet(self) -> None:
        mapping: dict[str, str] = {}
        aliases = [anonymise(f"Person {i}", mapping) for i in range(30)]
        assert len(set(aliases)) == 30
