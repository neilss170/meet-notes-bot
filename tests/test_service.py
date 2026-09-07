"""The local web service: sending the bot without a terminal.

The runner itself is stubbed throughout - these tests are about the job
lifecycle, the HTTP surface, and the things a long-lived multi-meeting
process needs that a one-shot CLI never did.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.runner import MeetingRun
from meetbot.service import jobs as jobs_module
from meetbot.service.app import create_app
from meetbot.service.jobs import JobManager, JobStatus, free_port, load_past_runs

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

MEET_URL = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def service_config(tmp_path: Path) -> Config:
    return Config(
        meet_url="",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
    )


def _stub_runner(monkeypatch, *, utterances=1, errors=None, block=False, boom=None):
    """Replace ``run_meeting`` with something fast and controllable."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_meeting(config, *, on_stop=None, on_output_dir=None, **kwargs):
        output_dir = config.output_dir / "run"
        output_dir.mkdir(parents=True, exist_ok=True)
        if on_output_dir is not None:
            on_output_dir(output_dir)
        if on_stop is not None:
            on_stop(release.set)
        started.set()
        if boom is not None:
            raise boom
        if block:
            await release.wait()
        return MeetingRun(
            output_dir=output_dir,
            transcript_jsonl=output_dir / "transcript.jsonl",
            end_reason=None if utterances == 0 else __import__(
                "meetbot.join", fromlist=["EndReason"]
            ).EndReason.MEETING_ENDED,
            utterance_count=utterances,
            errors=list(errors or []),
        )

    monkeypatch.setattr(jobs_module, "run_meeting", fake_run_meeting)
    return started, release


class TestFreePort:
    def test_returns_a_usable_port(self) -> None:
        assert 1024 < free_port() <= 65535

    def test_successive_calls_differ(self) -> None:
        """Concurrent meetings must not collide on the audio bridge port.

        A fixed port fails deterministically on the second simultaneous
        meeting, which is the whole reason this exists.
        """
        ports = {free_port() for _ in range(8)}
        assert len(ports) > 1


class TestJobManager:
    async def test_start_runs_a_meeting_to_completion(
        self, service_config, monkeypatch
    ) -> None:
        _stub_runner(monkeypatch, utterances=3)
        manager = JobManager(service_config)
        job = manager.start(MEET_URL)
        await job._task
        assert job.status is JobStatus.FINISHED
        assert job.utterance_count == 3

    async def test_a_run_with_no_audio_is_failed_not_finished(
        self, service_config, monkeypatch
    ) -> None:
        _stub_runner(monkeypatch, utterances=0)
        manager = JobManager(service_config)
        job = manager.start(MEET_URL)
        await job._task
        assert job.status is JobStatus.FAILED

    async def test_a_crashing_run_does_not_take_down_the_manager(
        self, service_config, monkeypatch
    ) -> None:
        """One bad meeting must not kill a server that is running others."""
        _stub_runner(monkeypatch, boom=RuntimeError("chromium exploded"))
        manager = JobManager(service_config)
        job = manager.start(MEET_URL)
        await job._task
        assert job.status is JobStatus.FAILED
        assert any("chromium exploded" in e for e in job.errors)
        # The manager still works afterwards.
        assert manager.get(job.id) is job

    async def test_rejects_a_bad_url_at_request_time(self, service_config) -> None:
        """A bad link is a rejected request, not a job that fails later."""
        manager = JobManager(service_config)
        with pytest.raises(ValueError):
            manager.start("https://zoom.us/j/123")
        assert manager.list() == []

    async def test_stop_asks_the_running_meeting_to_finish(
        self, service_config, monkeypatch
    ) -> None:
        started, _ = _stub_runner(monkeypatch, block=True)
        manager = JobManager(service_config)
        job = manager.start(MEET_URL)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await manager.stop(job.id) is True
        await asyncio.wait_for(job._task, timeout=2)
        assert job.status is JobStatus.FINISHED

    async def test_stopping_an_unknown_or_finished_job_is_false(
        self, service_config, monkeypatch
    ) -> None:
        _stub_runner(monkeypatch)
        manager = JobManager(service_config)
        assert await manager.stop("nope") is False
        job = manager.start(MEET_URL)
        await job._task
        assert await manager.stop(job.id) is False

    async def test_concurrent_meetings_get_distinct_ports(
        self, service_config, monkeypatch
    ) -> None:
        ports: list[int] = []

        async def capture(config, **kwargs):
            ports.append(config.bridge_port)
            return MeetingRun(
                output_dir=config.output_dir,
                transcript_jsonl=config.output_dir / "t.jsonl",
                utterance_count=1,
            )

        monkeypatch.setattr(jobs_module, "run_meeting", capture)
        manager = JobManager(service_config)
        tasks = [manager.start(MEET_URL)._task for _ in range(3)]
        await asyncio.gather(*tasks)
        assert len(set(ports)) == 3, f"ports collided: {ports}"

    async def test_shutdown_stops_everything_still_running(
        self, service_config, monkeypatch
    ) -> None:
        """A bot left behind sits in someone's meeting until they remove it."""
        started, _ = _stub_runner(monkeypatch, block=True)
        manager = JobManager(service_config)
        manager.start(MEET_URL)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert manager.active_count == 1
        await asyncio.wait_for(manager.shutdown(), timeout=3)
        assert manager.active_count == 0

    async def test_overrides_reach_the_config(
        self, service_config, monkeypatch
    ) -> None:
        seen: list[Config] = []

        async def capture(config, **kwargs):
            seen.append(config)
            return MeetingRun(
                output_dir=config.output_dir,
                transcript_jsonl=config.output_dir / "t.jsonl",
                utterance_count=1,
            )

        monkeypatch.setattr(jobs_module, "run_meeting", capture)
        manager = JobManager(service_config)
        await manager.start(MEET_URL, anonymise_analysis=True)._task
        assert seen[0].anonymise_analysis is True
        assert seen[0].meet_url == MEET_URL


class TestProgress:
    def test_reads_live_progress_from_the_transcript_store(
        self, service_config, tmp_path
    ) -> None:
        """Progress comes from the append-only store, not extra plumbing."""
        from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        store = TranscriptStore(run_dir / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url=MEET_URL, bot_name="bot"))
        store.append(Utterance("Neil Sharma", "Morning all.", 0.0, 2.0))
        store.append(Utterance("Priya", "Morning.", 2.5, 3.0))
        store.append_event("joined")
        store.close()

        job = jobs_module.MeetingJob(id="x", meet_url=MEET_URL, output_dir=run_dir)
        progress = job.read_progress()
        assert progress["utterance_count"] == 2
        assert progress["speakers"] == ["Neil Sharma", "Priya"]
        assert progress["transcript"][0]["text"] == "Morning all."

    def test_progress_on_a_missing_directory_is_empty_not_an_error(self) -> None:
        job = jobs_module.MeetingJob(id="x", meet_url=MEET_URL)
        assert job.read_progress()["utterance_count"] == 0

    def test_a_truncated_final_line_does_not_break_a_status_poll(
        self, tmp_path
    ) -> None:
        """The normal state of a file being appended to."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "transcript.jsonl").write_text(
            '{"type":"utterance","speaker":"A","text":"hi","start":0}\n{"type":"utter',
            encoding="utf-8",
        )
        job = jobs_module.MeetingJob(id="x", meet_url=MEET_URL, output_dir=run_dir)
        assert job.read_progress()["utterance_count"] == 1


class TestHttpApi:
    @pytest.fixture
    def client(self, service_config, monkeypatch):
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            yield client

    def test_serves_the_ui(self, client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "meetbot" in response.text

    def test_config_endpoint_exposes_no_secrets(self, client) -> None:
        payload = client.get("/api/config").json()
        blob = str(payload).lower()
        assert "api_key" not in blob
        assert "dg" not in payload.get("llm_model", "")

    def test_start_list_and_fetch_a_meeting(self, client) -> None:
        created = client.post("/api/meetings", json={"meet_url": MEET_URL})
        assert created.status_code == 201
        job_id = created.json()["id"]

        listed = client.get("/api/meetings").json()
        assert any(m["id"] == job_id for m in listed["meetings"])
        assert client.get(f"/api/meetings/{job_id}").status_code == 200

    def test_a_bad_url_is_rejected_with_400(self, client) -> None:
        response = client.post("/api/meetings", json={"meet_url": "https://zoom.us/j/1"})
        assert response.status_code == 400
        assert "meet" in response.json()["detail"].lower()

    def test_unknown_meeting_is_404(self, client) -> None:
        assert client.get("/api/meetings/nope").status_code == 404
        assert client.post("/api/meetings/nope/stop").status_code == 404

    @pytest.mark.parametrize(
        "name",
        ["../../../.env", "..%2F.env", "secrets.txt", "config.py", "/etc/passwd"],
    )
    def test_artifact_names_are_an_allow_list_not_a_path(self, client, name) -> None:
        """The artifact name comes from the client; it must not address files.

        Joining it onto the output directory would let a caller read anything
        the process can - starting with the .env holding the API keys.
        """
        created = client.post("/api/meetings", json={"meet_url": MEET_URL})
        job_id = created.json()["id"]
        response = client.get(f"/api/meetings/{job_id}/artifact/{name}")
        # A name containing a slash does not match the route at all, so it is
        # refused by routing before the handler ever sees it; a slash-free
        # name reaches the handler and misses the allow-list. Both are 404,
        # and neither returns file content - that is the property that matters.
        assert response.status_code == 404
        assert "DEEPGRAM" not in response.text
        assert "api_key" not in response.text.lower()

    def test_a_known_artifact_that_does_not_exist_yet_is_404(self, client) -> None:
        created = client.post("/api/meetings", json={"meet_url": MEET_URL})
        job_id = created.json()["id"]
        response = client.get(f"/api/meetings/{job_id}/artifact/analysis.md")
        assert response.status_code == 404


class TestPastRuns:
    def test_completed_runs_on_disk_are_restored(self, service_config, tmp_path) -> None:
        """Restarting the server must not look like it erased history."""
        from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

        recordings = service_config.output_dir
        run_dir = recordings / "20260101-101010-abc-defg-hij"
        run_dir.mkdir(parents=True)
        store = TranscriptStore(run_dir / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url=MEET_URL, bot_name="bot"))
        store.append(Utterance("Neil Sharma", "Hello.", 0.0, 1.0))
        store.close()

        manager = JobManager(service_config)
        assert load_past_runs(manager, recordings) == 1
        restored = manager.list()[0]
        assert restored.status is JobStatus.FINISHED
        assert restored.meet_url == MEET_URL
        assert restored.utterance_count == 1

    def test_directories_without_a_transcript_are_skipped(
        self, service_config
    ) -> None:
        (service_config.output_dir / "empty-run").mkdir(parents=True)
        manager = JobManager(service_config)
        assert load_past_runs(manager, service_config.output_dir) == 0

    def test_a_missing_output_directory_is_not_an_error(self, service_config) -> None:
        manager = JobManager(service_config)
        assert load_past_runs(manager, service_config.output_dir / "nope") == 0


class TestStopFeedback:
    """Stopping is not instant, so the UI must not look dead while it works.

    Measured on a live call: ~11s from the stop request to analysis.md being
    written - the bot has to notice, leave, flush transcription and summarise.
    """

    def test_stop_marks_the_job_immediately(self, service_config, monkeypatch) -> None:
        """The status flips on the request, not when the run finishes."""
        started, _ = _stub_runner(monkeypatch, block=True)

        async def scenario() -> None:
            manager = JobManager(service_config)
            job = manager.start(MEET_URL)
            await asyncio.wait_for(started.wait(), timeout=2)
            await manager.stop(job.id)
            # Observable straight away, before the run has unwound.
            assert job.status is JobStatus.STOPPING
            assert not job.status.is_terminal
            await asyncio.wait_for(job._task, timeout=3)

        asyncio.run(scenario())

    def test_the_ui_acknowledges_the_click_before_the_next_poll(self) -> None:
        """The button disables itself rather than waiting 2s to re-render."""
        from meetbot.service.app import UI_PATH

        ui = UI_PATH.read_text(encoding="utf-8")
        assert "const stopping = new Set()" in ui
        assert "button.disabled = true" in ui
        assert "stopping.delete(job.id)" in ui, "stopped jobs must clear the flag"
