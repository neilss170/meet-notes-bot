"""Hearing an instruction without listening to the meeting.

Almost nothing here needs a microphone: the understanding is a pure function,
and the listener is driven by a fake process that says whatever the test wants
it to. The one test that exercises Windows' own recogniser does it by having
Windows speak to itself, so the suite proves the real chain without anybody
being present to talk to it.
"""

from __future__ import annotations

import sys
import threading

import pytest

from meetbot.capture import voice as voice_module
from meetbot.capture import voice
from meetbot.capture.voice import (
    MAX_TITLE,
    MIN_CONFIDENCE,
    VoiceListener,
    check_recogniser,
    command_of,
    voice_available,
)


class TestUnderstandingWhatWasSaid:
    @pytest.mark.parametrize(
        "said, kind",
        [
            ("scribe start recording", "start"),
            ("Scribe Start Recording", "start"),
            ("scribe record this", "start"),
            ("scribe stop recording", "stop"),
            ("scribe stop", "stop"),
            ("scribe mark this", "mark"),
            ("scribe bookmark this", "mark"),
        ],
    )
    def test_each_phrase_means_what_it_says(self, said: str, kind: str) -> None:
        command = command_of(said, 0.95)
        assert command is not None, said
        assert command.kind == kind

    def test_ordinary_talk_is_never_an_instruction(self) -> None:
        """The property that makes leaving a microphone open tolerable.

        The recogniser is given a closed list of phrases, but nothing stops a
        near-miss arriving anyway, so the parsing refuses those too.
        """
        for said in (
            "the migration script is two weeks behind",
            "we should ship it on friday",
            "scribe is the tool we are building",
            "start recording the meeting",
            "i asked scribe to stop by",
        ):
            assert command_of(said, 0.99) is None, said

    def test_a_mishearing_is_not_obeyed(self) -> None:
        """Starting a recording nobody asked for is the expensive mistake."""
        assert command_of("scribe start recording", MIN_CONFIDENCE - 0.01) is None
        assert command_of("scribe start recording", MIN_CONFIDENCE) is not None

    def test_naming_takes_whatever_follows(self) -> None:
        command = command_of("scribe call this the finance sync", 0.95)
        assert command is not None
        assert command.kind == "name"
        assert command.title == "The finance sync"

    def test_naming_nothing_is_not_a_name(self) -> None:
        assert command_of("scribe call this", 0.95) is None
        assert command_of("scribe name this    ", 0.95) is None

    def test_a_dictated_title_is_capped(self) -> None:
        command = command_of("scribe call this " + "a long name " * 20, 0.95)
        assert command is not None
        assert len(command.title) == MAX_TITLE


class _FakeRecogniser:
    """A recogniser process that says exactly what a test wants it to.

    Its output does not end when the lines run out, because a real one's does
    not either: the microphone keeps being open, and the stream is closed by
    terminating the process. A fake that ended immediately would look like a
    recogniser that had died on startup.
    """

    def __init__(self, lines: list[str]) -> None:
        self.drained = threading.Event()
        self.stderr = None
        self.terminated = False
        self._closed = threading.Event()
        self.stdout = self._speak(lines)

    def _speak(self, lines: list[str]):
        for line in lines:
            yield line
        self.drained.set()
        self._closed.wait(10)

    def terminate(self) -> None:
        self.terminated = True
        self._closed.set()

    def wait(self, timeout: float | None = None) -> int:
        return 0


READY = '{"ready":true}\n'


def _said(text: str, confidence: float = 0.95) -> str:
    return '{"text":"%s","confidence":%s,"grammar":"command"}\n' % (text, confidence)


class TestTheListener:
    @staticmethod
    def _listen(lines: list[str]):
        """Listen until everything has been said, then hand back what was heard."""
        heard: list = []
        process = _FakeRecogniser(lines)
        listener = VoiceListener(heard.append, spawn=lambda: process)
        assert listener.start() is True
        assert process.drained.wait(5), "the recogniser was never read"
        return listener, heard, process

    def test_commands_arrive_in_the_order_they_were_said(self) -> None:
        _, heard, _ = self._listen([
            READY,
            _said("scribe start recording"),
            _said("scribe call this the finance sync", 0.92),
            _said("scribe stop"),
        ])
        assert [command.kind for command in heard] == ["start", "name", "stop"]
        assert heard[1].title == "The finance sync"

    def test_noise_and_mishearings_never_reach_the_handler(self) -> None:
        _, heard, _ = self._listen([
            READY,
            "WARNING: something PowerShell wanted to mention\n",
            "\n",
            _said("scribe start recording", 0.2),
            _said("the budget is capped at forty thousand", 0.99),
        ])
        assert heard == []

    def test_what_it_throws_away_is_still_reported(self) -> None:
        """Silence is the worst way for this to fail.

        A command dropped for low confidence and a microphone that is not
        working look exactly the same from across the room, so anything heard
        and not acted on is handed over separately - with the reason, because
        the reasons want opposite responses and guessing between them is the
        whole of the frustration.

        The cases here are the ones the engine can really produce. It is
        handed a fixed list of phrases and a prefix-then-dictation grammar,
        and it only ever returns something one of those matched: an earlier
        version of this test fed it a sentence about a budget, which proved
        a branch that cannot be reached and missed the one that can.
        """
        heard: list = []
        unsure: list = []
        process = _FakeRecogniser([
            READY,
            # A phrase we know, under the floor: say it again.
            _said("scribe start recording", 0.55),
            # The naming prefix and then nothing. Saying it again more slowly
            # will not help; finishing the sentence will.
            '{"text":"scribe call this","confidence":0.88,"grammar":"naming"}\n',
            # Quiet enough to be the room rather than a person talking to us.
            _said("scribe stop", 0.05),
        ])
        listener = VoiceListener(
            heard.append,
            on_unsure=lambda said, sure, why: unsure.append((said, sure, why)),
            spawn=lambda: process,
        )
        assert listener.start() is True
        assert process.drained.wait(5), "the recogniser was never read"

        assert heard == [], "none of these should have been obeyed"
        assert [(said, why) for said, _sure, why in unsure] == [
            ("scribe start recording", voice.UNSURE),
            ("scribe call this", voice.NO_TITLE),
        ], "both near misses reported with their own reason, the room ignored"
        listener.stop()

    def test_the_reason_is_not_guessed_from_the_words(self) -> None:
        """Each answer sends somebody somewhere different.

        "Say it again more slowly" is right for a phrase that arrived under
        the confidence floor and wrong for one that arrived complete and
        meant nothing, and telling somebody to stop saying the thing that
        does work is worse than saying nothing at all. So the reason is taken
        from the grammar the engine reports rather than inferred afterwards.
        """
        # A phrase from the fixed list: heard, just not clearly enough.
        assert voice.unsure_reason("scribe stop recording", "command") == voice.UNSURE
        assert voice.unsure_reason("Scribe  Start  Recording") == voice.UNSURE

        # The prefix with a name is a working command, so if it was dropped
        # it was dropped for confidence.
        assert voice.unsure_reason(
            "scribe call this the finance sync", "naming"
        ) == voice.UNSURE
        # The prefix without one is a different problem.
        assert voice.unsure_reason("scribe call this", "naming") == voice.NO_TITLE
        assert voice.unsure_reason("scribe name this   ", "naming") == voice.NO_TITLE

        # Dictation matched but no prefix here did, which means this list and
        # the grammar the recogniser was given have drifted apart.
        assert voice.unsure_reason("scribe title this sprint", "naming") == voice.NO_TITLE

        # And the branch that needs the engine to be misconfigured to happen.
        assert voice.unsure_reason("what time is lunch", "command") == voice.UNKNOWN

    def test_it_does_not_claim_to_listen_before_the_microphone_opens(
        self, monkeypatch
    ) -> None:
        """Saying "listening" early would be a promise the page cannot keep."""
        monkeypatch.setattr(voice_module, "START_TIMEOUT_S", 0.2)
        process = _FakeRecogniser([_said("scribe start recording")])
        listener = VoiceListener(lambda command: None, spawn=lambda: process)
        try:
            assert listener.start() is False
            assert listener.listening is False
        finally:
            listener.stop()

    def test_stopping_lets_go_of_the_microphone(self) -> None:
        listener, _, process = self._listen([READY])
        listener.stop()
        assert process.terminated is True
        assert listener.listening is False
        assert listener.detail == "Not listening."

    def test_a_handler_that_explodes_does_not_deafen_it(self) -> None:
        heard: list = []

        def handler(command) -> None:
            if command.kind == "start":
                raise RuntimeError("the handler blew up")
            heard.append(command)

        process = _FakeRecogniser(
            [READY, _said("scribe start recording"), _said("scribe stop")]
        )
        listener = VoiceListener(handler, spawn=lambda: process)
        listener.start()
        assert process.drained.wait(5)
        listener.stop()
        assert [command.kind for command in heard] == ["stop"]

    def test_a_recogniser_that_will_not_start_is_reported_not_raised(self) -> None:
        def refuse():
            raise OSError("powershell.exe is not here")

        listener = VoiceListener(lambda command: None, spawn=refuse)
        assert listener.start() is False
        assert "powershell.exe is not here" in listener.detail


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows speech recognition")
class TestOnThisMachine:
    """The real recogniser, with nobody speaking to it."""

    def test_windows_says_whether_it_can_listen(self) -> None:
        ready, detail = voice_available()
        assert isinstance(ready, bool)
        assert detail, "an answer either way has to say something"

    def test_windows_understands_its_own_voice(self) -> None:
        """Proves the grammar, the engine, the JSON and the parsing at once."""
        ready, why = voice_available()
        if not ready:
            pytest.skip(why)
        heard, detail = check_recogniser()
        assert heard, detail
        assert "scribe start recording" in detail.lower()
