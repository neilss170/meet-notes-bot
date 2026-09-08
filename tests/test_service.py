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
from meetbot.service.auth import SESSION_COOKIE, Role, issue_session
from meetbot.service.jobs import JobManager, JobStatus, free_port, load_past_runs

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

MEET_URL = "https://meet.google.com/abc-defg-hij"
ADMIN_PASSWORD = "a-long-enough-password"


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
        assert "Send a note-taking bot" in response.text

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
        assert "lastSignature" in ui
        assert "signature === lastSignature" in ui

    def test_scroll_position_is_restored_across_a_rebuild(self) -> None:
        ui = self._ui()
        assert "window.scrollY" in ui
        assert "window.scrollTo" in ui
        assert "scrollTop" in ui

    def test_the_open_artifact_is_held_as_state_not_refetched(self) -> None:
        """Re-fetching on each render raced the poll loop.

        A slower earlier fetch could land after a newer one and paint the
        previous document, which is why Transcript and AI notes appeared to
        show the same content.
        """
        ui = self._ui()
        assert "let open = null" in ui
        assert "open.jobId === jobId && open.name === name" in ui

    def test_artifacts_render_as_structure_rather_than_preformatted_text(
        self,
    ) -> None:
        ui = self._ui()
        assert "function renderMarkdown" in ui
        for token in ("md-h", "md-list", "md-task", "md-turn"):
            assert token in ui, token


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
    def _report(*, can_record: bool):
        from meetbot.preflight import CheckResult, Preflight

        if can_record:
            return Preflight([CheckResult("Google sign-in", ok=True, detail="fine")])
        return Preflight([
            CheckResult(
                "Google sign-in",
                ok=False,
                detail="The profile is signed out",
                fix="python -m meetbot login",
            )
        ])

    def test_refuses_to_start_a_meeting_that_cannot_be_recorded(
        self, signed_in
    ) -> None:
        signed_in.app.state.health = self._report(can_record=False)
        response = signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "signed out" in detail
        assert "meetbot login" in detail, "the refusal must say what to do"

    def test_allows_a_meeting_when_everything_passes(self, signed_in) -> None:
        signed_in.app.state.health = self._report(can_record=True)
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_allows_a_meeting_before_the_checks_have_finished(
        self, signed_in
    ) -> None:
        """The checks start a browser, so they lag startup.

        Blocking until they finish would make the service refuse work for its
        first ten seconds, which is worse than the problem being guarded.
        """
        signed_in.app.state.health = None
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201

    def test_a_warning_does_not_block(self, signed_in) -> None:
        """A missing summary still leaves a usable transcript."""
        from meetbot.preflight import CheckResult, Preflight

        signed_in.app.state.health = Preflight([
            CheckResult("AI notes", ok=False, detail="no key", blocking=False),
        ])
        assert signed_in.post(
            "/api/meetings", json={"meet_url": MEET_URL}
        ).status_code == 201


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
