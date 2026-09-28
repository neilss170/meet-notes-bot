"""The local web service: sending the bot without a terminal.

The runner itself is stubbed throughout - these tests are about the job
lifecycle, the HTTP surface, and the things a long-lived multi-meeting
process needs that a one-shot CLI never did.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from pathlib import Path

import pytest

from meetbot.config import Config
from meetbot.runner import MeetingRun
from meetbot.service import jobs as jobs_module
from meetbot.service.app import create_app
from meetbot.service.auth import SESSION_COOKIE, Role, issue_session
from meetbot.service.jobs import JobManager, JobStatus, free_port, load_past_runs

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

MEET_URL = "https://meet.google.com/abc-defg-hij"
ADMIN_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def no_real_session_check(monkeypatch):
    """Keep the whole module off Playwright.

    Sending a bot re-checks the Google session live rather than trusting a
    cached report - which means a real browser launch. Every test here stubs
    the runner for exactly the same reason, so the check is stubbed too.
    Tests that care about the guard replace this with their own answer.
    """
    from meetbot.preflight import CheckResult
    from meetbot.service import app as app_module

    async def signed_in(_config):
        return CheckResult(
            "Google sign-in", ok=True, detail="stubbed for tests", mode="bot"
        )

    monkeypatch.setattr(app_module, "check_google_session", signed_in)


@pytest.fixture
def service_config(tmp_path: Path) -> Config:
    return Config(
        meet_url="",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        # Per-test, or the suite would read and write the real ~/.meetbot -
        # and bootstrap an admin into the developer's own account database.
        service_state_dir=tmp_path / "state",
        # The startup checks open a real Deepgram stream and call the LLM.
        # Tests that reach the network are slow, flaky, and fail on CI for
        # reasons that say nothing about the code; the readiness behaviour
        # is covered by setting app.state.health directly instead.
        preflight_on_start=False,
        # The watcher reads the real microphone state and raises Windows
        # notifications; a suite run during a call must do neither.
        call_alerts=False,
        # Writing a meeting up as it ends would mean a real LLM call for every
        # stubbed meeting in this file. The tests about that turn it back on.
        auto_notes=False,
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

    async def test_a_finished_meeting_is_announced_once(
        self, service_config, monkeypatch
    ) -> None:
        """The service writes the notes off the back of this, so it must fire."""
        _stub_runner(monkeypatch, utterances=2)
        seen = []
        manager = JobManager(service_config, on_finished=seen.append)
        job = manager.start(MEET_URL)
        await job._task
        assert [j.id for j in seen] == [job.id]
        assert seen[0].status is JobStatus.FINISHED

    async def test_a_listener_that_explodes_does_not_fail_the_meeting(
        self, service_config, monkeypatch
    ) -> None:
        """The recording is already on disk; a bad listener must not undo that."""
        _stub_runner(monkeypatch, utterances=2)

        def boom(_job) -> None:
            raise RuntimeError("writing the notes blew up")

        manager = JobManager(service_config, on_finished=boom)
        job = manager.start(MEET_URL)
        await job._task
        assert job.status is JobStatus.FINISHED

    async def test_runs_restored_from_disk_are_not_announced(
        self, service_config, tmp_path
    ) -> None:
        """Otherwise every startup would write up every recording on disk."""
        seen = []
        manager = JobManager(service_config, on_finished=seen.append)
        directory = tmp_path / "old-recordings" / "20260101-standup"
        directory.mkdir(parents=True)
        (directory / "transcript.jsonl").write_text(
            '{"type": "meta", "meet_url": "' + MEET_URL + '"}\n', encoding="utf-8"
        )
        assert load_past_runs(manager, tmp_path / "old-recordings") == 1
        assert seen == [], "a meeting that finished before we started is not news"

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
    def anonymous(self, service_config, monkeypatch):
        """A client with no session, for the guards."""
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            yield client

    @pytest.fixture
    def client(self, anonymous):
        """Signed in as an admin - the state most of these tests assume."""
        anonymous.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        response = anonymous.post(
            "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200, "sign-in failed"
        return anonymous

    @pytest.fixture
    def member(self, client):
        """A second, non-admin client against the same running app."""
        client.app.state.users.add("priya", ADMIN_PASSWORD, Role.MEMBER)
        other = TestClient(client.app)
        other.post("/login", data={"username": "priya", "password": ADMIN_PASSWORD})
        return other

    def test_serves_the_ui(self, client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert 'id="notepad"' in response.text

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
        assert "stopping: new Set()" in ui
        assert "state.stopping.add(jobId)" in ui
        assert 'state.stopping.has(job.id) ? " disabled" : ""' in ui
        assert "state.stopping.delete(job.id)" in ui, "stopped jobs clear the flag"


class TestUiRendering:
    """Bugs reported from real use of the UI, kept fixed.

    All three shared one root cause: the meetings list was rebuilt with
    innerHTML on every 2-second poll.
    """

    @staticmethod
    def _ui() -> str:
        from meetbot.service.app import UI_PATH

        return UI_PATH.read_text(encoding="utf-8")

    def test_the_list_is_not_rebuilt_when_nothing_changed(self) -> None:
        """Rebuilding on a timer threw away the reader's scroll position."""
        ui = self._ui()
        assert "state.sig" in ui
        assert "if (signature === state.sig.list) return;" in ui

    def test_scroll_position_is_preserved_across_a_repaint(self) -> None:
        """The transcript follows a live call without yanking the page away
        from somebody who has scrolled up to re-read something."""
        ui = self._ui()
        assert "atBottom" in ui
        assert "if (atBottom) pane.scrollTop = pane.scrollHeight;" in ui
        assert "if (pane) pane.scrollTop = scroll;" in ui

    def test_documents_are_held_as_state_not_refetched_per_render(self) -> None:
        """Re-fetching on each render raced the poll loop.

        A slower earlier fetch could land after a newer one and paint the
        previous document, which is why Transcript and AI notes appeared to
        show the same content. Everything on screen now comes from state
        that one fetch per poll replaces wholesale.
        """
        ui = self._ui()
        assert "state.pad" in ui
        assert "state.detail" in ui
        assert "state.sig.enhanced" in ui

    def test_the_poll_loop_never_overwrites_the_open_notepad(self) -> None:
        """The notepad is the one control the user types into.

        Refilling it from a 2-second poll - or rebuilding the pane on the
        elapsed clock, which recreates the textarea - deletes whatever was
        being written. Both paths are guarded.
        """
        ui = self._ui()
        # Loaded once per meeting, then client-owned.
        assert "padLoadedFor" in ui
        assert "state.padLoadedFor !== state.selected" in ui
        # The poll keeps the local copy while it is being edited.
        assert 'state.saveState === "editing"' in ui
        # The pane rebuild signature must not contain the volatile figures.
        # Read renderMain's own: the sidebar builds a signature too, and that
        # one is allowed to move with the counts - it has nothing to type in.
        main = ui[ui.index("function renderMain("):]
        head = main[main.index("const signature = JSON.stringify(["):]
        head = head[: head.index("]);")]
        assert "elapsed_s" not in head, "the elapsed clock must not rebuild the pane"
        assert "utterance_count" not in head, "the count must not rebuild the pane"

    def test_unsaved_notes_are_flushed_when_leaving_them(self) -> None:
        ui = self._ui()
        assert "function flushNotes" in ui
        assert "beforeunload" in ui
        assert "keepalive: true" in ui

    def test_artifacts_render_as_structure_rather_than_preformatted_text(
        self,
    ) -> None:
        ui = self._ui()
        assert "function renderMarkdown" in ui
        for token in ("md-h", "md-list", "md-task", "md-turn"):
            assert token in ui, token

    def test_a_meeting_that_vanished_is_let_go(self) -> None:
        """After a restart a live job's id is gone; the page must not chase it.

        Seen for real: a tab left open across a restart asked for the old id
        every two seconds, forever, and got a 404 every time.
        """
        ui = self._ui()
        assert "!state.meetings.some((j) => j.id === state.selected)" in ui

    def test_the_hidden_attribute_always_hides(self) -> None:
        """A class that sets display beats the browser's own [hidden] rule.

        Buttons became inline-flex, after which "Clear" and the admin-only
        "Accounts" stayed on screen with hidden set.
        """
        ui = self._ui()
        assert "[hidden] { display: none !important; }" in ui


class TestMarkdownRendering:
    """The renderer must cover what THIS project actually writes.

    Both misses below were visible in the UI: analysis.md ends with an
    italic "no action items" line and an italic generator footnote, and both
    showed their raw underscores.
    """

    @staticmethod
    def _ui() -> str:
        from meetbot.service.app import UI_PATH

        return UI_PATH.read_text(encoding="utf-8")

    def test_italics_are_rendered(self) -> None:
        ui = self._ui()
        assert "<em>" in ui, "underscore italics were showing raw"

    def test_bold_is_applied_before_italics(self) -> None:
        """Otherwise the ** in **bold** is consumed as two italic markers."""
        ui = self._ui()
        bold = ui.index("<strong>$1</strong>")
        italic = ui.index("<em>$2</em>")
        assert bold < italic

    def test_wrapped_prose_is_one_paragraph(self) -> None:
        """A model wraps its prose across source lines. That is one paragraph.

        Emitting a <p> per source line instead put a paragraph break inside
        every wrapped sentence. It read as broken leading rather than as a
        bug, and only became obvious once the notes had a capped measure to
        wrap in - so it is pinned here rather than left to the eye.
        """
        ui = self._ui()
        assert "const closePara" in ui
        assert "para.push(inline(line.trim()));" in ui
        assert "para.join(\" \")" in ui, "lines must be joined, not concatenated"

    def test_snake_case_is_protected_from_italics(self) -> None:
        """file_name_like_this and openai/gpt-oss-120b must survive.

        A naive /_(.+?)_/ turns any two underscores in a line into italics,
        which mangles identifiers and model ids. The rule is guarded by
        lookarounds on both sides.
        """
        ui = self._ui()
        assert "(?!\s)" in ui, "no non-space guard on the italic rule"
        assert "(?![\w_])" in ui, "no trailing word-character guard"

    def test_a_horizontal_rule_separates_the_footnote(self) -> None:
        ui = self._ui()
        assert "md-rule" in ui
        assert "md-note" in ui


class _SignedIn:
    """The shared test client, acting as one signed-in user."""

    def __init__(self, client, token: str) -> None:
        self._client = client
        self._token = token

    def request(self, method: str, path: str, **kwargs):
        self._client.cookies.clear()
        self._client.cookies.set(SESSION_COOKIE, self._token)
        return self._client.request(method, path, **kwargs)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)


class TestAccessControl:
    """Who can reach what.

    The service joins meetings and serves transcripts of them, so these are
    the tests that decide whether it is safe to put on a network at all.
    Most of them assert a refusal.
    """

    @pytest.fixture
    def anonymous(self, service_config, monkeypatch):
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            yield client

    @staticmethod
    def _sign_in(client, username, role=Role.MEMBER):
        """Create an account, sign it in, and return a view acting as it.

        Returns a wrapper rather than a second TestClient: each TestClient
        runs its own event loop, and tearing the outer one down while a
        meeting task lives on another raises "Event loop is closed".
        """
        client.app.state.users.add(username, ADMIN_PASSWORD, role)
        response = client.post(
            "/login",
            data={"username": username, "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303, f"could not sign in as {username}"
        token = response.cookies[SESSION_COOKIE]
        client.cookies.clear()   # the base client stays anonymous
        return _SignedIn(client, token)

    # -- signed out --------------------------------------------------------

    def test_the_page_redirects_to_the_login_form(self, anonymous) -> None:
        response = anonymous.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_login_form_is_reachable_signed_out(self, anonymous) -> None:
        assert anonymous.get("/login").status_code == 200

    @pytest.mark.parametrize(
        "method, path",
        [
            ("get", "/api/me"),
            ("get", "/api/config"),
            ("get", "/api/meetings"),
            ("post", "/api/meetings"),
            ("get", "/api/meetings/anything"),
            ("post", "/api/meetings/anything/stop"),
            ("get", "/api/meetings/anything/artifact/transcript.md"),
            ("get", "/api/users"),
            ("post", "/api/users"),
        ],
    )
    def test_every_api_route_refuses_a_stranger(
        self, anonymous, method: str, path: str
    ) -> None:
        response = anonymous.request(method, path, json={})
        assert response.status_code == 401, f"{method.upper()} {path} was not guarded"

    def test_a_wrong_password_sets_no_session(self, anonymous) -> None:
        anonymous.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        response = anonymous.post(
            "/login", data={"username": "neil", "password": "wrong"}
        )
        assert response.status_code == 401
        assert SESSION_COOKIE not in response.cookies
        assert anonymous.get("/api/me").status_code == 401

    def test_the_login_form_does_not_reveal_who_exists(self, anonymous) -> None:
        """The same answer for a wrong password and a missing account."""
        anonymous.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        wrong = anonymous.post("/login", data={"username": "neil", "password": "no"})
        missing = anonymous.post("/login", data={"username": "ghost", "password": "no"})
        assert wrong.status_code == missing.status_code == 401
        assert wrong.text == missing.text

    def test_a_forged_cookie_is_not_a_session(self, anonymous) -> None:
        anonymous.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        anonymous.cookies.set(SESSION_COOKIE, "bXkuYWRtaW4.99999999999.deadbeef")
        assert anonymous.get("/api/me").status_code == 401

    def test_a_session_signed_with_another_key_is_refused(self, anonymous) -> None:
        anonymous.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        anonymous.cookies.set(SESSION_COOKIE, issue_session("neil", b"x" * 48))
        assert anonymous.get("/api/me").status_code == 401

    # -- signed in ---------------------------------------------------------

    def test_signing_in_then_out(self, anonymous) -> None:
        anonymous.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        anonymous.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
        assert anonymous.get("/api/me").json()["username"] == "neil"
        anonymous.post("/logout")
        assert anonymous.get("/api/me").status_code == 401

    def test_deleting_an_account_ends_its_session_at_once(self, anonymous) -> None:
        """A revoked account must not keep working until its cookie expires."""
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        victim = self._sign_in(anonymous, "priya", Role.MEMBER)
        assert victim.get("/api/me").status_code == 200
        assert admin.delete("/api/users/priya").status_code == 200
        assert victim.get("/api/me").status_code == 401

    def test_demoting_an_admin_takes_effect_at_once(self, anonymous) -> None:
        self._sign_in(anonymous, "neil", Role.ADMIN)
        other = self._sign_in(anonymous, "priya", Role.ADMIN)
        assert other.get("/api/users").status_code == 200
        anonymous.app.state.users.set_role("priya", Role.MEMBER)
        assert other.get("/api/users").status_code == 403

    # -- one member must not see another one's meetings --------------------

    def test_a_member_sees_only_their_own_meetings(self, anonymous) -> None:
        neil = self._sign_in(anonymous, "neil", Role.MEMBER)
        priya = self._sign_in(anonymous, "priya", Role.MEMBER)
        job_id = neil.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]

        listed = neil.get("/api/meetings").json()["meetings"]
        assert [m["id"] for m in listed] == [job_id]
        assert priya.get("/api/meetings").json()["meetings"] == []

    def test_another_members_meeting_is_404_not_403(self, anonymous) -> None:
        """403 would confirm the id exists, which is itself a leak."""
        neil = self._sign_in(anonymous, "neil", Role.MEMBER)
        priya = self._sign_in(anonymous, "priya", Role.MEMBER)
        job_id = neil.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]

        assert priya.get(f"/api/meetings/{job_id}").status_code == 404
        assert priya.post(f"/api/meetings/{job_id}/stop").status_code == 404
        artifact = priya.get(f"/api/meetings/{job_id}/artifact/transcript.md")
        assert artifact.status_code == 404

    def test_an_admin_sees_every_meeting(self, anonymous) -> None:
        member = self._sign_in(anonymous, "priya", Role.MEMBER)
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        job_id = member.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]

        listed = admin.get("/api/meetings").json()["meetings"]
        assert any(m["id"] == job_id for m in listed)
        assert admin.get(f"/api/meetings/{job_id}").status_code == 200

    def test_a_started_meeting_records_who_sent_it(self, anonymous) -> None:
        neil = self._sign_in(anonymous, "neil", Role.MEMBER)
        created = neil.post("/api/meetings", json={"meet_url": MEET_URL})
        assert created.json()["owner"] == "neil"

    # -- account management is admin-only ----------------------------------

    @pytest.mark.parametrize(
        "method, path",
        [
            ("get", "/api/users"),
            ("post", "/api/users"),
            ("post", "/api/users/someone/password"),
            ("delete", "/api/users/someone"),
        ],
    )
    def test_members_cannot_manage_accounts(
        self, anonymous, method: str, path: str
    ) -> None:
        member = self._sign_in(anonymous, "priya", Role.MEMBER)
        response = member.request(method, path, json={"password": ADMIN_PASSWORD})
        assert response.status_code == 403

    def test_an_admin_can_add_and_remove_accounts(self, anonymous) -> None:
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        created = admin.post(
            "/api/users",
            json={"username": "amit", "password": ADMIN_PASSWORD, "role": "member"},
        )
        assert created.status_code == 201
        assert created.json()["role"] == "member"
        assert "password" not in created.text

        names = {u["username"] for u in admin.get("/api/users").json()["users"]}
        # "admin" is the account the service bootstraps on an empty database.
        assert {"neil", "amit"} <= names
        assert admin.delete("/api/users/amit").status_code == 200

    def test_a_weak_password_is_refused_with_a_reason(self, anonymous) -> None:
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        response = admin.post(
            "/api/users", json={"username": "amit", "password": "short"}
        )
        assert response.status_code == 400
        assert "8 characters" in response.json()["detail"]

    def test_an_admin_cannot_delete_themselves(self, anonymous) -> None:
        """Removing the account mid-request can strand the last admin."""
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        response = admin.delete("/api/users/neil")
        assert response.status_code == 400
        assert "your own account" in response.json()["detail"]

    def test_the_last_admin_cannot_be_removed(self, anonymous) -> None:
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        # Clear the bootstrapped admin so neil really is the last one, then
        # add a member - which must not count towards keeping a way in.
        assert admin.delete("/api/users/admin").status_code == 200
        anonymous.app.state.users.add("amit", ADMIN_PASSWORD, Role.MEMBER)
        assert admin.delete("/api/users/neil").status_code == 400

    def test_a_reset_password_works_immediately(self, anonymous) -> None:
        admin = self._sign_in(anonymous, "neil", Role.ADMIN)
        anonymous.app.state.users.add("amit", ADMIN_PASSWORD, Role.MEMBER)
        reset = admin.post(
            "/api/users/amit/password", json={"password": "a-fresh-password"}
        )
        assert reset.status_code == 200

        fresh = TestClient(anonymous.app)
        good = fresh.post(
            "/login", data={"username": "amit", "password": "a-fresh-password"}
        )
        assert good.status_code == 200
        stale = fresh.post(
            "/login", data={"username": "amit", "password": ADMIN_PASSWORD}
        )
        assert stale.status_code == 401


class TestSessionCookie:
    """The cookie itself, since it is the whole credential once issued."""

    @pytest.fixture
    def app_client(self, service_config, monkeypatch):
        _stub_runner(monkeypatch)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            yield client

    def test_is_httponly_and_samesite(self, app_client) -> None:
        """HttpOnly stops XSS lifting it; SameSite is the CSRF cover."""
        response = app_client.post(
            "/login",
            data={"username": "neil", "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        header = response.headers["set-cookie"].lower()
        assert "httponly" in header
        assert "samesite=lax" in header

    def test_logout_expires_the_cookie(self, app_client) -> None:
        app_client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
        response = app_client.post("/logout", follow_redirects=False)
        assert SESSION_COOKIE in response.headers["set-cookie"]
        assert app_client.get("/api/me").status_code == 401


class TestReadinessGuard:
    """The service must not send a bot into a meeting it cannot record.

    Every wasted call began with a bot that joined and produced nothing, or
    could not join at all, because something was already broken before anyone
    clicked send. Refusing costs a moment; finding out in the meeting costs
    the meeting.

    The session is re-checked when the bot is sent rather than read from the
    report the service cached at startup - see the class below for why.
    """

    @pytest.fixture
    def signed_in(self, service_config, monkeypatch):
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            yield client

    @staticmethod
    def _session(monkeypatch, *, ok: bool, hang: bool = False):
        """Replace the live session check. It launches a browser for real."""
        from meetbot.preflight import CheckResult
        from meetbot.service import app as app_module

        async def fake(_config):
            if hang:
                raise TimeoutError("took too long")
            if ok:
                return CheckResult(
                    "Google sign-in", ok=True, detail="signed in as a@b.com",
                    mode="bot",
                )
            return CheckResult(
                "Google sign-in",
                ok=False,
                detail="The profile is signed out",
                fix="python -m meetbot login",
                mode="bot",
            )

        monkeypatch.setattr(app_module, "check_google_session", fake)

    def test_refuses_when_the_bot_is_signed_out(self, signed_in, monkeypatch) -> None:
        self._session(monkeypatch, ok=False)
        response = signed_in.post("/api/meetings", json={"meet_url": MEET_URL})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "signed out" in detail
        assert "meetbot login" in detail, "the refusal must say what to do"

    def test_allows_a_meeting_when_the_session_is_live(
        self, signed_in, monkeypatch
    ) -> None:
        self._session(monkeypatch, ok=True)
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_a_stale_failure_does_not_block_a_session_that_now_works(
        self, signed_in, monkeypatch
    ) -> None:
        """The bug this guards against, exactly.

        The service checked once at startup, found the bot signed out, and
        cached it. Signing back in therefore appeared to do nothing - the
        start route was still quoting the report from before the fix, half an
        hour later. Readiness is now asked for, not remembered.
        """
        from meetbot.preflight import CheckResult, Preflight

        signed_in.app.state.health = Preflight([
            CheckResult(
                "Google sign-in", ok=False, detail="The profile is signed out",
                fix="python -m meetbot login", mode="bot",
            )
        ])
        signed_in.app.state.health_at = time.time()
        self._session(monkeypatch, ok=True)

        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_the_cached_report_is_corrected_by_the_live_check(
        self, signed_in, monkeypatch
    ) -> None:
        """So the health banner stops lying the moment the truth changes."""
        from meetbot.preflight import CheckResult, Preflight

        signed_in.app.state.health = Preflight([
            CheckResult(
                "Google sign-in", ok=False, detail="The profile is signed out",
                mode="bot",
            )
        ])
        signed_in.app.state.health_at = time.time()
        self._session(monkeypatch, ok=True)
        signed_in.post("/api/meetings", json={"meet_url": MEET_URL})

        health = signed_in.get("/api/health").json()
        session = next(c for c in health["checks"] if c["name"] == "Google sign-in")
        assert session["ok"] is True
        assert health["can_send_bot"] is True

    def test_a_check_that_cannot_run_does_not_refuse(
        self, signed_in, monkeypatch
    ) -> None:
        """Failing to check is not evidence of a problem.

        The run launches its own browser and will surface a dead session
        soon enough; refusing because the check timed out would block work
        over nothing.
        """
        self._session(monkeypatch, ok=True, hang=True)
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_allows_a_meeting_before_the_checks_have_finished(
        self, signed_in, monkeypatch
    ) -> None:
        """The checks start a browser, so they lag startup.

        Blocking until they finish would make the service refuse work for its
        first ten seconds, which is worse than the problem being guarded.
        """
        self._session(monkeypatch, ok=True)
        signed_in.app.state.health = None
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_a_warning_does_not_block(self, signed_in, monkeypatch) -> None:
        """A missing summary still leaves a usable transcript."""
        from meetbot.preflight import CheckResult, Preflight

        self._session(monkeypatch, ok=True)
        signed_in.app.state.health = Preflight([
            CheckResult("AI notes", ok=False, detail="no key", blocking=False),
        ])
        signed_in.app.state.health_at = time.time()
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_something_every_mode_needs_still_blocks(
        self, signed_in, monkeypatch
    ) -> None:
        """A bad Deepgram key is not a bot problem - it stops everything."""
        from meetbot.preflight import CheckResult, Preflight

        self._session(monkeypatch, ok=True)
        signed_in.app.state.health = Preflight([
            CheckResult("Deepgram", ok=False, detail="rejected the key"),
        ])
        signed_in.app.state.health_at = time.time()
        response = signed_in.post("/api/meetings", json={"meet_url": MEET_URL})
        assert response.status_code == 409
        assert "Deepgram" in response.json()["detail"]

    def test_recording_this_machine_never_waits_on_the_bot_session(
        self, signed_in, monkeypatch
    ) -> None:
        """Local capture has nothing to do with Google.

        It must not consult, wait for, or be refused by the bot's session -
        that was the whole point of not joining the meeting.
        """
        from meetbot.preflight import CheckResult, Preflight
        from meetbot.service import app as app_module

        called = []

        async def fake(_config):
            called.append(1)
            return CheckResult("Google sign-in", ok=False, detail="out", mode="bot")

        monkeypatch.setattr(app_module, "check_google_session", fake)
        signed_in.app.state.health = Preflight([
            CheckResult("Google sign-in", ok=False, detail="out", mode="bot")
        ])
        signed_in.app.state.health_at = time.time()

        # Fails only because this test box has no loopback device, never
        # because of Google - and the session check must not have been run.
        response = signed_in.post("/api/recordings", json={"title": "x"})
        assert response.status_code in (201, 409)
        if response.status_code == 409:
            assert "Google" not in response.json()["detail"]
        assert called == [], "local recording consulted the bot's session"


class TestHealthEndpoint:
    @pytest.fixture
    def signed_in(self, service_config, monkeypatch):
        _stub_runner(monkeypatch)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            yield client

    def test_reports_each_check_with_its_fix(self, signed_in) -> None:
        from meetbot.preflight import CheckResult, Preflight

        signed_in.app.state.health = Preflight([
            CheckResult("Google sign-in", ok=False, detail="signed out",
                        fix="python -m meetbot login"),
            CheckResult("Deepgram", ok=True, detail="fine"),
        ])
        payload = signed_in.get("/api/health").json()
        assert payload["can_record"] is False
        broken = [c for c in payload["checks"] if not c["ok"]]
        assert broken[0]["fix"] == "python -m meetbot login"

    def test_says_so_while_the_checks_are_still_running(self, signed_in) -> None:
        signed_in.app.state.health = None
        signed_in.app.state.health_checking = True
        payload = signed_in.get("/api/health").json()
        assert payload["checking"] is True
        assert payload["can_record"] is None

    def test_health_needs_an_account(self, service_config, monkeypatch) -> None:
        """It describes the deployment; strangers do not get to read it."""
        _stub_runner(monkeypatch)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            assert client.get("/api/health").status_code == 401
            assert client.post("/api/health/refresh").status_code == 401


class TestStartupDoesNotReachTheNetwork:
    """The suite must not depend on live credentials.

    The readiness checks open a real Deepgram stream and call the LLM. Running
    them from the service's startup meant the whole service suite did too - it
    passed on a laptop with working keys and failed on CI, where the guard
    then refused every meeting and the tests died on a missing 'id'. A test
    that needs the network fails for reasons that say nothing about the code.
    """

    def test_the_checks_are_on_by_default(self) -> None:
        """Off in tests, but an operator must still get them."""
        assert Config().preflight_on_start is True

    def test_disabling_them_leaves_no_health_report(self, service_config, monkeypatch):
        _stub_runner(monkeypatch)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            assert getattr(client.app.state, "health", None) is None

    def test_a_meeting_still_starts_with_no_report(self, service_config, monkeypatch):
        """No answer must not read as a failed answer."""
        _stub_runner(monkeypatch)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            created = client.post("/api/meetings", json={"meet_url": MEET_URL})
            assert created.status_code == 201
            assert "id" in created.json()


class TestNotepadApi:
    """The notepad: autosave, enhancement, and asking about a meeting.

    Every LLM call is faked - the suite must not need a key or a network.
    """

    @pytest.fixture
    def client(self, service_config, monkeypatch):
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as signed_out:
            signed_out.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            signed_out.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            yield signed_out

    @pytest.fixture
    def fake_llm(self, monkeypatch):
        """Replace the provider adapter the routes build per request."""
        from meetbot.service import app as app_module
        from tests.conftest import FakeLLMClient

        client = FakeLLMClient(text_responses=[])
        monkeypatch.setattr(app_module, "build_client", lambda *a, **k: client)
        return client

    @staticmethod
    def _meeting(client, *, transcript: bool = True) -> str:
        """A finished meeting, optionally with a transcript on disk."""
        from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

        job_id = client.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]
        for _ in range(80):
            status = client.get(f"/api/meetings/{job_id}").json()["status"]
            if status in ("finished", "failed"):
                break
        job = client.app.state.manager.get(job_id)
        assert job.output_dir is not None, "the run never reported its directory"
        if transcript:
            store = TranscriptStore(job.output_dir / "transcript.jsonl")
            store.write_meta(MeetingMeta(meet_url=MEET_URL, bot_name="Scribe"))
            store.append(Utterance("Speaker 0", "We are two weeks behind.", 0.0, 3.0))
            store.append(Utterance("Speaker 1", "I will take the script.", 4.0, 6.0))
            store.close()
        return job_id

    # -- templates ---------------------------------------------------------

    def test_templates_are_served_to_the_ui(self, client) -> None:
        payload = client.get("/api/templates").json()
        assert payload["default"]
        ids = [t["id"] for t in payload["templates"]]
        assert payload["default"] in ids
        assert "guidance" not in payload["templates"][0], "prompt internals leaked"

    def test_templates_need_a_session(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as anon:
            assert anon.get("/api/templates").status_code == 401

    # -- notes -------------------------------------------------------------

    def test_notes_round_trip(self, client) -> None:
        job_id = self._meeting(client)
        saved = client.put(
            f"/api/meetings/{job_id}/notes", json={"notes": "budget approved?"}
        )
        assert saved.status_code == 200
        assert saved.json()["saved"] is True
        assert client.get(f"/api/meetings/{job_id}/notepad").json()["notes"] == (
            "budget approved?"
        )

    def test_a_meeting_with_no_notes_reports_empty_rather_than_failing(
        self, client
    ) -> None:
        job_id = self._meeting(client)
        payload = client.get(f"/api/meetings/{job_id}/notepad").json()
        assert payload["notes"] == ""
        assert payload["enhanced"] == ""
        assert payload["chat"] == []
        assert payload["template"]

    def test_the_meetings_list_flags_notes_and_enhancement(self, client) -> None:
        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/notes", json={"notes": "x"})
        listed = client.get("/api/meetings").json()["meetings"]
        row = next(m for m in listed if m["id"] == job_id)
        assert row["has_notes"] is True
        assert row["has_enhanced"] is False

    def test_notes_need_a_session(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as anon:
            assert anon.put("/api/meetings/x/notes", json={"notes": "n"}).status_code == 401
            assert anon.get("/api/meetings/x/notepad").status_code == 401

    def test_a_member_cannot_read_another_persons_notes(self, client) -> None:
        """Transcripts are the sensitive part of this service; so are notes."""
        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/notes", json={"notes": "confidential"})
        client.app.state.users.add("priya", ADMIN_PASSWORD, Role.MEMBER)
        other = TestClient(client.app)
        other.post("/login", data={"username": "priya", "password": ADMIN_PASSWORD})
        response = other.get(f"/api/meetings/{job_id}/notepad")
        assert response.status_code == 404
        assert "confidential" not in response.text

    # -- enhancement -------------------------------------------------------

    def test_enhance_writes_notes_and_records_the_template(
        self, client, fake_llm
    ) -> None:
        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/notes", json={"notes": "api late"})
        fake_llm.text_responses = ["## Summary\n\nThe API work is two weeks late."]

        response = client.post(
            f"/api/meetings/{job_id}/enhance", json={"template": "standup"}
        )
        assert response.status_code == 200, response.text
        assert "two weeks late" in response.json()["enhanced"]
        assert response.json()["template"] == "standup"

        # Persisted, and visible to a later reader.
        pad = client.get(f"/api/meetings/{job_id}/notepad").json()
        assert "two weeks late" in pad["enhanced"]
        assert pad["template"] == "standup"
        assert pad["notes"] == "api late", "the original notes must survive"

    def test_enhance_sends_the_typed_notes_and_the_transcript(
        self, client, fake_llm
    ) -> None:
        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/notes", json={"notes": "priya owns it"})
        fake_llm.text_responses = ["notes"]
        client.post(f"/api/meetings/{job_id}/enhance", json={})
        prompt = fake_llm.text_calls[0]["user"]
        assert "priya owns it" in prompt
        assert "two weeks behind" in prompt.lower()

    def test_enhance_works_with_no_typed_notes(self, client, fake_llm) -> None:
        """The bot usually runs unattended, so this is the common case."""
        job_id = self._meeting(client)
        fake_llm.text_responses = ["## Summary\n\nSomething happened."]
        response = client.post(f"/api/meetings/{job_id}/enhance", json={})
        assert response.status_code == 200
        assert "Something happened" in response.json()["enhanced"]

    def test_enhance_refuses_when_there_is_nothing_to_work_from(
        self, client, fake_llm
    ) -> None:
        job_id = self._meeting(client, transcript=False)
        response = client.post(f"/api/meetings/{job_id}/enhance", json={})
        assert response.status_code == 409
        assert "nothing to work from" in response.json()["detail"].lower()
        assert not fake_llm.text_calls, "no quota should have been spent"

    def test_a_provider_failure_is_reported_as_a_bad_gateway(
        self, client, fake_llm
    ) -> None:
        from meetbot.analysis.llm import LLMError

        job_id = self._meeting(client)
        fake_llm.text_error = LLMError("rate limit hit")
        response = client.post(f"/api/meetings/{job_id}/enhance", json={})
        assert response.status_code == 502
        assert "rate limit" in response.json()["detail"]

    def test_enhance_is_refused_when_ai_is_switched_off(
        self, service_config, monkeypatch
    ) -> None:
        _stub_runner(monkeypatch, utterances=2)
        service_config = dataclasses.replace(service_config, analysis_enabled=False)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
            job_id = self._meeting(client)
            response = client.post(f"/api/meetings/{job_id}/enhance", json={})
            assert response.status_code == 409
            assert "switched off" in response.json()["detail"]
            # Notes are still saved - only the AI step is unavailable.
            assert client.put(
                f"/api/meetings/{job_id}/notes", json={"notes": "still works"}
            ).status_code == 200

    # -- ask ---------------------------------------------------------------

    def test_ask_answers_and_remembers_the_question(self, client, fake_llm) -> None:
        job_id = self._meeting(client)
        fake_llm.text_responses = ["They are two weeks behind."]
        response = client.post(
            f"/api/meetings/{job_id}/ask", json={"question": "How is the API work?"}
        )
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn["question"] == "How is the API work?"
        assert turn["answer"] == "They are two weeks behind."

        chat = client.get(f"/api/meetings/{job_id}/notepad").json()["chat"]
        assert [t["question"] for t in chat] == ["How is the API work?"]

    def test_ask_replays_earlier_turns_for_a_follow_up(self, client, fake_llm) -> None:
        job_id = self._meeting(client)
        fake_llm.text_responses = ["the API work", "Priya"]
        client.post(f"/api/meetings/{job_id}/ask", json={"question": "what slipped?"})
        client.post(f"/api/meetings/{job_id}/ask", json={"question": "who owns it?"})
        assert "what slipped?" in fake_llm.text_calls[-1]["user"]

    def test_an_empty_question_is_rejected_before_the_model(
        self, client, fake_llm
    ) -> None:
        job_id = self._meeting(client)
        response = client.post(f"/api/meetings/{job_id}/ask", json={"question": "  "})
        assert response.status_code == 400
        assert not fake_llm.text_calls

    def test_the_chat_can_be_cleared(self, client, fake_llm) -> None:
        job_id = self._meeting(client)
        fake_llm.text_responses = ["answer"]
        client.post(f"/api/meetings/{job_id}/ask", json={"question": "q"})
        assert client.delete(f"/api/meetings/{job_id}/chat").status_code == 200
        assert client.get(f"/api/meetings/{job_id}/notepad").json()["chat"] == []

    # -- artifacts ---------------------------------------------------------

    def test_notes_are_downloadable_as_artifacts(self, client, fake_llm) -> None:
        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/notes", json={"notes": "typed"})
        fake_llm.text_responses = ["## Enhanced"]
        client.post(f"/api/meetings/{job_id}/enhance", json={})
        assert client.get(
            f"/api/meetings/{job_id}/artifact/notes.md"
        ).text == "typed"
        assert "Enhanced" in client.get(
            f"/api/meetings/{job_id}/artifact/enhanced.md"
        ).text


def _finished_meeting(
    app,
    output_dir: Path,
    *,
    owner: str,
    name: str,
    lines: list[tuple[str, str]],
    title: str = "",
) -> str:
    """A finished meeting in its own directory, registered with the service.

    ``_stub_runner`` sends every job to the same directory, which is useless
    for proving that one account's question never reads another's meeting.
    """
    from meetbot.service.jobs import MeetingJob
    from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

    directory = output_dir / name
    store = TranscriptStore(directory / "transcript.jsonl")
    store.write_meta(MeetingMeta(meet_url=title or MEET_URL, bot_name="Scribe"))
    for i, (speaker, text) in enumerate(lines):
        store.append(Utterance(speaker, text, i * 20.0, i * 20.0 + 5.0))
    store.close()
    job = MeetingJob(
        id=f"job-{name}",
        meet_url=title or MEET_URL,
        kind="local" if title else "bot",
        title=title,
        owner=owner,
        status=JobStatus.FINISHED,
        created_at=time.time() - 600,
        finished_at=time.time() - 300,
        output_dir=directory,
    )
    app.state.manager._jobs[job.id] = job
    return job.id


def _signed_in_as(app, username: str, role: Role = Role.MEMBER):
    app.state.users.add(username, ADMIN_PASSWORD, role)
    other = TestClient(app)
    other.post("/login", data={"username": username, "password": ADMIN_PASSWORD})
    return other


class _CitingLLM:
    """Cites whichever prompt line contains ``phrase``, the way a model would."""

    model = "citing-model"

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase
        self.prompts: list[str] = []

    def complete_text(self, *, system: str, user: str, max_tokens: int = 0) -> str:
        import re

        self.prompts.append(user)
        found = re.search(r"\[(S\d+)\][^\n]*" + re.escape(self.phrase), user)
        return f"Here [{found.group(1)}]." if found else "I couldn't find that."

    def complete_json(self, **_kwargs) -> dict:
        return {}


class TestNotesWriteThemselves:
    """A meeting is written up as it ends, with nobody pressing anything.

    The stop button has been labelled "Stop & write notes" since it was added.
    These are the tests that make the label true - and the ones that keep the
    feature from writing up sixteen old recordings at every startup.
    """

    @pytest.fixture
    def auto_config(self, service_config) -> Config:
        """The suite runs with this off; these tests are what it is for."""
        return dataclasses.replace(service_config, auto_notes=True)

    @pytest.fixture
    def fake_llm(self, monkeypatch):
        from meetbot.service import app as app_module
        from tests.conftest import FakeLLMClient

        client = FakeLLMClient(text_responses=[])
        monkeypatch.setattr(app_module, "build_client", lambda *a, **k: client)
        return client

    @staticmethod
    def _sign_in(client) -> None:
        client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
        client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})

    @staticmethod
    def _transcript(directory: Path) -> None:
        from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

        store = TranscriptStore(directory / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url=MEET_URL, bot_name="Scribe"))
        store.append(Utterance("Speaker 0", "We are two weeks behind.", 0.0, 3.0))
        store.append(Utterance("Speaker 1", "I will take the script.", 4.0, 6.0))
        store.close()

    @staticmethod
    def _until(check, timeout: float = 15.0):
        """Whatever ``check`` first returns truthily, or ``None`` if it never does.

        The write-up runs as a task in the server's own loop, so the test has
        to wait for it rather than await it.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.05)
        return None

    def _recording(self, client) -> str:
        """A meeting that is running, with a transcript on disk."""
        job_id = client.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]
        directory = self._until(lambda: client.app.state.manager.get(job_id).output_dir)
        assert directory is not None, "the run never reported its directory"
        self._transcript(directory)
        return job_id

    def test_a_meeting_is_written_up_as_soon_as_it_ends(
        self, auto_config, monkeypatch, fake_llm
    ) -> None:
        _stub_runner(monkeypatch, utterances=2, block=True)
        auto_config.output_dir.mkdir(parents=True, exist_ok=True)
        fake_llm.text_responses = ["## Summary\n\nThe API work is two weeks behind."]
        with TestClient(create_app(auto_config)) as client:
            self._sign_in(client)
            job_id = self._recording(client)

            client.post(f"/api/meetings/{job_id}/stop")

            written = self._until(
                lambda: client.get(f"/api/meetings/{job_id}/notepad").json()["enhanced"]
            )
        assert written and "two weeks behind" in written

    def test_what_was_typed_during_the_call_is_the_spine_of_it(
        self, auto_config, monkeypatch, fake_llm
    ) -> None:
        """The automatic write-up is the same call the button makes."""
        _stub_runner(monkeypatch, utterances=2, block=True)
        auto_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(auto_config)) as client:
            self._sign_in(client)
            job_id = self._recording(client)
            client.put(
                f"/api/meetings/{job_id}/notes", json={"notes": "priya owns migration"}
            )

            client.post(f"/api/meetings/{job_id}/stop")

            assert self._until(lambda: fake_llm.text_calls) is not None
            prompt = fake_llm.text_calls[0]["user"]
        assert "priya owns migration" in prompt
        assert "two weeks behind" in prompt.lower()

    def test_a_meeting_that_captured_nothing_is_not_sent_to_the_model(
        self, auto_config, monkeypatch, fake_llm
    ) -> None:
        """Paying for a call to be told there was nothing is worse than silence."""
        _stub_runner(monkeypatch, utterances=2, block=True)
        auto_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(auto_config)) as client:
            self._sign_in(client)
            job_id = client.post(
                "/api/meetings", json={"meet_url": MEET_URL}
            ).json()["id"]
            assert self._until(
                lambda: client.app.state.manager.get(job_id).output_dir
            ) is not None

            client.post(f"/api/meetings/{job_id}/stop")

            assert self._until(
                lambda: client.get(f"/api/meetings/{job_id}").json()["status"]
                in ("finished", "failed")
            )
            time.sleep(0.4)
        assert not fake_llm.text_calls, "no quota should have been spent"

    def test_notes_written_during_the_call_are_not_replaced(
        self, auto_config, monkeypatch, fake_llm
    ) -> None:
        """Notes somebody may already have read are not rewritten behind them."""
        _stub_runner(monkeypatch, utterances=2, block=True)
        auto_config.output_dir.mkdir(parents=True, exist_ok=True)
        fake_llm.text_responses = ["## Summary\n\nWritten by hand mid-call."]
        with TestClient(create_app(auto_config)) as client:
            self._sign_in(client)
            job_id = self._recording(client)
            client.post(f"/api/meetings/{job_id}/enhance", json={})
            spent = len(fake_llm.text_calls)

            client.post(f"/api/meetings/{job_id}/stop")

            assert self._until(
                lambda: client.get(f"/api/meetings/{job_id}").json()["status"]
                == "finished"
            )
            time.sleep(0.4)
            pad = client.get(f"/api/meetings/{job_id}/notepad").json()
        assert len(fake_llm.text_calls) == spent, "it wrote them a second time"
        assert "by hand" in pad["enhanced"]

    def test_recordings_restored_from_disk_are_left_alone(
        self, auto_config, monkeypatch, fake_llm
    ) -> None:
        """Sixteen recordings on disk must not cost sixteen calls at startup."""
        _stub_runner(monkeypatch)
        past = auto_config.output_dir / "20260101-093000-local"
        past.mkdir(parents=True)
        self._transcript(past)
        with TestClient(create_app(auto_config)) as client:
            self._sign_in(client)
            listed = client.get("/api/meetings").json()["meetings"]
            assert any(m["id"].startswith("past-") for m in listed), "not restored"
            time.sleep(0.4)
        assert not fake_llm.text_calls

    def test_it_can_be_switched_off(
        self, service_config, monkeypatch, fake_llm
    ) -> None:
        """AUTO_NOTES=false, for somebody on a metered key."""
        assert service_config.auto_notes is False
        _stub_runner(monkeypatch, utterances=2, block=True)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            self._sign_in(client)
            job_id = self._recording(client)

            client.post(f"/api/meetings/{job_id}/stop")

            assert self._until(
                lambda: client.get(f"/api/meetings/{job_id}").json()["status"]
                == "finished"
            )
            time.sleep(0.4)
            pad = client.get(f"/api/meetings/{job_id}/notepad").json()
        assert not fake_llm.text_calls
        assert pad["enhanced"] == ""
        # Still enhanceable by hand - only the automatic part is off.
        assert service_config.analysis_enabled is True

    def test_the_page_is_told_while_the_notes_are_being_written(
        self, auto_config, monkeypatch
    ) -> None:
        """The page shows a thinking indicator for this, so it has to know."""
        writing = threading.Event()
        holding = threading.Event()

        class SlowLLM:
            model = "slow-model"

            def complete_text(self, **_kwargs) -> str:
                writing.set()
                holding.wait(20)
                return "## Summary\n\nWritten at last."

            def complete_json(self, **_kwargs) -> dict:
                return {}

        from meetbot.service import app as app_module

        monkeypatch.setattr(app_module, "build_client", lambda *a, **k: SlowLLM())
        _stub_runner(monkeypatch, utterances=2, block=True)
        auto_config.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with TestClient(create_app(auto_config)) as client:
                self._sign_in(client)
                job_id = self._recording(client)

                client.post(f"/api/meetings/{job_id}/stop")

                assert writing.wait(20), "the write-up never started"
                row = next(
                    m
                    for m in client.get("/api/meetings").json()["meetings"]
                    if m["id"] == job_id
                )
                assert row["enhancing"] is True
                assert client.get(f"/api/meetings/{job_id}").json()["enhancing"] is True

                holding.set()
                assert self._until(
                    lambda: client.get(f"/api/meetings/{job_id}/notepad")
                    .json()["enhanced"]
                )
                assert (
                    client.get(f"/api/meetings/{job_id}").json()["enhancing"] is False
                )
        finally:
            # A still-held LLM call would hang the loop's shutdown grace.
            holding.set()


class TestSpeakerNames:
    """Putting a real name to "Speaker 0", once, for everything downstream.

    Nothing links an audio track to a person, so diarization can only say that
    two different people spoke. Somebody who was there can say who they were in
    a couple of seconds - and "Speaker 1 will write the migration script" is a
    note nobody can act on.
    """

    @pytest.fixture
    def client(self, service_config, monkeypatch):
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as signed_out:
            signed_out.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            signed_out.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            yield signed_out

    @pytest.fixture
    def fake_llm(self, monkeypatch):
        from meetbot.service import app as app_module
        from tests.conftest import FakeLLMClient

        llm = FakeLLMClient(text_responses=[])
        monkeypatch.setattr(app_module, "build_client", lambda *a, **k: llm)
        return llm

    @staticmethod
    def _meeting(client, *, transcript: bool = True) -> str:
        """A finished meeting with two diarized voices on disk."""
        return TestNotepadApi._meeting(client, transcript=transcript)

    def _voices(self, client, job_id) -> list[dict]:
        return client.get(f"/api/meetings/{job_id}/transcript").json()["speakers"]

    def test_a_meeting_starts_with_the_labels_it_was_recorded_with(
        self, client
    ) -> None:
        job_id = self._meeting(client)
        assert self._voices(client, job_id) == [
            {"id": "Speaker 0", "name": "Speaker 0"},
            {"id": "Speaker 1", "name": "Speaker 1"},
        ]

    def test_a_voice_can_be_given_a_name(self, client) -> None:
        job_id = self._meeting(client)
        response = client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 1": "Priya"}}
        )
        assert response.status_code == 200, response.text
        assert {"id": "Speaker 1", "name": "Priya"} in response.json()["speakers"]

        transcript = client.get(f"/api/meetings/{job_id}/transcript").json()
        said = {s["speaker"]: s["text"] for s in transcript["segments"]}
        assert "Priya" in said
        assert "take the script" in said["Priya"]
        assert "Speaker 1" not in said, "the old label is still being shown"

    def test_the_transcript_keeps_what_was_actually_recorded(self, client) -> None:
        """Append-only is why a crash cannot lose a meeting; naming respects it."""
        job_id = self._meeting(client)
        client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 1": "Priya"}}
        )
        raw = (
            client.app.state.manager.get(job_id).output_dir / "transcript.jsonl"
        ).read_text(encoding="utf-8")
        assert '"Speaker 1"' in raw, "an utterance was rewritten"
        assert "speaker_names" in raw, "the mapping was not recorded as an event"

    def test_the_name_reaches_the_notes(self, client, fake_llm) -> None:
        """The whole point: the model stops writing about Speaker 1."""
        job_id = self._meeting(client)
        client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 1": "Priya"}}
        )
        fake_llm.text_responses = ["## Summary: Priya took the script."]
        assert client.post(f"/api/meetings/{job_id}/enhance", json={}).status_code == 200
        prompt = fake_llm.text_calls[0]["user"]
        assert "Priya" in prompt
        assert "Speaker 1" not in prompt

    def test_naming_again_wins(self, client) -> None:
        job_id = self._meeting(client)
        for name in ("Priya", "Priya Raghunathan"):
            client.put(
                f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 0": name}}
            )
        assert {"id": "Speaker 0", "name": "Priya Raghunathan"} in self._voices(
            client, job_id
        )

    def test_clearing_a_name_puts_the_label_back(self, client) -> None:
        """Somebody who named the wrong voice has to be able to undo it."""
        job_id = self._meeting(client)
        client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 0": "Priya"}}
        )
        client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 0": "   "}}
        )
        assert {"id": "Speaker 0", "name": "Speaker 0"} in self._voices(client, job_id)

    def test_a_name_is_tidied_and_capped(self, client) -> None:
        job_id = self._meeting(client)
        client.put(
            f"/api/meetings/{job_id}/speakers",
            json={"names": {"Speaker 0": "  Priya   R " + "a" * 200}},
        )
        named = next(
            v["name"] for v in self._voices(client, job_id) if v["id"] == "Speaker 0"
        )
        assert named.startswith("Priya R")
        assert len(named) == 60, "an unbounded name would reach every line"

    def test_a_voice_nobody_used_is_refused(self, client) -> None:
        """Accepting it silently would look like a save that did nothing."""
        job_id = self._meeting(client)
        response = client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 9": "Ghost"}}
        )
        assert response.status_code == 400
        assert "Speaker 9" in response.json()["detail"]

    def test_a_meeting_with_no_transcript_has_nobody_to_name(self, client) -> None:
        job_id = self._meeting(client, transcript=False)
        response = client.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 0": "Priya"}}
        )
        assert response.status_code == 409
        assert "nobody to name" in response.json()["detail"].lower()

    def test_naming_somebody_elses_meeting_is_a_404(self, client) -> None:
        job_id = self._meeting(client)
        client.app.state.users.add("priya", ADMIN_PASSWORD, Role.MEMBER)
        other = TestClient(client.app)
        other.post("/login", data={"username": "priya", "password": ADMIN_PASSWORD})
        assert other.put(
            f"/api/meetings/{job_id}/speakers", json={"names": {"Speaker 0": "Me"}}
        ).status_code == 404

    def test_naming_needs_a_session(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as anon:
            assert anon.put(
                "/api/meetings/x/speakers", json={"names": {"Speaker 0": "Me"}}
            ).status_code == 401


class TestVoiceCommands:
    """Saying it out loud, without touching the page.

    The recogniser itself is replaced: these tests are about what the service
    does when it is told something, not about whether Windows can hear. That
    is proved in test_voice.py, by having Windows speak to itself.
    """

    @pytest.fixture
    def rig(self, service_config, monkeypatch):
        import meetbot.capture.local as local_module
        from meetbot.service import app as app_module

        monkeypatch.setattr(local_module, "capture_available", lambda: (True, "ok"))
        monkeypatch.setattr(app_module, "voice_available", lambda: (True, "ready"))

        async def recording(config, *, on_output_dir=None, on_stop=None, **kwargs):
            directory = config.output_dir / "spoken"
            directory.mkdir(parents=True, exist_ok=True)
            if on_output_dir is not None:
                on_output_dir(directory)
            release = asyncio.Event()
            if on_stop is not None:
                on_stop(release.set)
            await release.wait()
            return MeetingRun(
                output_dir=directory,
                transcript_jsonl=directory / "transcript.jsonl",
                utterance_count=2,
            )

        monkeypatch.setattr(jobs_module, "run_local_meeting", recording)

        listeners: list = []

        class FakeListener:
            """Stands in for the microphone, and remembers being switched on."""

            def __init__(self, on_command, **kwargs) -> None:
                self.on_command = on_command
                self.started = False
                listeners.append(self)

            def start(self) -> bool:
                self.started = True
                return True

            def stop(self) -> None:
                self.started = False

            @property
            def listening(self) -> bool:
                return self.started

            @property
            def detail(self) -> str:
                return "Listening for \"Scribe, start recording\"."

        monkeypatch.setattr(app_module, "VoiceListener", FakeListener)
        config = dataclasses.replace(service_config, voice_commands=True)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            yield client, listeners[0]

    @staticmethod
    def _say(listener, kind: str, text: str, title: str = "") -> None:
        """Speak, the way the listener's own thread would."""
        from meetbot.capture.voice import VoiceCommand

        listener.on_command(VoiceCommand(kind, text, 0.95, title))

    @staticmethod
    def _until(check, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.05)
        return None

    def _recording(self, client):
        """The live local recording, once there is one."""
        return self._until(
            lambda: next(
                (
                    meeting
                    for meeting in client.get("/api/meetings").json()["meetings"]
                    if meeting["kind"] == "local"
                    and meeting["status"] not in ("finished", "failed")
                ),
                None,
            )
        )

    def test_the_microphone_is_opened_when_voice_commands_are_on(self, rig) -> None:
        _, listener = rig
        assert listener.started is True

    def test_saying_start_begins_a_recording(self, rig) -> None:
        client, listener = rig
        self._say(listener, "start", "scribe start recording")
        assert self._recording(client) is not None, "nothing started recording"

    def test_a_recording_started_by_voice_belongs_to_an_admin(self, rig) -> None:
        """Nobody is signed in when you speak to the room."""
        client, listener = rig
        self._say(listener, "start", "scribe start recording")
        meeting = self._recording(client)
        assert meeting is not None
        owner = client.app.state.users.get(meeting["owner"])
        assert owner is not None and owner.is_admin

    def test_it_does_not_start_a_second_recording_over_the_first(self, rig) -> None:
        client, listener = rig
        self._say(listener, "start", "scribe start recording")
        assert self._recording(client) is not None
        self._say(listener, "start", "scribe start recording")
        time.sleep(0.4)
        live = [
            meeting
            for meeting in client.get("/api/meetings").json()["meetings"]
            if meeting["status"] not in ("finished", "failed")
        ]
        assert len(live) == 1

    def test_saying_stop_ends_the_recording(self, rig) -> None:
        client, listener = rig
        self._say(listener, "start", "scribe start recording")
        meeting = self._recording(client)
        assert meeting is not None

        self._say(listener, "stop", "scribe stop recording")

        assert self._until(
            lambda: client.get(f"/api/meetings/{meeting['id']}").json()["status"]
            in ("finished", "failed")
        ), "it kept recording"

    def test_marking_a_moment_lands_in_the_transcript(self, rig) -> None:
        from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

        client, listener = rig
        self._say(listener, "start", "scribe start recording")
        meeting = self._recording(client)
        assert meeting is not None
        job = client.app.state.manager.get(meeting["id"])
        assert self._until(lambda: job.output_dir) is not None
        store = TranscriptStore(job.output_dir / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url="", bot_name="Scribe (local capture)"))
        store.append(Utterance("You", "This is the bit that matters.", 0.0, 4.0))
        store.close()

        self._say(listener, "mark", "scribe mark this")

        marks = self._until(
            lambda: client.get(f"/api/meetings/{meeting['id']}/transcript")
            .json()["marks"]
        )
        assert marks, "the moment was not recorded"

    def test_naming_the_meeting_renames_it(self, rig) -> None:
        client, listener = rig
        self._say(listener, "start", "scribe start recording")
        meeting = self._recording(client)
        assert meeting is not None

        self._say(listener, "name", "scribe call this the finance sync", "Finance sync")

        renamed = self._until(
            lambda: client.get(f"/api/meetings/{meeting['id']}").json()["title"]
            == "Finance sync"
        )
        assert renamed, "the recording kept its old name"
        # Written down too, so a restart does not forget it.
        job = client.app.state.manager.get(meeting["id"])
        raw = (job.output_dir / "transcript.jsonl").read_text(encoding="utf-8")
        assert "Finance sync" in raw

    def test_the_page_can_see_what_it_is_listening_for(self, rig) -> None:
        client, listener = rig
        payload = client.get("/api/voice").json()
        assert payload["enabled"] is True
        assert payload["listening"] is True
        assert "start recording" in payload["detail"]
        assert payload["phrases"]["start"] == "scribe start recording"

        self._say(listener, "mark", "scribe mark this")
        heard = self._until(lambda: client.get("/api/voice").json()["heard"])
        assert heard and heard["kind"] == "mark"

    def test_a_member_is_not_told_about_the_microphone(self, rig) -> None:
        """What is said in somebody's room is their business."""
        client, _ = rig
        client.app.state.users.add("priya", ADMIN_PASSWORD, Role.MEMBER)
        other = TestClient(client.app)
        other.post("/login", data={"username": "priya", "password": ADMIN_PASSWORD})
        payload = other.get("/api/voice").json()
        assert payload["enabled"] is False
        assert payload["listening"] is False
        assert payload["heard"] is None

    def test_scribe_listening_is_not_mistaken_for_a_call(
        self, service_config, monkeypatch
    ) -> None:
        """Listening holds the microphone, and Scribe watches for exactly that.

        Without telling the watcher which program the recogniser runs in,
        turning voice commands on makes Scribe offer to record the call it
        thinks somebody has just joined - itself.
        """
        from meetbot.capture import calls as calls_module
        from meetbot.capture.calls import MicSession
        from meetbot.capture.voice import recogniser_executable

        monkeypatch.setattr(calls_module, "GRACE_S", 0.0)
        listening = MicSession(recogniser_executable().replace("\\", "#"), 1, False)
        monkeypatch.setattr(calls_module, "read_mic_sessions", lambda: [listening])

        config = dataclasses.replace(
            service_config, voice_commands=True, call_alerts=True
        )
        app = create_app(config)
        assert app.state.calls.poll() == []
        assert app.state.calls.pending() == []

    def test_voice_is_off_unless_it_is_asked_for(
        self, service_config, monkeypatch
    ) -> None:
        """An always-open microphone is agreed to, not discovered."""
        assert service_config.voice_commands is False
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            payload = client.get("/api/voice").json()
        assert payload["enabled"] is False
        assert "VOICE_COMMANDS" in payload["detail"]


class TestRenamingAndDeleting:
    """Calling a meeting what it was, and getting rid of one for good."""

    @pytest.fixture
    def client(self, service_config, monkeypatch):
        _stub_runner(monkeypatch, utterances=2)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as signed_out:
            signed_out.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            signed_out.post(
                "/login", data={"username": "neil", "password": ADMIN_PASSWORD}
            )
            yield signed_out

    @staticmethod
    def _meeting(client) -> str:
        """A finished meeting with a transcript on disk."""
        from meetbot.transcript.store import MeetingMeta, TranscriptStore, Utterance

        job_id = client.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]
        for _ in range(80):
            if client.get(f"/api/meetings/{job_id}").json()["status"] in (
                "finished",
                "failed",
            ):
                break
        job = client.app.state.manager.get(job_id)
        assert job.output_dir is not None, "the run never reported its directory"
        store = TranscriptStore(job.output_dir / "transcript.jsonl")
        store.write_meta(MeetingMeta(meet_url=MEET_URL, bot_name="Scribe"))
        store.append(Utterance("Speaker 0", "We are two weeks behind.", 0.0, 3.0))
        store.close()
        return job_id

    # -- renaming ----------------------------------------------------------

    def test_a_meeting_can_be_called_what_it_actually_was(self, client) -> None:
        job_id = self._meeting(client)
        response = client.put(
            f"/api/meetings/{job_id}/title", json={"title": "Roadmap review"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "Roadmap review"
        listed = client.get("/api/meetings").json()["meetings"]
        assert next(m for m in listed if m["id"] == job_id)["title"] == "Roadmap review"

    def test_a_rename_outlives_the_process(self, client) -> None:
        """Kept in memory it would last until the next restart and no longer."""
        from meetbot.service.jobs import JobManager, load_past_runs
        from meetbot.transcript.store import read_title

        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/title", json={"title": "Roadmap review"})
        directory = client.app.state.manager.get(job_id).output_dir
        assert read_title(directory / "transcript.jsonl") == "Roadmap review"

        # What a restart does: read the same directories back off disk.
        restored = JobManager(client.app.state.manager._base)
        load_past_runs(restored, directory.parent)
        assert [j.title for j in restored.list()] == ["Roadmap review"]

    def test_renaming_a_bot_meeting_keeps_the_way_back_into_the_call(
        self, client
    ) -> None:
        """The name is not the link. Writing one over the other loses the call."""
        from meetbot.service.jobs import JobManager, load_past_runs

        job_id = self._meeting(client)
        client.put(f"/api/meetings/{job_id}/title", json={"title": "Roadmap review"})
        directory = client.app.state.manager.get(job_id).output_dir

        restored = JobManager(client.app.state.manager._base)
        load_past_runs(restored, directory.parent)
        job = restored.list()[0]
        assert job.title == "Roadmap review"
        assert job.meet_url == MEET_URL, "the Meet link was overwritten by the name"

    def test_a_name_that_is_only_spaces_is_refused(self, client) -> None:
        job_id = self._meeting(client)
        response = client.put(f"/api/meetings/{job_id}/title", json={"title": "   "})
        assert response.status_code == 400
        assert "name" in response.json()["detail"].lower()

    def test_an_absurd_name_is_cut_rather_than_refused(self, client) -> None:
        """Refusing a long name helps nobody; a novel per transcript does not."""
        from meetbot.service.app import MAX_TITLE_CHARS

        job_id = self._meeting(client)
        response = client.put(f"/api/meetings/{job_id}/title", json={"title": "x" * 500})
        assert response.status_code == 200
        assert len(response.json()["title"]) == MAX_TITLE_CHARS

    def test_renaming_needs_a_session(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as anon:
            assert (
                anon.put("/api/meetings/x/title", json={"title": "n"}).status_code == 401
            )

    def test_a_member_cannot_rename_someone_elses_meeting(self, client) -> None:
        job_id = self._meeting(client)
        client.app.state.users.add("priya", ADMIN_PASSWORD, Role.MEMBER)
        other = TestClient(client.app)
        other.post("/login", data={"username": "priya", "password": ADMIN_PASSWORD})
        assert (
            other.put(
                f"/api/meetings/{job_id}/title", json={"title": "mine now"}
            ).status_code
            == 404
        )

    # -- deleting ----------------------------------------------------------

    def test_deleting_takes_the_recording_with_it(self, client) -> None:
        job_id = self._meeting(client)
        directory = client.app.state.manager.get(job_id).output_dir
        assert (directory / "transcript.jsonl").exists()

        assert client.delete(f"/api/meetings/{job_id}").status_code == 200

        assert not directory.exists(), "the transcript is still on disk"
        assert client.get(f"/api/meetings/{job_id}").status_code == 404
        listed = client.get("/api/meetings").json()["meetings"]
        assert all(m["id"] != job_id for m in listed)

    def test_a_meeting_still_recording_is_not_deleted(
        self, service_config, monkeypatch
    ) -> None:
        """Deleting the directory from under the recorder helps nobody."""
        _stub_runner(monkeypatch, block=True)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
            job_id = client.post("/api/meetings", json={"meet_url": MEET_URL}).json()["id"]
            for _ in range(80):
                if client.get(f"/api/meetings/{job_id}").json()["status"] == "joining":
                    break
                time.sleep(0.02)

            response = client.delete(f"/api/meetings/{job_id}")

            assert response.status_code == 409
            assert "still recording" in response.json()["detail"].lower()
            assert client.get(f"/api/meetings/{job_id}").status_code == 200

    def test_a_delete_never_reaches_outside_the_recordings_directory(
        self, client, tmp_path
    ) -> None:
        """The path comes from the runner, not the request - but a delete earns
        the check anyway."""
        job_id = self._meeting(client)
        elsewhere = tmp_path / "not-a-recording"
        elsewhere.mkdir()
        (elsewhere / "precious.txt").write_text("keep me", encoding="utf-8")
        client.app.state.manager.get(job_id).output_dir = elsewhere

        assert client.delete(f"/api/meetings/{job_id}").status_code == 200

        assert (elsewhere / "precious.txt").exists(), "it deleted unrelated files"
        assert client.get(f"/api/meetings/{job_id}").status_code == 404

    def test_deleting_needs_a_session(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as anon:
            assert anon.delete("/api/meetings/x").status_code == 401

    def test_a_member_cannot_delete_someone_elses_meeting(self, client) -> None:
        job_id = self._meeting(client)
        client.app.state.users.add("priya", ADMIN_PASSWORD, Role.MEMBER)
        other = TestClient(client.app)
        other.post("/login", data={"username": "priya", "password": ADMIN_PASSWORD})
        assert other.delete(f"/api/meetings/{job_id}").status_code == 404
        assert client.get(f"/api/meetings/{job_id}").status_code == 200


class TestRecallApi:
    """Questions across every meeting, and the citations behind every answer."""

    @pytest.fixture
    def client(self, service_config, monkeypatch):
        _stub_runner(monkeypatch)
        service_config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
            yield client

    @staticmethod
    def _llm(monkeypatch, llm):
        from meetbot.service import app as app_module

        monkeypatch.setattr(app_module, "build_client", lambda *a, **k: llm)
        return llm

    # -- every meeting -----------------------------------------------------

    def test_an_answer_cites_the_meeting_it_came_from(
        self, client, service_config, monkeypatch
    ) -> None:
        llm = self._llm(monkeypatch, _CitingLLM("capped at forty thousand"))
        _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="standup",
            title="Standup", lines=[("Speaker 0", "The API work is two weeks behind.")],
        )
        finance = _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="finance",
            title="Finance sync",
            lines=[("Speaker 1", "The marketing budget is capped at forty thousand.")],
        )

        response = client.post("/api/chat", json={"question": "What is the budget?"})
        assert response.status_code == 200, response.text
        turn = response.json()
        [citation] = turn["citations"]
        assert citation["meeting_id"] == finance
        assert citation["meeting_title"] == "Finance sync"
        assert citation["text"] == "The marketing budget is capped at forty thousand."
        assert "[^1]" in turn["answer"]
        assert turn["searched"] == 2
        assert "two weeks behind" in llm.prompts[0], "every meeting should be read"

        chat = client.get("/api/chat").json()
        assert [t["question"] for t in chat["chat"]] == ["What is the budget?"]
        assert chat["chat"][0]["citations"] == turn["citations"]

    def test_a_member_is_only_searched_against_their_own_meetings(
        self, client, service_config, monkeypatch
    ) -> None:
        """The chat must not become a way round per-meeting visibility."""
        llm = self._llm(monkeypatch, _CitingLLM("anything"))
        _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="board",
            title="Board", lines=[("Speaker 0", "Confidential: we are acquiring Contoso.")],
        )
        _finished_meeting(
            client.app, service_config.output_dir, owner="priya", name="mine",
            title="Priya's standup", lines=[("Speaker 0", "My tickets are done.")],
        )
        priya = _signed_in_as(client.app, "priya")
        response = priya.post("/api/chat", json={"question": "Are we acquiring anyone?"})
        assert response.status_code == 200, response.text
        assert "Contoso" not in llm.prompts[-1]
        assert "My tickets are done." in llm.prompts[-1]
        assert response.json()["searched"] == 1

    def test_each_account_keeps_its_own_conversation(
        self, client, service_config, monkeypatch
    ) -> None:
        self._llm(monkeypatch, _CitingLLM("x"))
        _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="m",
            lines=[("Speaker 0", "Hello.")],
        )
        client.post("/api/chat", json={"question": "neil's private question"})
        priya = _signed_in_as(client.app, "priya")
        assert priya.get("/api/chat").json()["chat"] == []

    def test_the_conversation_can_be_cleared(
        self, client, service_config, monkeypatch
    ) -> None:
        self._llm(monkeypatch, _CitingLLM("x"))
        _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="m",
            lines=[("Speaker 0", "Hello.")],
        )
        client.post("/api/chat", json={"question": "q"})
        assert client.delete("/api/chat").status_code == 200
        assert client.get("/api/chat").json()["chat"] == []

    def test_nothing_to_search_is_refused_before_the_model(
        self, client, monkeypatch
    ) -> None:
        llm = self._llm(monkeypatch, _CitingLLM("x"))
        response = client.post("/api/chat", json={"question": "anything?"})
        assert response.status_code == 400
        assert "nothing to search" in response.json()["detail"]
        assert llm.prompts == []

    def test_a_provider_failure_is_a_bad_gateway(
        self, client, service_config, monkeypatch
    ) -> None:
        from meetbot.analysis.llm import LLMError
        from tests.conftest import FakeLLMClient

        failing = FakeLLMClient()
        failing.text_error = LLMError("rate limit hit")
        self._llm(monkeypatch, failing)
        _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="m",
            lines=[("Speaker 0", "Hello.")],
        )
        response = client.post("/api/chat", json={"question": "q"})
        assert response.status_code == 502
        assert "rate limit" in response.json()["detail"]

    def test_the_chat_needs_a_session(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as anon:
            assert anon.get("/api/chat").status_code == 401
            assert anon.post("/api/chat", json={"question": "q"}).status_code == 401

    # -- one meeting -------------------------------------------------------

    def test_asking_one_meeting_returns_citations_and_keeps_them(
        self, client, service_config, monkeypatch
    ) -> None:
        self._llm(monkeypatch, _CitingLLM("two weeks behind"))
        job_id = _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="standup",
            lines=[
                ("Speaker 0", "Where are we on the API?"),
                ("Speaker 1", "The API work is two weeks behind."),
            ],
        )
        response = client.post(
            f"/api/meetings/{job_id}/ask", json={"question": "How late is it?"}
        )
        assert response.status_code == 200, response.text
        turn = response.json()
        [citation] = turn["citations"]
        assert citation["speaker"] == "Speaker 1"
        assert citation["start"] == 20.0
        assert citation["before"][0]["text"] == "Where are we on the API?"
        stored = client.get(f"/api/meetings/{job_id}/notepad").json()["chat"][0]
        assert stored["citations"] == turn["citations"]

    def test_the_whole_transcript_is_served_in_citable_lines(
        self, client, service_config
    ) -> None:
        job_id = _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="m",
            lines=[("Speaker 0", "First."), ("Speaker 1", "Second.")],
        )
        payload = client.get(f"/api/meetings/{job_id}/transcript").json()
        assert [s["text"] for s in payload["segments"]] == ["First.", "Second."]
        assert payload["segments"][1]["start"] == 20.0
        assert payload["utterance_count"] == 2

    def test_another_members_transcript_stays_hidden(
        self, client, service_config
    ) -> None:
        job_id = _finished_meeting(
            client.app, service_config.output_dir, owner="neil", name="m",
            lines=[("Speaker 0", "Confidential.")],
        )
        priya = _signed_in_as(client.app, "priya")
        response = priya.get(f"/api/meetings/{job_id}/transcript")
        assert response.status_code == 404
        assert "Confidential" not in response.text


class TestCallAlertsApi:
    """The offer to record a call, as the page sees it."""

    WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"

    @pytest.fixture
    def rig(self, service_config, monkeypatch):
        from meetbot.capture import calls as calls_module
        from meetbot.service import app as app_module

        sessions: list = []
        toasts: list = []
        monkeypatch.setattr(calls_module, "read_mic_sessions", lambda: list(sessions))
        monkeypatch.setattr(calls_module, "visible_window_titles", lambda: [])
        monkeypatch.setattr(calls_module, "GRACE_S", 0.0)
        monkeypatch.setattr(app_module, "POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(app_module, "detection_available", lambda: (True, "watching"))
        monkeypatch.setattr(
            app_module, "show_toast", lambda prompt, base: toasts.append(prompt) or True
        )
        _stub_runner(monkeypatch)
        config = dataclasses.replace(service_config, call_alerts=True)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
            yield client, sessions, toasts

    def _start_call(self, sessions: list) -> None:
        from meetbot.capture.calls import MicSession

        filetime = int(time.time() * 10_000_000) + 116_444_736_000_000_000
        sessions.append(MicSession(self.WHATSAPP, filetime, True))

    @staticmethod
    def _offered(client, count: int = 1) -> list[dict]:
        for _ in range(250):
            prompts = client.get("/api/calls").json()["prompts"]
            if len(prompts) >= count:
                return prompts
            time.sleep(0.02)
        raise AssertionError("the call was never offered")

    def test_a_call_is_offered_and_announced_once(self, rig) -> None:
        client, sessions, toasts = rig
        self._start_call(sessions)
        [prompt] = self._offered(client)
        assert prompt["app"] == "WhatsApp"
        assert prompt["title"] == "WhatsApp call"
        time.sleep(0.3)  # many more polls
        assert [t.id for t in toasts] == [prompt["id"]], "one notification per call"

    def test_a_member_learns_nothing_about_the_hosts_calls(self, rig) -> None:
        client, sessions, _ = rig
        self._start_call(sessions)
        [prompt] = self._offered(client)
        priya = _signed_in_as(client.app, "priya")
        payload = priya.get("/api/calls").json()
        assert payload["prompts"] == []
        assert payload["enabled"] is False
        assert priya.post(f"/api/calls/{prompt['id']}/dismiss").status_code == 403
        assert priya.post("/api/calls/mute", json={"app_id": self.WHATSAPP}).status_code == 403

    def test_dismissing_a_call_hides_it(self, rig) -> None:
        client, sessions, _ = rig
        self._start_call(sessions)
        [prompt] = self._offered(client)
        assert client.post(f"/api/calls/{prompt['id']}/dismiss").status_code == 200
        assert client.get("/api/calls").json()["prompts"] == []
        assert client.post("/api/calls/no-such-call/dismiss").status_code == 404

    def test_muting_an_app_is_remembered_and_reversible(self, rig) -> None:
        client, sessions, _ = rig
        self._start_call(sessions)
        [prompt] = self._offered(client)
        muted = client.post("/api/calls/mute", json={"app_id": prompt["app_id"]}).json()
        assert muted["muted"] == [{"app_id": self.WHATSAPP, "app": "WhatsApp"}]
        assert client.get("/api/calls").json()["prompts"] == []
        client.post("/api/calls/unmute", json={"app_id": self.WHATSAPP})
        assert client.get("/api/calls").json()["muted"] == []

    def test_recording_the_call_ends_the_offer(self, rig, monkeypatch) -> None:
        import meetbot.capture.local as local_module

        client, sessions, _ = rig
        monkeypatch.setattr(local_module, "capture_available", lambda: (True, "ok"))

        async def recording(config, **kwargs):
            await asyncio.sleep(3600)

        monkeypatch.setattr(jobs_module, "run_local_meeting", recording)
        self._start_call(sessions)
        [prompt] = self._offered(client)
        response = client.post(
            "/api/recordings", json={"title": prompt["title"], "call_id": prompt["id"]}
        )
        assert response.status_code == 201, response.text
        assert response.json()["title"] == "WhatsApp call"
        assert client.get("/api/calls").json()["prompts"] == []

    def test_alerts_can_be_switched_off(self, service_config, monkeypatch) -> None:
        _stub_runner(monkeypatch)
        with TestClient(create_app(service_config)) as client:
            client.app.state.users.add("neil", ADMIN_PASSWORD, Role.ADMIN)
            client.post("/login", data={"username": "neil", "password": ADMIN_PASSWORD})
            payload = client.get("/api/calls").json()
            assert payload["enabled"] is False
            assert "CALL_ALERTS" in payload["detail"]
