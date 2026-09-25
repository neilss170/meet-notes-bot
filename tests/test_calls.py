"""Call alerts: noticing an app take the microphone, and what happens next.

The registry and the window list are faked throughout, apart from one smoke
test that reads the real registry on Windows.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from meetbot.capture.calls import (
    GRACE_S,
    CallPrompt,
    CallWatcher,
    MicSession,
    app_display_name,
    read_mic_sessions,
    toast_xml,
    web_call_in,
)

_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000

WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
CHROME = "C:#Program Files#Google#Chrome#Application#chrome.exe"
SCRIBE = "C:#Python#python.exe"


def _filetime(epoch: float) -> int:
    return int(epoch * 10_000_000) + _FILETIME_UNIX_EPOCH


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeMicrophone:
    """Stands in for the registry: who is holding the microphone right now."""

    def __init__(self) -> None:
        self.sessions: list[MicSession] = []

    def __call__(self) -> list[MicSession]:
        return list(self.sessions)

    def take(self, app_id: str, *, at: float, packaged: bool = False) -> MicSession:
        session = MicSession(app_id, _filetime(at), packaged)
        self.sessions.append(session)
        return session

    def release(self, session: MicSession) -> None:
        self.sessions.remove(session)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mic() -> FakeMicrophone:
    return FakeMicrophone()


@pytest.fixture
def titles() -> list[str]:
    return []


@pytest.fixture
def watcher(tmp_path: Path, clock, mic, titles) -> CallWatcher:
    return CallWatcher(
        tmp_path / "call-alerts.json",
        reader=mic,
        titles=lambda: list(titles),
        clock=clock,
        own_executables=[r"C:\Python\python.exe"],
    )


def _settle(watcher: CallWatcher, clock: Clock) -> list[CallPrompt]:
    """Poll, wait out the grace period, and poll again."""
    watcher.poll()
    clock.advance(GRACE_S)
    return watcher.poll()


# --- naming ------------------------------------------------------------------


class TestNaming:
    @pytest.mark.parametrize(
        "app_id, packaged, name",
        [
            (WHATSAPP, True, "WhatsApp"),
            ("MSTeams_8wekyb3d8bbwe", True, "Microsoft Teams"),
            ("Microsoft.YourPhone_8wekyb3d8bbwe", True, "Phone Link"),
            (CHROME, False, "Chrome"),
            ("C:#Program Files (x86)#Microsoft#Edge#Application#msedge.exe", False, "Edge"),
            ("C:#Users#n#AppData#Roaming#Zoom#bin#Zoom.exe", False, "Zoom"),
            ("C:#Users#n#AppData#Local#CiscoSparkLauncher#CiscoCollabHost.exe", False, "Webex"),
        ],
    )
    def test_known_apps_get_their_real_names(
        self, app_id: str, packaged: bool, name: str
    ) -> None:
        assert app_display_name(app_id, packaged=packaged) == name

    def test_an_app_nobody_listed_is_still_named(self) -> None:
        """Detection does not depend on the list - it only tidies names."""
        assert app_display_name("C:#Games#cs2.exe", packaged=False) == "cs2"
        assert app_display_name("Contoso.Dialer_abc123", packaged=True) == "Dialer"

    def test_steam_is_not_mistaken_for_teams(self) -> None:
        name = app_display_name("C:#Program Files (x86)#Steam#steam.exe", packaged=False)
        assert name == "steam"

    def test_a_meet_tab_is_recognised_with_its_code(self) -> None:
        assert web_call_in(["Meet - abc-defg-hij - Google Chrome"]) == (
            "Google Meet",
            "abc-defg-hij",
        )

    def test_whatsapp_web_is_recognised(self) -> None:
        assert web_call_in(["(3) WhatsApp - Google Chrome"]) == ("WhatsApp", "")

    def test_ordinary_windows_name_no_service(self) -> None:
        assert web_call_in(
            ["app.py - meet-notes-bot - Visual Studio Code", "Scribe - Google Chrome"]
        ) == ("", "")


# --- the watcher -------------------------------------------------------------


class TestWatcher:
    def test_a_call_is_offered_once_it_has_held_the_microphone(
        self, watcher, mic, clock
    ) -> None:
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        assert watcher.poll() == [], "not before the grace period"
        assert watcher.pending() == []
        clock.advance(GRACE_S)
        [prompt] = watcher.poll()
        assert prompt.app == "WhatsApp"
        assert prompt.title == "WhatsApp call"
        assert watcher.pending() == [prompt]

    def test_each_call_is_announced_once(self, watcher, mic, clock) -> None:
        """A notification every two seconds for one call would be unbearable."""
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        assert len(_settle(watcher, clock)) == 1
        clock.advance(10)
        assert watcher.poll() == []
        assert len(watcher.pending()) == 1, "still waiting for an answer, though"

    def test_the_offer_goes_when_the_call_ends(self, watcher, mic, clock) -> None:
        session = mic.take(WHATSAPP, at=clock.now, packaged=True)
        _settle(watcher, clock)
        mic.release(session)
        watcher.poll()
        assert watcher.pending() == []

    def test_a_brief_blip_is_not_a_call(self, watcher, mic, clock) -> None:
        session = mic.take(CHROME, at=clock.now)
        watcher.poll()
        clock.advance(1)
        mic.release(session)
        watcher.poll()
        clock.advance(GRACE_S)
        assert watcher.poll() == []
        assert watcher.pending() == []

    def test_scribe_recording_is_never_a_call(self, watcher, mic, clock) -> None:
        mic.take(SCRIBE, at=clock.now)
        assert _settle(watcher, clock) == []

    def test_nothing_is_offered_while_already_recording(
        self, watcher, mic, clock
    ) -> None:
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        watcher.poll(recording=True)
        clock.advance(GRACE_S)
        assert watcher.poll(recording=True) == []
        # Stopping the recording mid-call must not immediately offer to start
        # another one for the same call.
        assert watcher.poll(recording=False) == []

    def test_the_next_call_is_offered_after_a_recorded_one(
        self, watcher, mic, clock
    ) -> None:
        first = mic.take(WHATSAPP, at=clock.now, packaged=True)
        watcher.poll(recording=True)
        mic.release(first)
        watcher.poll()
        clock.advance(600)
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        assert len(_settle(watcher, clock)) == 1

    def test_dismissing_hides_this_call_but_not_the_next(
        self, watcher, mic, clock
    ) -> None:
        session = mic.take(WHATSAPP, at=clock.now, packaged=True)
        [prompt] = _settle(watcher, clock)
        assert watcher.dismiss(prompt.id) is True
        assert watcher.pending() == []
        mic.release(session)
        watcher.poll()
        clock.advance(60)
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        assert len(_settle(watcher, clock)) == 1

    def test_dismissing_a_call_that_ended_says_so(self, watcher) -> None:
        assert watcher.dismiss("no-such-call") is False

    def test_marking_a_call_handled_hides_it(self, watcher, mic, clock) -> None:
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        [prompt] = _settle(watcher, clock)
        watcher.mark_handled(prompt.id)
        assert watcher.pending() == []

    def test_muting_an_app_lasts_across_restarts(
        self, tmp_path, watcher, mic, clock
    ) -> None:
        mic.take(WHATSAPP, at=clock.now, packaged=True)
        _settle(watcher, clock)
        watcher.mute(WHATSAPP)
        assert watcher.pending() == []

        restarted = CallWatcher(
            tmp_path / "call-alerts.json", reader=mic, clock=clock, own_executables=[]
        )
        assert restarted.muted() == [{"app_id": WHATSAPP, "app": "WhatsApp"}]
        assert _settle(restarted, clock) == []

        assert restarted.unmute(WHATSAPP) is True
        assert len(restarted.pending()) == 1

    def test_a_browser_call_is_named_after_its_tab(
        self, watcher, mic, clock, titles
    ) -> None:
        titles.append("Meet - abc-defg-hij - Google Chrome")
        mic.take(CHROME, at=clock.now)
        [prompt] = _settle(watcher, clock)
        assert prompt.app == "Chrome"
        assert prompt.service == "Google Meet"
        assert prompt.title == "Google Meet abc-defg-hij"

    def test_a_tab_that_is_still_loading_is_relabelled(
        self, watcher, mic, clock, titles
    ) -> None:
        titles.append("New Tab - Google Chrome")
        mic.take(CHROME, at=clock.now)
        watcher.poll()
        titles[:] = ["Meet - abc-defg-hij - Google Chrome"]
        clock.advance(GRACE_S)
        [prompt] = watcher.poll()
        assert prompt.service == "Google Meet"

    def test_a_session_left_by_a_crashed_app_is_ignored(
        self, watcher, mic, clock
    ) -> None:
        """An app that died holding the microphone never records a stop time."""
        mic.take(WHATSAPP, at=clock.now - 7 * 3600, packaged=True)
        assert _settle(watcher, clock) == []

    def test_unreadable_window_titles_do_not_stop_the_alert(
        self, tmp_path, mic, clock
    ) -> None:
        def broken() -> list[str]:
            raise OSError("no desktop")

        watcher = CallWatcher(
            tmp_path / "c.json", reader=mic, titles=broken, clock=clock, own_executables=[]
        )
        mic.take(CHROME, at=clock.now)
        [prompt] = _settle(watcher, clock)
        assert prompt.title == "Chrome call"


# --- the notification --------------------------------------------------------


class TestNotification:
    def test_the_notification_links_back_to_the_call(self) -> None:
        prompt = CallPrompt("abc123", WHATSAPP, "AT&T <Dialer>", "", "AT&T call", 0.0)
        document = ElementTree.fromstring(toast_xml(prompt, "http://127.0.0.1:8080/"))
        assert document.get("launch") == "http://127.0.0.1:8080/?call=abc123"
        record = next(
            a for a in document.iter("action") if a.get("content") == "Start recording"
        )
        assert record.get("arguments") == "http://127.0.0.1:8080/?record=abc123"
        texts = [t.text for t in document.iter("text")]
        assert texts[0] == "AT&T <Dialer> call detected"


@pytest.mark.skipif(sys.platform != "win32", reason="the microphone registry is Windows-only")
def test_the_real_registry_can_be_read() -> None:
    sessions = read_mic_sessions()
    assert isinstance(sessions, list)
    assert all(isinstance(s, MicSession) for s in sessions)
