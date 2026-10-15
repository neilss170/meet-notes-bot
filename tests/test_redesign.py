"""What the calmer page needs from the service.

The redesign changed how things look, and asked four new things of the
service underneath it: a recording that ends by itself when the meeting is
over, the room's loudness for the recording pill, who was in each meeting for
the list, and somewhere to keep an edit to the write-up together with which
lines are now the person's own.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from meetbot.capture.calls import CallWatcher, MicSession
from meetbot.capture.local import SOUND_LEVEL, AudioDevice, LocalRecorder
from meetbot.config import Config
from meetbot.service.app import create_app
from meetbot.service.auth import Role
from meetbot.service.autostop import CALL_ENDED, SILENCE, SLEPT, AutoStop
from meetbot.service.jobs import JobStatus, MeetingJob
from meetbot.transcript.notes import Notepad
from meetbot.transcript.store import (
    MIC_EVENT,
    SPEAKER_NAMES_EVENT,
    MeetingMeta,
    TranscriptStore,
    Utterance,
    append_event,
)

np = pytest.importorskip("numpy", reason="numpy is needed to measure audio")

PASSWORD = "a-long-enough-password"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def auto(clock: Clock) -> AutoStop:
    return AutoStop(countdown_s=10, silence_s=900, call_gone_s=6, clock=clock)


# -- stopping by itself ---------------------------------------------------------


class TestTheCallEnding:
    def test_a_call_that_lets_go_of_the_mic_starts_the_countdown(self, auto, clock) -> None:
        assert auto.observe("j", call_on_mic=True, quiet_s=0) == ""
        clock.now += 1
        assert auto.observe("j", call_on_mic=False, quiet_s=0) == ""
        clock.now += 5
        assert auto.observe("j", call_on_mic=False, quiet_s=0) == ""
        clock.now += 1
        assert auto.observe("j", call_on_mic=False, quiet_s=0) == CALL_ENDED
        assert auto.pending("j") == {"reason": CALL_ENDED, "seconds_left": 10.0}

    def test_a_brief_drop_is_not_the_end(self, auto, clock) -> None:
        """Apps let go and take the mic back when the audio device changes."""
        auto.observe("j", call_on_mic=True, quiet_s=0)
        clock.now += 1
        auto.observe("j", call_on_mic=False, quiet_s=0)
        clock.now += 3
        auto.observe("j", call_on_mic=True, quiet_s=0)
        clock.now += 4
        assert auto.observe("j", call_on_mic=False, quiet_s=0) == ""
        assert auto.pending("j") is None

    def test_no_call_ever_seen_never_ends_the_recording(self, auto, clock) -> None:
        """A recording of a room, with no call app at all, is not "a call that ended"."""
        for _ in range(30):
            clock.now += 1
            assert auto.observe("j", call_on_mic=False, quiet_s=0) == ""

    def test_unknown_is_not_no(self, auto, clock) -> None:
        auto.observe("j", call_on_mic=True, quiet_s=0)
        for _ in range(30):
            clock.now += 1
            assert auto.observe("j", call_on_mic=None, quiet_s=0) == ""

    def test_keeping_it_waits_for_another_call_to_end(self, auto, clock) -> None:
        auto.observe("j", call_on_mic=True, quiet_s=0)
        clock.now += 1
        auto.observe("j", call_on_mic=False, quiet_s=0)
        clock.now += 7
        assert auto.observe("j", call_on_mic=False, quiet_s=0) == CALL_ENDED
        assert auto.cancel("j") is True
        assert auto.pending("j") is None
        for _ in range(20):
            clock.now += 1
            assert auto.observe("j", call_on_mic=False, quiet_s=0) == ""
        auto.observe("j", call_on_mic=True, quiet_s=0)
        clock.now += 1
        auto.observe("j", call_on_mic=False, quiet_s=0)
        clock.now += 7
        assert auto.observe("j", call_on_mic=False, quiet_s=0) == CALL_ENDED


class TestSilence:
    def test_fifteen_minutes_of_nothing_starts_the_countdown(self, auto) -> None:
        assert auto.observe("j", call_on_mic=None, quiet_s=899) == ""
        assert auto.observe("j", call_on_mic=None, quiet_s=900) == SILENCE

    def test_keeping_it_buys_another_full_stretch(self, auto, clock) -> None:
        auto.observe("j", call_on_mic=None, quiet_s=900)
        auto.cancel("j")
        clock.now += 60
        assert auto.observe("j", call_on_mic=None, quiet_s=960) == ""
        clock.now += 840
        assert auto.observe("j", call_on_mic=None, quiet_s=1800) == SILENCE

    def test_a_recording_that_cannot_say_is_never_silent(self, auto) -> None:
        assert auto.observe("j", call_on_mic=None, quiet_s=None) == ""


class TestTheCountdown:
    def test_it_stops_once_and_only_when_the_time_is_up(self, auto, clock) -> None:
        auto.slept("j")
        assert auto.pending("j")["reason"] == SLEPT
        clock.now += 9.9
        assert auto.due() == []
        clock.now += 0.2
        assert auto.due() == [("j", SLEPT)]
        assert auto.due() == []
        assert auto.pending("j") is None

    def test_a_countdown_is_not_restarted_by_another_reason(self, auto, clock) -> None:
        auto.slept("j")
        clock.now += 5
        assert auto.observe("j", call_on_mic=None, quiet_s=5000) == ""
        assert auto.pending("j") == {"reason": SLEPT, "seconds_left": 5.0}

    def test_cancelling_nothing_says_so(self, auto) -> None:
        assert auto.cancel("j") is False

    def test_a_recording_that_ended_is_forgotten(self, auto) -> None:
        auto.slept("old")
        auto.keep_only({"new"})
        assert auto.pending("old") is None


# -- what the service watches -----------------------------------------------------


class TestWhoHasTheMic:
    def test_nothing_is_known_before_the_first_look(self, tmp_path) -> None:
        watcher = CallWatcher(tmp_path / "m.json", reader=lambda: [], titles=lambda: [])
        assert watcher.others_on_mic() is None

    def test_another_app_on_the_mic_and_then_not(self, tmp_path) -> None:
        sessions = [MicSession(r"C:#Zoom#Zoom.exe", 133_000_000_000_000_000, False)]
        watcher = CallWatcher(
            tmp_path / "m.json", reader=lambda: list(sessions), titles=lambda: [],
            own_executables=(),
        )
        watcher.poll()
        assert watcher.others_on_mic() is True
        sessions.clear()
        watcher.poll()
        assert watcher.others_on_mic() is False

    def test_scribe_itself_does_not_count(self, tmp_path) -> None:
        own = r"C:\Program Files\Scribe\resources\server\scribe-server.exe"
        sessions = [MicSession(own.replace("\\", "#"), 133_000_000_000_000_000, False)]
        watcher = CallWatcher(
            tmp_path / "m.json", reader=lambda: sessions, titles=lambda: [],
            own_executables=(own,),
        )
        watcher.poll()
        assert watcher.others_on_mic() is False


SPEAKERS = AudioDevice(index=3, name="Speakers [Loopback]", channels=2, rate=48_000, is_loopback=True)
MIC = AudioDevice(index=9, name="Microphone", channels=2, rate=48_000, is_loopback=False)


def tone(seconds: float) -> bytes:
    t = np.arange(int(48_000 * seconds)) / 48_000
    wave = (np.sin(2 * np.pi * 440 * t) * 12_000).astype(np.int16)
    return np.repeat(wave, 2).tobytes()


class TestTheRoom:
    WANT = 1600 * 2

    def test_the_room_keeps_moving_while_you_are_muted(self) -> None:
        """The pill's bars say the meeting is being heard - which it still is."""
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.mic_muted = True
        recorder._them.callback(tone(0.2), 0, None, 0)
        recorder._next_block(self.WANT)
        assert recorder.mic_levels() == []
        assert max(recorder.room_levels()) >= SOUND_LEVEL

    def test_quiet_is_counted_from_the_last_sound(self) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.last_sound_at -= 120
        recorder._next_block(self.WANT)
        assert recorder.quiet_for() >= 119, "silence reset the quiet clock"
        recorder._me.callback(tone(0.2), 0, None, 0)
        recorder._next_block(self.WANT)
        assert recorder.quiet_for() < 1

    def test_your_voice_does_not_count_while_muted(self) -> None:
        """Muted, Scribe is not hearing you - so you cannot keep it awake."""
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.last_sound_at -= 120
        recorder.mic_muted = True
        recorder._me.callback(tone(0.2), 0, None, 0)
        recorder._next_block(self.WANT)
        assert recorder.quiet_for() >= 119


# -- the write-up, edited ------------------------------------------------------------


class TestEditedNotes:
    def test_only_lines_still_in_the_notes_stay_yours(self, tmp_path) -> None:
        pad = Notepad(tmp_path)
        pad.write_enhanced("## Summary\n\n- one\n- two\n")
        pad.write_edited_enhanced("## Summary\n\n- one, rewritten\n- two\n", ["- one, rewritten", "- gone"])
        assert pad.edited_lines == ["- one, rewritten"]
        assert "rewritten" in pad.read_enhanced()

    def test_a_fresh_write_up_is_all_the_models_again(self, tmp_path) -> None:
        pad = Notepad(tmp_path)
        pad.write_edited_enhanced("- mine\n", ["- mine"])
        pad.write_enhanced("- theirs\n", template="general")
        assert pad.edited_lines == []
        assert pad.template == "general"


@pytest.fixture
def served(tmp_path: Path):
    recordings = tmp_path / "recordings"
    run = recordings / "run-launch"
    run.mkdir(parents=True)
    with TranscriptStore(run / "transcript.jsonl") as store:
        store.write_meta(MeetingMeta(meet_url="", bot_name="Scribe"))
        store.append(Utterance("Speaker 0", "Where are we on the API?", 1.0, 3.0))
        store.append(Utterance("You", "Two weeks behind.", 4.0, 6.0))
        store.append(Utterance("Speaker 1", "I'll take the migration.", 7.0, 9.0))
    append_event(run / "transcript.jsonl", SPEAKER_NAMES_EVENT, names={"Speaker 1": "Priya"})
    append_event(run / "transcript.jsonl", MIC_EVENT, muted=True, t=10.0)
    append_event(run / "transcript.jsonl", MIC_EVENT, muted=False, t=14.0)
    Notepad(run).write_enhanced("## Summary\n\n- api late\n")

    config = Config(
        meet_url="", output_dir=recordings, deepgram_api_key="dg", anthropic_api_key="sk",
        service_state_dir=tmp_path / "state", preflight_on_start=False,
        call_alerts=False, auto_notes=False,
    )
    from fastapi.testclient import TestClient

    with TestClient(create_app(config)) as client:
        client.app.state.users.add("neil", PASSWORD, Role.ADMIN)
        client.post("/login", data={"username": "neil", "password": PASSWORD})
        job = MeetingJob(
            id="launch", meet_url="", kind="local", title="Launch sync", owner="neil",
            status=JobStatus.FINISHED, created_at=time.time() - 600,
            finished_at=time.time() - 300, output_dir=run,
        )
        client.app.state.manager._jobs[job.id] = job
        yield client


class TestTheRoutes:
    def test_an_edit_and_its_lines_are_kept(self, served) -> None:
        response = served.put(
            "/api/meetings/launch/enhanced",
            json={"enhanced": "## Summary\n\n- the API is two weeks late\n",
                  "mine": ["- the API is two weeks late"]},
        )
        assert response.status_code == 200
        pad = served.get("/api/meetings/launch/notepad").json()
        assert pad["enhanced"].endswith("- the API is two weeks late\n")
        assert pad["mine"] == ["- the API is two weeks late"]

    def test_the_list_says_who_was_there_but_not_you(self, served) -> None:
        listed = {m["id"]: m for m in served.get("/api/meetings").json()["meetings"]}
        assert listed["launch"]["attendees"] == ["Speaker 0", "Priya"]

    def test_the_transcript_carries_the_muted_stretches(self, served) -> None:
        body = served.get("/api/meetings/launch/transcript").json()
        assert body["mutes"] == [{"start": 10.0, "end": 14.0}]

    def test_a_finished_meeting_has_no_countdown_to_cancel(self, served) -> None:
        assert served.get("/api/meetings/launch").json()["auto_stop"] is None
        response = served.post("/api/meetings/launch/auto-stop/cancel")
        assert response.status_code == 200
        assert response.json() == {"cancelled": False}

    def test_cancelling_someone_elses_meeting_is_not_found(self, served) -> None:
        assert served.post("/api/meetings/nope/auto-stop/cancel").status_code == 404

    def test_a_finished_meeting_has_no_levels(self, served) -> None:
        assert served.get("/api/meetings/launch").json()["levels"] == []


class TestTheSetting:
    def test_on_unless_turned_off(self) -> None:
        from meetbot.config import load_config

        assert load_config(env={}).auto_stop is True
        assert load_config(env={"AUTO_STOP": "false"}).auto_stop is False


class TestCitationChips:
    """How a model's citation markers become chips, pinned in the page's code."""

    @staticmethod
    def _ui() -> str:
        from meetbot.service.app import UI_PATH

        return UI_PATH.read_text(encoding="utf-8")

    def test_a_run_of_markers_is_one_chip(self) -> None:
        ui = self._ui()
        assert 'data-ns="${ns.join(",")}"' in ui
        assert '<span class="more">+${ns.length - 1}</span>' in ui

    def test_the_spaces_models_put_before_markers_are_dropped(self) -> None:
        """Real answers put a narrow no-break space before [^1]."""
        assert r"\u202f" in self._ui()
