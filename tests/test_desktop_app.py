"""The server as the desktop app runs it: data directory, shell control, shutdown.

The Electron shell itself is checked by `npm run smoke`, against the real
build. These cover the Python side of the contract it relies on: where data
lives and how an earlier install is moved in, the unauthenticated health
check, the control token, and the graceful shutdown route.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from meetbot.appdata import DataDir, migrate
from meetbot.config import Config
from meetbot.server_main import _alive, watch_parent
from meetbot.service.app import CONTROL_HEADER, create_app
from meetbot.service.auth import SESSION_COOKIE

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

TOKEN = "t" * 64


# -- the data directory ---------------------------------------------------------


@pytest.fixture
def legacy(tmp_path: Path) -> dict[str, Path]:
    """An earlier from-source Scribe: a checkout, and its ~/.meetbot."""
    project = tmp_path / "checkout"
    (project / "recordings" / "20261001-090000-standup").mkdir(parents=True)
    (project / "recordings" / "20261001-090000-standup" / "transcript.jsonl").write_text(
        '{"type": "meta"}\n', encoding="utf-8"
    )
    (project / ".env").write_text("DEEPGRAM_API_KEY=dg-secret\n", encoding="utf-8")
    state = tmp_path / "home" / ".meetbot"
    (state / "chats").mkdir(parents=True)
    (state / "users.json").write_text("{}", encoding="utf-8")
    (state / "session.key").write_text("key", encoding="utf-8")
    (state / "chats" / "neil.jsonl").write_text("", encoding="utf-8")
    (state / "chrome-profile" / "Default").mkdir(parents=True)
    (state / "chrome-profile" / "Default" / "Cookies").write_text("x", encoding="utf-8")
    return {"project": project, "state": state}


class TestMovingIn:
    def test_everything_is_copied_and_nothing_is_moved(self, tmp_path, legacy) -> None:
        data = DataDir(tmp_path / "AppData" / "Scribe").create()
        record = migrate(data, project_dirs=[legacy["project"]], legacy_state=legacy["state"])

        assert data.env_file.read_text(encoding="utf-8") == "DEEPGRAM_API_KEY=dg-secret\n"
        assert (data.state / "users.json").exists()
        assert (data.state / "chats" / "neil.jsonl").exists()
        assert (data.recordings / "20261001-090000-standup" / "transcript.jsonl").exists()
        # The originals are exactly where they were.
        assert (legacy["project"] / ".env").exists()
        assert (legacy["state"] / "users.json").exists()
        assert (legacy["project"] / "recordings" / "20261001-090000-standup").exists()
        assert record["copied"]["recordings"]["files"] == 1

    def test_a_copied_meeting_keeps_its_date(self, tmp_path, legacy) -> None:
        """Past meetings are dated by their folder's time; a copy must not reset it."""
        meeting = legacy["project"] / "recordings" / "20261001-090000-standup"
        old = time.time() - 14 * 86400
        os.utime(meeting, (old, old))
        data = DataDir(tmp_path / "Scribe").create()
        migrate(data, project_dirs=[legacy["project"]], legacy_state=legacy["state"])
        copied = data.recordings / "20261001-090000-standup"
        assert abs(copied.stat().st_mtime - old) < 2

    def test_the_bots_browser_profile_stays_where_it_is(self, tmp_path, legacy) -> None:
        data = DataDir(tmp_path / "Scribe").create()
        migrate(data, project_dirs=[legacy["project"]], legacy_state=legacy["state"])
        assert not (data.state / "chrome-profile").exists()

    def test_it_happens_once(self, tmp_path, legacy) -> None:
        data = DataDir(tmp_path / "Scribe").create()
        assert migrate(data, project_dirs=[legacy["project"]], legacy_state=legacy["state"])
        (data.recordings / "20261001-090000-standup").rename(data.recordings / "renamed")
        assert migrate(data, project_dirs=[legacy["project"]], legacy_state=legacy["state"]) is None
        assert not (data.recordings / "20261001-090000-standup").exists()
        assert json.loads(data.marker.read_text(encoding="utf-8"))["project"]

    def test_settings_already_there_are_not_replaced(self, tmp_path, legacy) -> None:
        data = DataDir(tmp_path / "Scribe").create()
        data.env_file.write_text("DEEPGRAM_API_KEY=typed-here\n", encoding="utf-8")
        migrate(data, project_dirs=[legacy["project"]], legacy_state=legacy["state"])
        assert "typed-here" in data.env_file.read_text(encoding="utf-8")

    def test_a_fresh_machine_gets_a_settings_file_to_fill_in(self, tmp_path) -> None:
        template = tmp_path / ".env.example"
        template.write_text("DEEPGRAM_API_KEY=\n", encoding="utf-8")
        data = DataDir(tmp_path / "Scribe").create()
        record = migrate(data, project_dirs=[], legacy_state=tmp_path / "nothing")
        assert record["copied"] == {}
        assert data.seed_env(template) is True
        assert data.env_file.read_text(encoding="utf-8") == "DEEPGRAM_API_KEY=\n"
        assert data.seed_env(template) is False, "an existing .env was overwritten"


# -- the shell's routes ---------------------------------------------------------


def _config(tmp_path: Path) -> Config:
    return Config(
        meet_url="",
        output_dir=tmp_path / "recordings",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        service_state_dir=tmp_path / "state",
        preflight_on_start=False,
        call_alerts=False,
        auto_notes=False,
    )


@pytest.fixture
def shell(tmp_path):
    exits: list[bool] = []
    app = create_app(_config(tmp_path), control_token=TOKEN, shell_notifications=True)
    with TestClient(app) as client:
        app.state.request_exit = lambda: exits.append(True)
        yield client, exits


class TestTheShellsRoutes:
    def test_health_needs_no_account(self, shell) -> None:
        client, _ = shell
        body = client.get("/health").json()
        assert body["ok"] is True and body["app"] == "scribe"

    @pytest.mark.parametrize("path", ["/api/control/session", "/api/control/shutdown"])
    def test_control_routes_do_not_exist_without_the_token(self, shell, path) -> None:
        client, exits = shell
        assert client.post(path).status_code == 404
        assert client.post(path, headers={CONTROL_HEADER: "wrong"}).status_code == 404
        assert exits == []

    def test_the_token_acts_as_the_owner_on_ordinary_routes(self, shell) -> None:
        client, _ = shell
        assert client.get("/api/meetings").status_code == 401
        mine = client.get("/api/me", headers={CONTROL_HEADER: TOKEN})
        assert mine.status_code == 200 and mine.json()["role"] == "admin"

    def test_the_window_is_signed_in_as_the_owner(self, shell) -> None:
        client, _ = shell
        grant = client.post("/api/control/session", headers={CONTROL_HEADER: TOKEN}).json()
        assert grant["cookie"] == SESSION_COOKIE
        client.cookies.set(SESSION_COOKIE, grant["value"])
        assert client.get("/api/me").json()["username"] == grant["username"]

    def test_shutdown_is_asked_for_not_forced(self, shell) -> None:
        client, exits = shell
        response = client.post("/api/control/shutdown", headers={CONTROL_HEADER: TOKEN})
        assert response.json() == {"stopping": True, "recording": False}
        assert exits == [True]

    def test_without_a_shell_no_header_is_trusted(self, tmp_path) -> None:
        app = create_app(_config(tmp_path))
        with TestClient(app) as client:
            assert client.get("/api/me", headers={CONTROL_HEADER: ""}).status_code == 401
            assert client.post(
                "/api/control/shutdown", headers={CONTROL_HEADER: ""}
            ).status_code == 404

    def test_the_page_is_told_the_shortcut_the_shell_registered(self, shell) -> None:
        from meetbot.service.jobs import JobStatus, MeetingJob

        client, _ = shell
        client.app.state.mic_shortcut = "Ctrl+Alt+M"
        manager = client.app.state.manager
        manager._jobs["j1"] = MeetingJob(
            id="j1", meet_url="Standup", kind="local", status=JobStatus.IN_CALL
        )
        meetings = client.get("/api/meetings", headers={CONTROL_HEADER: TOKEN}).json()
        assert meetings["meetings"][0]["mic_shortcut"] == "Ctrl+Alt+M"


# -- not outliving the shell ----------------------------------------------------


class TestTheParent:
    def test_this_process_is_alive(self) -> None:
        assert _alive(os.getpid()) is True

    def test_an_exited_process_is_not(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        assert _alive(child.pid) is False

    def test_the_server_is_told_when_the_parent_goes(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
        gone = threading.Event()
        watch_parent(child.pid, gone.set, poll_s=0.1)
        assert gone.wait(10), "the server was not told its parent had gone"


class TestMarkingAMoment:
    """The bubble's "Mark this moment", through the ordinary route."""

    def test_a_live_meeting_can_be_marked(self, shell, tmp_path) -> None:
        from meetbot.service.jobs import JobStatus, MeetingJob
        from meetbot.transcript.store import read_marks

        client, _ = shell
        directory = tmp_path / "recordings" / "live"
        directory.mkdir(parents=True)
        manager = client.app.state.manager
        manager._jobs["j1"] = MeetingJob(
            id="j1", meet_url="Standup", kind="local",
            status=JobStatus.IN_CALL, output_dir=directory,
        )
        response = client.post("/api/meetings/j1/mark", headers={CONTROL_HEADER: TOKEN})
        assert response.status_code == 200
        assert read_marks(directory / "transcript.jsonl") == [0.0]

    def test_a_finished_meeting_cannot(self, shell, tmp_path) -> None:
        from meetbot.service.jobs import JobStatus, MeetingJob

        client, _ = shell
        client.app.state.manager._jobs["j2"] = MeetingJob(
            id="j2", meet_url="Old", kind="local",
            status=JobStatus.FINISHED, output_dir=tmp_path,
        )
        response = client.post("/api/meetings/j2/mark", headers={CONTROL_HEADER: TOKEN})
        assert response.status_code == 409
