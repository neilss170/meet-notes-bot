"""Starting Scribe without a terminal: detection, shortcuts, and the icon.

Nothing here touches the real Desktop or the real Startup folder - the two
places where a test that "just tried it" would leave something behind on the
developer's machine. Where Windows is unavoidable, the test says so and skips.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from meetbot import desktop


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(body: str):
    """A one-page HTTP server on a free port, stopped by the caller."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the base class names it
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):  # keep the suite output clean
            return

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestIsItRunning:
    def test_finds_scribe_on_the_port(self) -> None:
        server, port = _serve("<title>Scribe</title><h1>Sign in</h1>")
        try:
            assert desktop.is_running(port) is True
        finally:
            server.shutdown()

    def test_something_else_on_the_port_is_not_scribe(self) -> None:
        """Otherwise a launch hands somebody their browser pointed at it."""
        server, port = _serve("<title>Jupyter</title>")
        try:
            assert desktop.is_running(port) is False
        finally:
            server.shutdown()

    def test_nothing_listening_is_not_running(self) -> None:
        assert desktop.is_running(_free_port()) is False

    def test_waiting_gives_up_rather_than_hanging(self) -> None:
        assert desktop.wait_until_running(_free_port(), timeout=0.6) is False


class TestWhereItRuns:
    def test_the_project_directory_is_the_one_with_the_env_in_it(self) -> None:
        """A shortcut started anywhere else finds no .env and no recordings."""
        directory = desktop.project_dir()
        assert (directory / "pyproject.toml").exists() or (directory / ".env").exists()

    def test_the_background_interpreter_leaves_no_console(self) -> None:
        chosen = desktop.background_python()
        if sys.platform != "win32":
            assert chosen == Path(sys.executable)
        else:
            # pythonw.exe sits beside python.exe in every normal install, but
            # an embedded or stripped one may not have it.
            assert chosen.name in ("pythonw.exe", Path(sys.executable).name)


class TestStartingIt:
    def test_it_starts_the_server_detached_and_keeps_the_log(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(desktop, "project_dir", lambda: tmp_path)
        seen: dict = {}

        def fake_popen(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        log_path = desktop.start_detached(8123)

        assert log_path == tmp_path / desktop.SERVER_LOG
        assert log_path.exists(), "there is nowhere to read a failure from"
        # `tray`, not `serve`: a windowless server with no icon is a process
        # you can only stop from Task Manager.
        assert seen["command"][1:] == ["-m", "meetbot", "tray", "--port", "8123"]
        assert seen["kwargs"]["cwd"] == str(tmp_path)
        # Closing the window that started it must not take a meeting down.
        if sys.platform == "win32":
            assert seen["kwargs"]["creationflags"] & subprocess.DETACHED_PROCESS
        else:
            assert seen["kwargs"]["start_new_session"] is True


class TestShortcuts:
    @pytest.fixture
    def folders(self, monkeypatch, tmp_path):
        """Stand-ins for the Desktop and Startup folders."""
        desktop_dir = tmp_path / "Desktop"
        startup_dir = tmp_path / "Startup"
        desktop_dir.mkdir()
        startup_dir.mkdir()
        monkeypatch.setattr(
            desktop,
            "windows_folder",
            lambda name: desktop_dir if name == desktop.DESKTOP_FOLDER else startup_dir,
        )
        made: list[dict] = []

        def fake_link(link, target, arguments, icon):
            made.append(
                {"link": link, "target": target, "args": arguments, "icon": icon}
            )
            link.write_text("shortcut", encoding="utf-8")
            return True

        monkeypatch.setattr(desktop, "_make_shortcut", fake_link)
        return desktop_dir, startup_dir, made

    def test_the_desktop_icon_launches_rather_than_serves(self, folders) -> None:
        """Pressing it twice should open the page, not fail to bind the port."""
        desktop_dir, _, made = folders

        links = desktop.install(port=8080)

        assert links == [desktop_dir / "Scribe.lnk"]
        assert "-m meetbot launch --port 8080" == made[0]["args"]

    def test_signing_in_starts_the_server_without_a_browser(self, folders) -> None:
        _, startup_dir, made = folders

        links = desktop.install(port=9000, startup=True)

        assert startup_dir / "Scribe.lnk" in links
        startup = next(m for m in made if m["link"].parent == startup_dir)
        assert startup["args"] == "-m meetbot tray --port 9000"
        assert "launch" not in startup["args"], "it would open a browser at sign-in"
        assert "--open" not in startup["args"]

    def test_startup_is_opt_in(self, folders) -> None:
        _, startup_dir, _ = folders
        desktop.install(port=8080)
        assert not (startup_dir / "Scribe.lnk").exists()

    def test_removing_takes_both_away(self, folders) -> None:
        desktop_dir, startup_dir, _ = folders
        desktop.install(port=8080, startup=True)

        gone = desktop.remove()

        assert set(gone) == {desktop_dir / "Scribe.lnk", startup_dir / "Scribe.lnk"}
        assert desktop.installed() == []

    def test_removing_nothing_is_not_an_error(self, folders) -> None:
        assert desktop.remove() == []


class TestTheIcon:
    def test_it_writes_a_real_icon_file(self, tmp_path) -> None:
        pytest.importorskip("PIL", reason="Pillow is not installed")
        path = tmp_path / "scribe.ico"

        assert desktop.write_icon(path) is True

        # The ICO header: reserved 0, type 1 (icon), then the image count.
        assert path.read_bytes()[:4] == b"\x00\x00\x01\x00"
        assert path.stat().st_size > 500

    def test_it_reads_at_the_size_the_notification_area_asks_for(self) -> None:
        """16 pixels is what Windows gives a tray icon on a 1080p display."""
        pytest.importorskip("PIL", reason="Pillow is not installed")
        small = desktop.spark_image(16)
        assert small is not None and small.size == (16, 16)
        # Something was actually drawn: the corners are transparent, the
        # middle is not.
        assert small.getpixel((8, 8))[3] == 255
        assert small.getpixel((0, 0))[3] == 0

    def test_recording_turns_the_whole_tile_red(self) -> None:
        """A dot in the corner of a 16-pixel icon is a grey smudge. The tile
        itself changes, so it can be told apart at a glance in a row of
        monochrome system icons."""
        pytest.importorskip("PIL", reason="Pillow is not installed")
        idle = desktop.spark_image(32)
        live = desktop.spark_image(32, recording=True)
        assert idle is not None and live is not None
        assert idle.tobytes() != live.tobytes()
        # The tile behind the mark, a few pixels in from the top edge.
        live_tile = live.getpixel((16, 4))
        idle_tile = idle.getpixel((16, 4))
        assert live_tile[0] > 200 and live_tile[1] < 120, "the tile is not red"
        assert max(idle_tile[:3]) < 60, "the idle tile is no longer dark"

    def test_a_missing_pillow_is_not_fatal(self, monkeypatch, tmp_path) -> None:
        """No icon is a plain-looking shortcut, not a failed install."""
        import builtins

        real_import = builtins.__import__

        def no_pillow(name, *args, **kwargs):
            if name.startswith("PIL"):
                raise ImportError("no Pillow here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pillow)
        assert desktop.write_icon(tmp_path / "scribe.ico") is False


class TestElsewhere:
    def test_another_platform_is_told_what_to_do_instead(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ready, detail = desktop.available()
        assert ready is False
        assert "serve" in detail, "it has to say what to run instead"
