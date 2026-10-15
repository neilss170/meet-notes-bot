"""Muting your microphone in Scribe while the recording goes on.

Muting in the call app does not reach a local recording, which reads the
microphone itself, so Scribe has a mute of its own. These follow it through
every layer: the audio that goes to the transcriber, the record of it in the
transcript, the replay of that record after a restart, the API, the voice
commands, the icon - and the write-ups, which must read a muted stretch as a
choice rather than as audio that failed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meetbot import tray
from meetbot.analysis.notepad import enhance_notes
from meetbot.analysis.recall import MeetingSource, answer_about_meeting
from meetbot.capture.local import CHANNEL_COUNT, AudioDevice, LocalRecorder, level_of
from meetbot.capture.voice import command_of
from meetbot.local_runner import MicSwitch
from meetbot.runner import MeetingRun
from meetbot.service import jobs as jobs_module
from meetbot.service.app import create_app
from meetbot.service.auth import Role
from meetbot.transcript.format import render_for_llm, render_markdown
from meetbot.transcript.store import (
    MIC_EVENT,
    MicMute,
    TranscriptStore,
    Utterance,
    append_event,
    iter_records,
    read_mic_mutes,
)

np = pytest.importorskip("numpy", reason="numpy is needed to resample audio")

SPEAKERS = AudioDevice(
    index=10, name="Speakers [Loopback]", channels=2, rate=48_000, is_loopback=True
)
MIC = AudioDevice(index=9, name="Microphone", channels=2, rate=48_000, is_loopback=False)
PASSWORD = "a-long-enough-password"
UI = (Path(__file__).parent.parent / "meetbot" / "service" / "ui.html").read_text(
    encoding="utf-8"
)


def tone(hz: float, seconds: float, rate: int = 48_000, channels: int = 2) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    mono = (0.5 * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)
    return np.repeat(mono[:, None], channels, axis=1).ravel().tobytes()


def channels(block: bytes) -> tuple[np.ndarray, np.ndarray]:
    frames = np.frombuffer(block, dtype=np.int16)
    return frames[0::CHANNEL_COUNT], frames[1::CHANNEL_COUNT]


def transcript(tmp_path: Path, *records: tuple) -> Path:
    """A transcript written the way a recording writes one."""
    path = tmp_path / "transcript.jsonl"
    with TranscriptStore(path) as store:
        for record in records:
            if isinstance(record, Utterance):
                store.append(record)
            else:
                kind, details = record
                store.append_event(kind, **details)
    return path


# -- the audio ----------------------------------------------------------------


class TestTheAudio:
    WANT = 1600 * 2

    def test_muting_silences_your_channel_and_only_yours(self) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder._me.callback(tone(440, 0.2), 0, None, 0)
        recorder._them.callback(tone(1000, 0.2), 0, None, 0)
        recorder.mic_muted = True
        me, them = channels(recorder._next_block(self.WANT))
        assert np.abs(me).max() == 0, "your voice reached the transcriber while muted"
        assert np.abs(them).max() > 1000, "muting you silenced the other side"

    def test_a_muted_block_is_still_a_full_block(self) -> None:
        """Silence rather than nothing: the stream stays open and in step."""
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.mic_muted = True
        block = recorder._next_block(self.WANT)
        assert len(block) == self.WANT * CHANNEL_COUNT

    def test_what_was_said_while_muted_is_not_sent_on_unmuting(self) -> None:
        """The microphone is drained while muted, not left to back up."""
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder.mic_muted = True
        recorder._me.callback(tone(440, 1.0), 0, None, 0)
        for _ in range(12):
            recorder._next_block(self.WANT)
        recorder.mic_muted = False
        me, _ = channels(recorder._next_block(self.WANT))
        assert np.abs(me).max() == 0, "muted speech arrived after unmuting"

    def test_the_level_follows_your_voice_and_stops_while_muted(self) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        recorder._me.callback(tone(440, 0.4), 0, None, 0)
        recorder._next_block(self.WANT)
        assert recorder.mic_levels() and max(recorder.mic_levels()) > 0.5
        recorder.mic_muted = True
        recorder._next_block(self.WANT)
        assert recorder.mic_levels() == []

    def test_level_of_silence_is_zero_and_of_a_loud_tone_is_high(self) -> None:
        assert level_of(b"\x00\x00" * 1600) == 0.0
        assert level_of(tone(440, 0.1, 16_000, 1)) > 0.8


class TestTheSwitch:
    def _switch(self, recorder, clock=lambda: 12.345):
        written: list[tuple[str, dict]] = []
        return MicSwitch(recorder, lambda kind, **d: written.append((kind, d)), clock), written

    def test_muting_writes_when_on_the_recording_timeline(self) -> None:
        recorder = LocalRecorder(SPEAKERS, MIC)
        switch, written = self._switch(recorder)
        assert switch.set(True) is True
        assert recorder.mic_muted is True
        assert written == [(MIC_EVENT, {"muted": True, "t": 12.35})]

    def test_asking_for_the_state_it_is_in_writes_nothing(self) -> None:
        switch, written = self._switch(LocalRecorder(SPEAKERS, MIC))
        switch.set(True)
        assert switch.set(True) is False
        assert switch.set(False) is True
        assert [d["muted"] for _, d in written] == [True, False]

    def test_toggle_reports_the_new_state(self) -> None:
        switch, _ = self._switch(LocalRecorder(SPEAKERS, MIC))
        assert switch.toggle() is True
        assert switch.toggle() is False

    def test_a_recording_without_a_microphone_has_nothing_to_mute(self) -> None:
        switch, written = self._switch(LocalRecorder(SPEAKERS, None))
        assert switch.available is False
        assert switch.set(True) is False
        assert written == []


# -- the record of it -----------------------------------------------------------


class TestReplay:
    def test_pairs_become_stretches(self, tmp_path) -> None:
        path = transcript(
            tmp_path,
            (MIC_EVENT, {"muted": True, "t": 10.0}),
            (MIC_EVENT, {"muted": False, "t": 25.0}),
            (MIC_EVENT, {"muted": True, "t": 40.0}),
            (MIC_EVENT, {"muted": False, "t": 41.5}),
        )
        stretches, muted_now = read_mic_mutes(path)
        assert stretches == [MicMute(10.0, 25.0), MicMute(40.0, 41.5)]
        assert muted_now is False

    def test_a_second_mute_does_not_open_a_stretch_inside_the_first(self, tmp_path) -> None:
        path = transcript(
            tmp_path,
            (MIC_EVENT, {"muted": True, "t": 10.0}),
            (MIC_EVENT, {"muted": True, "t": 15.0}),
            (MIC_EVENT, {"muted": False, "t": 20.0}),
            (MIC_EVENT, {"muted": False, "t": 22.0}),
        )
        assert read_mic_mutes(path)[0] == [MicMute(10.0, 20.0)]

    def test_a_mute_open_at_the_end_lasts_until_the_recording_stopped(self, tmp_path) -> None:
        path = transcript(
            tmp_path,
            (MIC_EVENT, {"muted": True, "t": 30.0}),
            ("local_capture_stopped", {"elapsed_s": 95.0}),
        )
        stretches, muted_now = read_mic_mutes(path)
        assert stretches == [MicMute(30.0, 95.0)]
        assert muted_now is True

    def test_a_process_killed_while_muted_closes_at_the_last_thing_heard(
        self, tmp_path
    ) -> None:
        """No stop record is written when the process dies; the file still knows."""
        path = transcript(
            tmp_path,
            (MIC_EVENT, {"muted": True, "t": 30.0}),
            Utterance("Speaker 0", "Still talking.", 50.0, 61.0),
        )
        assert read_mic_mutes(path)[0] == [MicMute(30.0, 61.0)]

    def test_a_restored_meeting_reports_the_state_its_file_left_it_in(self, tmp_path) -> None:
        directory = tmp_path / "recordings" / "20261015-101500-local"
        directory.mkdir(parents=True)
        path = directory / "transcript.jsonl"
        with TranscriptStore(path) as store:
            store.write_meta(
                __import__("meetbot.transcript.store", fromlist=["MeetingMeta"]).MeetingMeta(
                    meet_url="Standup", bot_name="Scribe (local capture)"
                )
            )
            store.append_event(MIC_EVENT, muted=True, t=4.0)
        manager = jobs_module.JobManager(SimpleNamespace())
        assert jobs_module.load_past_runs(manager, tmp_path / "recordings") == 1
        payload = manager.list()[0].to_dict()
        assert payload["mic_muted"] is True
        # Restored means over: there is nothing left to mute.
        assert payload["mic_available"] is False


# -- the write-ups --------------------------------------------------------------


MEETING = [
    Utterance("You", "Let me check with Sam.", 2.0, 4.0),
    Utterance("Speaker 0", "Sure, take your time.", 5.0, 7.0),
    Utterance("Speaker 0", "We can carry on meanwhile.", 15.0, 18.0),
    Utterance("You", "Back now, Friday works.", 30.0, 33.0),
]
STRETCH = [MicMute(8.0, 29.0)]


class TestTranscriptMarkdown:
    def test_a_muted_stretch_is_one_line_where_it_happened(self) -> None:
        markdown = render_markdown(MEETING, mic_muted=STRETCH)
        assert "_— Your mic muted 00:00:08–00:00:29 —_" in markdown
        before, after = markdown.split("Your mic muted", 1)
        assert "Sure, take your time." in before
        assert "We can carry on meanwhile." in after

    def test_no_stretch_no_line(self) -> None:
        assert "mic muted" not in render_markdown(MEETING)

    def test_a_turn_is_not_merged_across_a_mute(self) -> None:
        """The marker must sit between the two things said, not after both."""
        lines = [
            Utterance("Speaker 0", "First.", 1.0, 2.0),
            Utterance("Speaker 0", "Second.", 4.0, 5.0),
        ]
        text = render_for_llm(lines, mic_muted=[MicMute(3.0, 3.5)])
        assert text.index("First.") < text.index("muted") < text.index("Second.")


class TestNotesAndAnswers:
    def test_the_notes_prompt_is_told_the_silence_was_chosen(self, fake_llm) -> None:
        enhance_notes(MEETING, "", fake_llm, mic_muted=STRETCH)
        prompt = fake_llm.text_calls[-1]["user"]
        assert "muted their own microphone during 00:00:08-00:00:29" in prompt
        assert "not as missing or failed audio" in prompt
        assert "muted their own microphone until 00:00:29" in prompt

    def test_notes_without_a_mute_say_nothing_about_one(self, fake_llm) -> None:
        enhance_notes(MEETING, "", fake_llm)
        assert "muted" not in fake_llm.text_calls[-1]["user"]

    def test_a_cited_answer_is_told_too(self, fake_llm) -> None:
        source = MeetingSource(
            id="m1", title="Standup", started_at=0, utterances=MEETING,
            mic_muted=STRETCH,
        )
        answer_about_meeting(source, "What did I say?", fake_llm)
        prompt = fake_llm.text_calls[-1]["user"]
        assert "muted their own microphone during 00:00:08-00:00:29" in prompt


# -- voice ----------------------------------------------------------------------


class TestVoice:
    @pytest.mark.parametrize(
        "said, kind",
        [
            ("scribe mute my mic", "mute"),
            ("Scribe mute my microphone", "mute"),
            ("scribe unmute my mic", "unmute"),
            ("scribe unmute me", "unmute"),
        ],
    )
    def test_the_phrases_are_understood(self, said: str, kind: str) -> None:
        command = command_of(said, 0.95)
        assert command is not None and command.kind == kind


# -- the service ---------------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A running service whose local recordings have a real microphone switch.

    Only the devices are fake: the switch, the recorder it silences and the
    transcript it writes to are the real ones.
    """
    import meetbot.capture.local as local_module
    from meetbot.config import Config

    monkeypatch.setattr(local_module, "capture_available", lambda: (True, "ok"))
    switches: list[MicSwitch] = []

    async def recording(config, *, on_output_dir=None, on_stop=None, on_mic=None,
                        microphone_index=None, **kwargs):
        directory = config.output_dir / f"rec-{len(switches)}"
        directory.mkdir(parents=True, exist_ok=True)
        on_output_dir(directory)
        path = directory / "transcript.jsonl"
        release = asyncio.Event()
        on_stop(release.set)
        started = time.monotonic()
        recorder = LocalRecorder(SPEAKERS, None if microphone_index == -1 else MIC)
        switch = MicSwitch(
            recorder,
            lambda kind, **details: append_event(path, kind, **details),
            lambda: time.monotonic() - started,
        )
        switches.append(switch)
        on_mic(switch)
        await release.wait()
        return MeetingRun(output_dir=directory, transcript_jsonl=path, utterance_count=1)

    monkeypatch.setattr(jobs_module, "run_local_meeting", recording)

    from meetbot.service import app as app_module

    listeners: list = []

    class FakeListener:
        """Stands in for the recogniser, which holds the microphone itself."""

        def __init__(self, on_command, **kwargs) -> None:
            self.on_command = on_command
            listeners.append(self)

        def start(self) -> bool:
            return True

        def stop(self) -> None:
            pass

        listening = True
        detail = "listening"

    monkeypatch.setattr(app_module, "VoiceListener", FakeListener)
    monkeypatch.setattr(app_module, "voice_available", lambda: (True, "ready"))
    config = Config(
        meet_url="",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        service_state_dir=tmp_path / "state",
        preflight_on_start=False,
        call_alerts=False,
        auto_notes=False,
        voice_commands=True,
    )
    config.output_dir.mkdir(parents=True)
    from fastapi.testclient import TestClient

    with TestClient(create_app(config)) as client:
        client.app.state.users.add("neil", PASSWORD, Role.ADMIN)
        client.post("/login", data={"username": "neil", "password": PASSWORD})
        client.listener = listeners[0]
        yield client, switches


def _until(check, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    return None


def _start(client, switches, **body) -> str:
    count = len(switches)
    job_id = client.post("/api/recordings", json={"title": "Standup", **body}).json()["id"]
    assert _until(lambda: len(switches) > count), "the recording never opened"
    return job_id


class TestTheRoute:
    def test_muting_says_so_and_writes_it_down(self, rig) -> None:
        client, switches = rig
        job_id = _start(client, switches)
        before = client.get(f"/api/meetings/{job_id}").json()
        assert before["mic_available"] is True and before["mic_muted"] is False

        response = client.post(f"/api/meetings/{job_id}/mic", json={"muted": True})
        assert response.status_code == 200
        assert response.json()["mic_muted"] is True
        assert client.get(f"/api/meetings/{job_id}").json()["mic_muted"] is True

        path = Path(client.get(f"/api/meetings/{job_id}").json()["output_dir"])
        events = [
            r for r in iter_records(path / "transcript.jsonl")
            if r.get("kind") == MIC_EVENT
        ]
        assert [e["details"]["muted"] for e in events] == [True]
        assert isinstance(events[0]["details"]["t"], float)

        client.post(f"/api/meetings/{job_id}/mic", json={"muted": False})
        assert client.get(f"/api/meetings/{job_id}").json()["mic_muted"] is False

    def test_the_state_outlives_the_recording_through_its_file(self, rig) -> None:
        client, switches = rig
        job_id = _start(client, switches)
        client.post(f"/api/meetings/{job_id}/mic", json={"muted": True})
        client.post(f"/api/meetings/{job_id}/stop")
        finished = _until(
            lambda: (lambda m: m if m["status"] == "finished" else None)(
                client.get(f"/api/meetings/{job_id}").json()
            )
        )
        assert finished is not None
        assert finished["mic_muted"] is True, "the replay forgot the mute"
        assert finished["mic_available"] is False
        refused = client.post(f"/api/meetings/{job_id}/mic", json={"muted": False})
        assert refused.status_code == 409

    def test_a_bot_meeting_has_no_microphone_of_yours(self, rig) -> None:
        client, _ = rig
        manager = client.app.state.manager
        job = jobs_module.MeetingJob(
            id="bot1", meet_url="https://meet.google.com/abc-defg-hij",
            owner="neil", status=jobs_module.JobStatus.IN_CALL,
        )
        manager._jobs[job.id] = job
        response = client.post("/api/meetings/bot1/mic", json={"muted": True})
        assert response.status_code == 409
        assert "bot hears the call" in response.json()["detail"]
        assert client.get("/api/meetings/bot1").json()["mic_available"] is False

    def test_a_recording_without_a_microphone_refuses_with_a_reason(self, rig) -> None:
        client, switches = rig
        job_id = _start(client, switches, microphone_index=-1)
        response = client.post(f"/api/meetings/{job_id}/mic", json={"muted": True})
        assert response.status_code == 409
        assert "not capturing a microphone" in response.json()["detail"]

    def test_someone_elses_recording_is_404(self, rig) -> None:
        client, switches = rig
        job_id = _start(client, switches)
        client.app.state.users.add("priya", PASSWORD, Role.MEMBER)
        client.cookies.clear()
        client.post("/login", data={"username": "priya", "password": PASSWORD})
        response = client.post(f"/api/meetings/{job_id}/mic", json={"muted": True})
        assert response.status_code == 404

    def test_a_stranger_is_refused(self, rig) -> None:
        client, switches = rig
        job_id = _start(client, switches)
        client.cookies.clear()
        response = client.post(f"/api/meetings/{job_id}/mic", json={"muted": True})
        assert response.status_code == 401

    def test_saying_unmute_works_while_muted(self, rig) -> None:
        """The recogniser hears the room itself; muting the recording leaves it."""
        from meetbot.capture.voice import VoiceCommand

        client, switches = rig
        job_id = _start(client, switches)
        muted = lambda: client.get(f"/api/meetings/{job_id}").json()["mic_muted"]
        client.listener.on_command(VoiceCommand("mute", "scribe mute my mic", 0.95))
        assert _until(muted) is True, "saying mute did nothing"
        client.listener.on_command(VoiceCommand("unmute", "scribe unmute my mic", 0.95))
        assert _until(lambda: muted() is False) is True, "saying unmute did nothing"


# -- the icon -------------------------------------------------------------------


def _live(muted: bool, available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id="j1", title="Standup", meet_url="Standup", kind="local",
        status=SimpleNamespace(is_terminal=False), elapsed_s=252.0,
        mic_available=available, mic_muted=muted,
    )


def _app(*jobs, loop=None) -> SimpleNamespace:
    calls: list = []

    class Manager:
        def list(self):
            return list(jobs)

        def set_mic(self, job_id, muted=None):
            calls.append((job_id, muted))
            jobs[0].mic_muted = not jobs[0].mic_muted if muted is None else muted
            return jobs[0].mic_muted

    return SimpleNamespace(
        state=SimpleNamespace(manager=Manager(), users=None, in_flight=set(), loop=loop)
    ), calls


class TestTheIcon:
    def test_the_hover_text_says_you_are_muted(self) -> None:
        app, _ = _app(_live(True))
        status = tray.read_status(app)
        assert tray.tooltip(status) == "Scribe — Recording · your mic is muted · 4:12"

    def test_the_menu_offers_the_opposite_of_the_state(self) -> None:
        for muted, label in ((False, "Mute my mic"), (True, "Unmute my mic")):
            app, _ = _app(_live(muted))
            labels = [i.label for i in tray.menu_for(tray.read_status(app))]
            assert label in labels

    def test_no_microphone_no_item(self) -> None:
        app, _ = _app(_live(False, available=False))
        labels = [i.label for i in tray.menu_for(tray.read_status(app))]
        assert not any("mic" in label for label in labels)

    def test_the_item_and_the_shortcut_toggle_through_the_server_loop(self) -> None:
        loop = asyncio.new_event_loop()
        import threading

        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            live = _live(False)
            app, calls = _app(live, loop=loop)
            icon = tray.ScribeTray(app, url="http://x", on_quit=lambda: None)
            icon.invoke("mic")
            assert live.mic_muted is True
            icon.mic_shortcut()
            assert live.mic_muted is False
            assert calls == [("j1", None), ("j1", None)]
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(2)
            loop.close()

    def test_the_shortcut_does_nothing_when_nothing_is_recording(self) -> None:
        app, calls = _app()
        icon = tray.ScribeTray(app, url="http://x", on_quit=lambda: None)
        said: list[str] = []
        icon.notify = said.append
        icon.mic_shortcut()
        assert calls == [] and said == []

    def test_the_muted_icon_is_not_the_recording_icon(self) -> None:
        from meetbot.desktop import spark_image

        pytest.importorskip("PIL")
        red = spark_image(32, recording=True)
        amber = spark_image(32, recording=True, muted=True)
        assert red.tobytes() != amber.tobytes()


# -- the page -------------------------------------------------------------------


class TestThePage:
    def test_the_mic_button_sits_before_stop_and_is_not_the_accent(self) -> None:
        assert UI.index("? micButtonHtml() +") < UI.index('id="stop"')
        assert 'class="btn sm ghost mic"' in UI

    def test_the_recording_pill_wears_an_amber_badge_while_muted(self) -> None:
        assert 'id="rp-badge"' in UI
        assert '$("rp-badge").hidden = !muted' in UI

    def test_unmuting_from_the_banner_and_the_button_use_the_route(self) -> None:
        assert "/api/meetings/${job.id}/mic" in UI
        assert "Scribe isn’t hearing you" in UI

    def test_the_tooltip_only_offers_ways_back_that_work(self) -> None:
        """A shortcut only the icon registers, and a phrase only an open mic hears."""
        assert "if (job.mic_shortcut)" in UI
        assert "if (state.voice.listening)" in UI
