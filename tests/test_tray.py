"""The notification-area icon: what it says, and what its menu does.

Nothing here shows an icon. The parts worth testing are the ones that decide
what the menu says and which of them fire - a real icon would need a desktop
session, and the message loop would never return to the test. The one place
pystray itself is exercised is the translation into its menu objects, because
that layer has already been wrong once: pystray inspects the argument count of
a callback and rejects the obvious lambda.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meetbot import tray
from meetbot.preflight import CheckResult, Preflight


def job(
    job_id: str = "j1",
    *,
    title: str = "Standup",
    terminal: bool = False,
    elapsed: float = 247.0,
    kind: str = "local",
) -> SimpleNamespace:
    """A stand-in for :class:`~meetbot.service.jobs.MeetingJob`."""
    return SimpleNamespace(
        id=job_id,
        title=title,
        meet_url=title,
        kind=kind,
        status=SimpleNamespace(is_terminal=terminal),
        elapsed_s=elapsed,
    )


class FakeManager:
    """Records what the menu asked it to do."""

    def __init__(self, jobs: list[SimpleNamespace] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []

    def list(self) -> list[SimpleNamespace]:
        return list(self.jobs)

    def start_local(self, **kwargs: Any) -> SimpleNamespace:
        self.started.append(kwargs)
        started = job("new", title=kwargs.get("title", ""))
        self.jobs.insert(0, started)
        return started

    async def stop(self, job_id: str) -> bool:
        self.stopped.append(job_id)
        return any(j.id == job_id for j in self.jobs)


class FakeUsers:
    def __init__(self, *names: tuple[str, bool]) -> None:
        self._users = [
            SimpleNamespace(username=name, is_admin=is_admin) for name, is_admin in names
        ]

    def list(self) -> list[SimpleNamespace]:
        return list(self._users)


def fake_app(
    manager: FakeManager | None = None,
    *,
    health: Preflight | None = None,
    in_flight: set[str] | None = None,
    users: FakeUsers | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SimpleNamespace:
    state = SimpleNamespace(
        manager=manager if manager is not None else FakeManager(),
        users=users or FakeUsers(("neil", True), ("sam", False)),
        in_flight=in_flight or set(),
        loop=loop,
    )
    if health is not None:
        state.health = health
    return SimpleNamespace(state=state)


def blocked(name: str = "Deepgram", mode: str = "both") -> Preflight:
    return Preflight(
        results=[
            CheckResult(name, False, "not configured", blocking=True, mode=mode),
            CheckResult("Local capture", True, "fine", blocking=True, mode="local"),
        ]
    )


@pytest.fixture
def server_loop():
    """A running event loop on another thread, as the real service has."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


class TestTheClock:
    def test_a_fresh_recording_reads_as_zero(self) -> None:
        assert tray.clock(0) == "0:00"

    def test_minutes_and_seconds(self) -> None:
        assert tray.clock(247) == "4:07"

    def test_an_hour_gains_a_field(self) -> None:
        """A standup that ran to 64 minutes must not read as 4:07."""
        assert tray.clock(3847) == "1:04:07"

    def test_nonsense_does_not_raise(self) -> None:
        assert tray.clock(-5) == "0:00"


class TestWhatTheIconSays:
    def test_nothing_running_says_so(self) -> None:
        status = tray.read_status(fake_app())
        assert status.label == "Not recording"
        assert status.recording is False
        assert status.live_id == ""

    def test_a_live_meeting_is_named(self) -> None:
        status = tray.read_status(fake_app(FakeManager([job("abc", title="Standup")])))
        assert status.label == "Recording · Standup"
        assert status.recording is True
        assert status.live_id == "abc"
        assert status.elapsed_s == pytest.approx(247.0)

    def test_the_clock_is_in_the_tooltip_not_the_menu(self) -> None:
        """The menu is only rebuilt when its text changes, so a clock in it
        would mean handing Windows a new menu every two seconds."""
        status = tray.read_status(fake_app(FakeManager([job()])))
        assert "4:07" not in status.label
        assert tray.tooltip(status) == "Scribe — Recording · Standup · 4:07"

    def test_a_write_up_in_flight_is_not_silence(self) -> None:
        """Twenty seconds of "Not recording" after a call ends reads as lost
        work; it is the summary being written."""
        manager = FakeManager([job("abc", title="Standup", terminal=True)])
        status = tray.read_status(fake_app(manager, in_flight={"abc:enhance"}))
        assert status.label == "Writing up · Standup"
        assert status.writing is True

    def test_recording_wins_over_writing(self) -> None:
        """Notes for the last call matter less than the live one."""
        manager = FakeManager([job("live"), job("old", terminal=True)])
        status = tray.read_status(fake_app(manager, in_flight={"old:enhance"}))
        assert status.label.startswith("Recording")

    def test_a_blocker_is_named_rather_than_hinted_at(self) -> None:
        status = tray.read_status(fake_app(health=blocked("Deepgram")))
        assert status.label == "Setup needed · Deepgram"
        assert status.ready is False
        assert status.can_start is False

    def test_an_expired_google_session_is_not_a_broken_scribe(self) -> None:
        """Local capture is the default path and does not touch Google, so a
        bot-only failure must not put "setup needed" in the tooltip."""
        status = tray.read_status(fake_app(health=blocked("Google sign-in", mode="bot")))
        assert status.label == "Not recording"
        assert status.can_start is True

    def test_before_the_checks_finish_it_claims_nothing(self) -> None:
        """An icon that says "setup needed" for the first few seconds of every
        sign-in teaches people to ignore it."""
        status = tray.read_status(fake_app())
        assert status.ready is True

    def test_before_the_server_is_up_it_says_starting(self) -> None:
        status = tray.read_status(SimpleNamespace(state=SimpleNamespace()))
        assert status.label == "Starting…"
        assert status.can_start is False

    def test_the_latest_finished_meeting_is_the_one_to_open(self) -> None:
        manager = FakeManager(
            [job("live"), job("recent", terminal=True), job("older", terminal=True)]
        )
        assert tray.read_status(fake_app(manager)).latest_id == "recent"

    def test_a_long_name_is_cut_not_wrapped(self) -> None:
        manager = FakeManager([job(title="Quarterly planning " * 6)])
        status = tray.read_status(fake_app(manager))
        assert len(status.label) < 60
        assert status.label.endswith("…")

    def test_the_tooltip_fits_what_windows_will_show(self) -> None:
        """Longer than this and Windows cuts it mid-word without saying so."""
        manager = FakeManager([job(title="x" * 400)])
        assert len(tray.tooltip(tray.read_status(fake_app(manager)))) <= 127

    def test_a_meeting_with_no_name_still_has_one(self) -> None:
        manager = FakeManager([SimpleNamespace(
            id="j", title="", meet_url="", kind="local",
            status=SimpleNamespace(is_terminal=False), elapsed_s=1.0,
        )])
        assert tray.read_status(fake_app(manager)).label == "Recording · a meeting"


class TestTheMenu:
    def test_it_opens_with_the_status_rather_than_a_title(self) -> None:
        """The one glance somebody gives a tray menu should answer the
        question they opened it with."""
        items = tray.menu_for(tray.Status(label="Recording · Standup"))
        assert items[0].label == "Recording · Standup"
        assert items[0].enabled is False
        assert items[0].action is None

    def test_clicking_the_icon_opens_the_page(self) -> None:
        items = tray.menu_for(tray.Status(label="Not recording"))
        defaults = [item for item in items if item.default]
        assert len(defaults) == 1
        assert defaults[0].label == "Open Scribe"
        assert defaults[0].action == "open"

    def test_idle_offers_to_start(self) -> None:
        labels = [item.label for item in tray.menu_for(tray.Status(label="x"))]
        assert "Start recording" in labels
        assert "Quit Scribe" in labels

    def test_recording_offers_to_stop(self) -> None:
        status = tray.Status(label="x", recording=True, live_id="abc")
        labels = [item.label for item in tray.menu_for(status)]
        assert "Stop recording" in labels
        assert "Start recording" not in labels

    def test_quitting_mid_meeting_says_what_it_will_do(self) -> None:
        status = tray.Status(label="x", recording=True, live_id="abc")
        labels = [item.label for item in tray.menu_for(status)]
        assert "Stop recording and quit" in labels
        assert "Quit Scribe" not in labels

    def test_an_unrecordable_machine_greys_the_item_rather_than_hiding_it(self) -> None:
        """A missing item reads as a missing feature."""
        status = tray.Status(label="x", can_start=False)
        start = [i for i in tray.menu_for(status) if i.label == "Start recording"]
        assert start and start[0].enabled is False

    def test_nothing_recorded_yet_means_nothing_to_open(self) -> None:
        latest = [
            item
            for item in tray.menu_for(tray.Status(label="x"))
            if item.label == "Latest notes"
        ]
        assert latest and latest[0].enabled is False

    def test_every_action_is_one_the_tray_knows(self) -> None:
        """A label wired to a typo would be a menu item that does nothing."""
        handled = {"open", "toggle", "latest", "folder", "log", "quit"}
        for status in (
            tray.Status(label="x"),
            tray.Status(label="x", recording=True, live_id="a", latest_id="b"),
        ):
            for item in tray.menu_for(status, log=True):
                assert item.action is None or item.action in handled

    def test_separators_do_not_double_up(self) -> None:
        items = tray.menu_for(tray.Status(label="x"), log=False, folder=False)
        pairs = zip(items, items[1:])
        assert not any(a.separator and b.separator for a, b in pairs)
        assert not items[0].separator and not items[-1].separator


class TestWhatTheMenuHolds:
    def test_the_log_is_only_offered_when_there_is_one(self, tmp_path: Path) -> None:
        icon = tray.ScribeTray(
            fake_app(),
            url="http://127.0.0.1:8080",
            on_quit=lambda: None,
            log_path=tmp_path / "scribe-server.log",
            output_dir=tmp_path,
        )
        assert "Server log" not in [item.label for item in icon.items()]
        (tmp_path / "scribe-server.log").write_text("started", encoding="utf-8")
        assert "Server log" in [item.label for item in icon.items()]

    def test_no_recordings_folder_no_item(self) -> None:
        icon = tray.ScribeTray(
            fake_app(), url="http://127.0.0.1:8080", on_quit=lambda: None
        )
        assert "Recordings folder" not in [item.label for item in icon.items()]


class TestClickingThings:
    @pytest.fixture
    def icon(self, server_loop, monkeypatch, tmp_path: Path):
        """A tray wired to a fake service, with the browser stubbed out."""
        opened: list[str] = []
        monkeypatch.setattr(tray.webbrowser, "open", lambda url: opened.append(url))
        manager = FakeManager()
        app = fake_app(manager, loop=server_loop)
        icon = tray.ScribeTray(
            app,
            url="http://127.0.0.1:8080/",
            on_quit=lambda: None,
            output_dir=tmp_path,
        )
        icon.opened = opened  # type: ignore[attr-defined]
        icon.manager = manager  # type: ignore[attr-defined]
        return icon

    def test_open_goes_to_the_page(self, icon) -> None:
        icon.invoke("open")
        assert icon.opened == ["http://127.0.0.1:8080"]

    def test_starting_records_this_machine_under_the_admin(
        self, icon, monkeypatch
    ) -> None:
        """An unowned recording is admin-only, which would hide it from the
        very account whose machine it is."""
        monkeypatch.setattr(
            "meetbot.capture.local.capture_available", lambda: (True, "ready")
        )
        icon.invoke("toggle")
        assert len(icon.manager.started) == 1
        assert icon.manager.started[0]["owner"] == "neil"
        assert icon.manager.started[0]["title"].startswith("Recording ")

    def test_a_machine_that_cannot_be_recorded_says_why(
        self, icon, monkeypatch
    ) -> None:
        """Rejected here rather than started and abandoned: a job that dies two
        seconds in looks like a recording that happened."""
        monkeypatch.setattr(
            "meetbot.capture.local.capture_available",
            lambda: (False, "No loopback device"),
        )
        said: list[str] = []
        monkeypatch.setattr(icon, "notify", said.append)
        icon.invoke("toggle")
        assert icon.manager.started == []
        assert said == ["No loopback device"]

    def test_a_stale_menu_cannot_start_a_second_recording(
        self, icon, monkeypatch
    ) -> None:
        """Windows keeps the menu it was last given. If a recording started
        elsewhere - the page, a voice command - the icon's "Start recording"
        has to stop it, not record the same call twice."""
        monkeypatch.setattr(
            "meetbot.capture.local.capture_available", lambda: (True, "ready")
        )
        icon.manager.jobs.append(job("live"))
        icon.invoke("toggle")
        assert icon.manager.started == []
        assert icon.manager.stopped == ["live"]

    def test_a_meeting_that_ends_between_the_read_and_the_click_is_reported(
        self, icon, monkeypatch
    ) -> None:
        """The menu decides from a status read a moment earlier, and a call can
        end in that moment. It reports it rather than pretending it stopped."""
        with pytest.raises(tray.TrayError, match="already finished"):
            icon.stop_recording("gone")

        # The status the click was decided from, for a job the manager no
        # longer has: exactly what a call ending a moment earlier looks like.
        said: list[str] = []
        monkeypatch.setattr(icon, "notify", said.append)
        monkeypatch.setattr(
            icon,
            "status",
            lambda: tray.Status(label="x", recording=True, live_id="live"),
        )
        icon.invoke("toggle")
        assert said == ["That recording had already finished."]

    def test_latest_notes_opens_that_meeting(self, icon) -> None:
        icon.manager.jobs.append(job("done", terminal=True))
        icon.invoke("latest")
        assert icon.opened == ["http://127.0.0.1:8080/?meeting=done"]

    def test_latest_notes_with_nothing_recorded_says_so(
        self, icon, monkeypatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(icon, "notify", said.append)
        icon.invoke("latest")
        assert said == ["Nothing has been recorded yet."]
        assert icon.opened == []

    def test_a_failing_click_does_not_take_the_icon_down(
        self, icon, monkeypatch
    ) -> None:
        """The message loop runs on the thread the click arrives on: an
        exception out of here is an icon that disappears when pressed."""
        said: list[str] = []
        monkeypatch.setattr(icon, "notify", said.append)
        monkeypatch.setattr(
            icon, "open_page", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        icon.invoke("open")
        assert said and "log" in said[0]

    def test_an_unknown_action_is_ignored(self, icon) -> None:
        icon.invoke("teleport")
        assert icon.opened == []

    def test_work_runs_on_the_servers_loop_not_the_menus(self, icon) -> None:
        """Jobs are tasks on that loop; starting or cancelling one from the
        icon's thread is how you get a task on a loop nobody is running."""
        where = icon._on_server(lambda: threading.current_thread().name)
        assert where != threading.current_thread().name

    def test_a_wedged_server_gives_up_rather_than_hanging_the_menu(
        self, icon
    ) -> None:
        async def never() -> None:
            await asyncio.sleep(30)

        with pytest.raises(tray.TrayError):
            icon._on_server(never, timeout=0.3)

    def test_before_the_loop_exists_nothing_is_attempted(self, tmp_path: Path) -> None:
        icon = tray.ScribeTray(
            SimpleNamespace(state=SimpleNamespace()),
            url="http://127.0.0.1:8080",
            on_quit=lambda: None,
        )
        with pytest.raises(tray.TrayError):
            icon.stop_recording("anything")


class TestQuitting:
    def test_it_asks_the_server_to_finish(self) -> None:
        shut: list[bool] = []
        icon = tray.ScribeTray(
            fake_app(), url="http://127.0.0.1:8080", on_quit=lambda: shut.append(True)
        )
        icon.quit()
        assert shut == [True]

    def test_quitting_twice_only_shuts_down_once(self) -> None:
        """Two clicks while the write-up finishes must not double-join."""
        shut: list[bool] = []
        icon = tray.ScribeTray(
            fake_app(), url="http://127.0.0.1:8080", on_quit=lambda: shut.append(True)
        )
        icon.quit()
        icon.quit()
        assert shut == [True]

    def test_mid_meeting_it_says_what_it_is_waiting_for(self, monkeypatch) -> None:
        said: list[str] = []
        icon = tray.ScribeTray(
            fake_app(FakeManager([job("live")])),
            url="http://127.0.0.1:8080",
            on_quit=lambda: None,
        )
        monkeypatch.setattr(icon, "notify", said.append)
        icon.quit()
        assert said and "Finishing" in said[0]

    def test_a_failing_shutdown_still_stops_the_icon(self) -> None:
        """Otherwise Quit leaves an icon that no longer does anything."""
        stopped: list[bool] = []
        icon = tray.ScribeTray(
            fake_app(),
            url="http://127.0.0.1:8080",
            on_quit=lambda: (_ for _ in ()).throw(RuntimeError("no")),
        )
        icon._icon = SimpleNamespace(
            visible=True, stop=lambda: stopped.append(True), notify=lambda *a: None
        )
        with pytest.raises(RuntimeError):
            icon.quit()
        assert stopped == [True]


class TestTheServerOnAThread:
    """uvicorn off the main thread, which is what leaves the icon its own."""

    @staticmethod
    async def _app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"<title>Scribe</title>"})

    def test_it_serves_and_then_stops(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])

        server, thread = tray.background_server(
            self._app, port=port, log_level="warning"
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not getattr(server, "started", False):
                time.sleep(0.1)
            assert getattr(server, "started", False), "uvicorn never came up"
        finally:
            assert tray.stop_server(server, thread, timeout=15) is True


class TestTheMenuPystrayIsGiven:
    """The translation layer, against the real library."""

    @pytest.fixture
    def icon(self):
        pytest.importorskip("pystray")
        pytest.importorskip("PIL")
        return tray.ScribeTray(
            fake_app(), url="http://127.0.0.1:8080", on_quit=lambda: None
        )

    def test_it_can_be_shown_here(self) -> None:
        pytest.importorskip("pystray")
        ready, detail = tray.available()
        assert ready is True
        assert detail

    def test_every_line_survives_the_translation(self, icon) -> None:
        described = [item for item in icon.items() if not item.separator]
        entries = [entry for entry in icon._entries() if entry.text != "- - - -"]
        assert [entry.text for entry in entries] == [item.label for item in described]

    def test_a_click_reaches_the_action(self, icon, monkeypatch) -> None:
        """pystray inspects a callback's argument count and refuses a lambda
        that carries its action as a third argument - this is that guard."""
        clicked: list[str] = []
        monkeypatch.setattr(icon, "invoke", clicked.append)
        entry = next(e for e in icon._entries() if e.text == "Open Scribe")
        entry(None)
        assert clicked == ["open"]

    def test_the_disabled_status_line_is_not_clickable(self, icon) -> None:
        entry = next(iter(icon._entries()))
        assert entry.enabled is False
        entry(None)  # must not raise

    def test_the_icon_it_builds_carries_the_mark_and_the_tooltip(self, icon) -> None:
        built = icon.build()
        assert built.icon.size == (tray.ICON_SIZE, tray.ICON_SIZE)
        assert built.title.startswith("Scribe")
        assert list(built.menu)


class TestTheCommandBehindTheShortcuts:
    def test_no_icon_still_serves(self, monkeypatch) -> None:
        """The shortcuts run `tray`. On a machine that cannot draw an icon it
        has to fall back to serving: a Scribe you cannot see still records the
        call, and the alternative is a double-click that does nothing."""
        import argparse

        from meetbot import cli

        monkeypatch.setattr(tray, "available", lambda: (False, "no pystray here"))
        served: list[Any] = []
        monkeypatch.setattr(cli, "cmd_serve", lambda args: served.append(args) or 0)

        code = cli.cmd_tray(
            argparse.Namespace(
                port=8080,
                host="127.0.0.1",
                open_browser=False,
                log_level=None,
                env_file=None,
                llm_provider=None,
                llm_model=None,
            )
        )

        assert code == 0
        assert len(served) == 1

    def test_it_is_a_subcommand_the_parser_knows(self) -> None:
        from meetbot.cli import build_parser

        args = build_parser().parse_args(["tray", "--port", "9100"])
        assert args.command == "tray" and args.port == 9100


class TestKeepingItVisible:
    """Windows 11 hides an icon it has not seen before, and there is no API.

    These run against the real registry, under a key of their own that is
    removed afterwards - the value being written is undocumented, so a fake
    would only prove the fake works.
    """

    KEY = r"Software\Scribe Tests\NotifyIconSettings"

    @pytest.fixture
    def entries(self):
        """Two filed icons: ours, and something else's."""
        if sys.platform != "win32":
            pytest.skip("the notification area is Windows-only")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as root:
            with winreg.CreateKey(root, "ours") as entry:
                winreg.SetValueEx(
                    entry, "InitialTooltip", 0, winreg.REG_SZ, "Scribe — Not recording"
                )
                winreg.SetValueEx(
                    entry, "ExecutablePath", 0, winreg.REG_SZ, sys.executable.upper()
                )
            with winreg.CreateKey(root, "theirs") as entry:
                winreg.SetValueEx(
                    entry, "InitialTooltip", 0, winreg.REG_SZ, "Some other app"
                )
                winreg.SetValueEx(
                    entry, "ExecutablePath", 0, winreg.REG_SZ, sys.executable
                )
            with winreg.CreateKey(root, "half-written"):
                pass  # an entry with no values at all, which Windows does have
        yield winreg
        for name in ("ours", "theirs", "half-written"):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{self.KEY}\{name}")
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.KEY)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Scribe Tests")

    def _value(self, winreg, name: str) -> Any:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{self.KEY}\{name}") as entry:
            try:
                return winreg.QueryValueEx(entry, "IsPromoted")[0]
            except OSError:
                return None

    def test_it_marks_our_icon(self, entries) -> None:
        assert tray.promote(key=self.KEY) is True
        assert self._value(entries, "ours") == 1

    def test_it_leaves_everyone_elses_alone(self, entries) -> None:
        """This value decides where somebody's icon sits. Ours is the only one
        we have any business writing."""
        tray.promote(key=self.KEY)
        assert self._value(entries, "theirs") is None
        assert self._value(entries, "half-written") is None

    def test_a_different_interpreter_is_not_ours(self, entries) -> None:
        """Two Pythons on one machine, and only one of them is this Scribe."""
        assert tray.promote(key=self.KEY, executable="C:/elsewhere/python.exe") is False
        assert self._value(entries, "ours") is None

    def test_doing_it_twice_is_not_an_error(self, entries) -> None:
        assert tray.promote(key=self.KEY) is True
        assert tray.promote(key=self.KEY) is True
        assert self._value(entries, "ours") == 1

    def test_nothing_filed_yet_is_not_a_failure(self) -> None:
        """Before Windows has seen the icon there is simply nothing to mark."""
        assert tray.promote(key=r"Software\Scribe Tests\Nothing Here") is False
