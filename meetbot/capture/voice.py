"""Taking an instruction out loud, without listening to the meeting.

Scribe already notices calls: any app that takes the microphone raises a card
and a notification offering to record. This is the other half - the recording
that is not a call, and the moment when your hands are not free because you
are talking to somebody.

**Why the Windows recogniser.** Windows ships an offline speech engine, and
this machine has ``MS-1033-80-DESK`` installed already. No model to download,
no new dependency, and - the part that matters for a tool that records
meetings - the audio never leaves the machine. Streaming an always-open
microphone to a cloud recogniser would make "nothing joins your meeting" a
lie told in a different place.

**Why a grammar rather than dictation.** The engine is given the exact phrases
it may hear. It cannot transcribe anything else, so an hour of ordinary
conversation produces nothing at all. Measured before this module was
written: spoken commands matched at 0.94-0.95, while a synthetic meeting about
migrations, budgets and Friday deadlines produced no match of any kind. That
property - not a confidence threshold - is what makes it tolerable to leave a
microphone open. Naming a meeting is the exception, because a title cannot be
enumerated in advance: "scribe call this" is a fixed prefix with free
dictation after it, and nothing is understood before the prefix.

**Why PowerShell.** The engine is .NET, reached the same way the call
notifications are. Hosting it in a child process also means a recogniser that
falls over cannot take the service with it, and that closing the process is
all it takes to let go of the microphone.

**What it costs.** The microphone is held while Scribe is idle, so Windows
shows its microphone indicator the whole time. That is a real thing to
consent to rather than a detail, which is why it is off unless VOICE_COMMANDS
says otherwise. Scribe's own call detection already ignores audio its own
process opened, so listening does not make Scribe think a call has started.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: The word every command starts with. Everything the recogniser is allowed to
#: hear hangs off it, which is most of why ordinary talk never matches.
WAKE_WORD = "scribe"

#: Below this a match is treated as mishearing rather than an instruction.
#: Correct commands were measured at 0.94-0.95; the only sub-0.9 match seen
#: was a partial "scribe stop" at 0.65 that the full phrase repeated a moment
#: later. Starting a recording nobody asked for is the expensive mistake here,
#: and "say it again" is the cheap one.
MIN_CONFIDENCE = 0.7

#: Below this, a recognition is the room rather than somebody talking to
#: Scribe, and saying so would fill the panel with chatter. Between this and
#: MIN_CONFIDENCE is the interesting band: heard, understood, not acted on.
MIN_REPORT = 0.3

#: Every phrase the recogniser may hear, and what each one means. Whole
#: phrases rather than parts assembled at runtime: the engine is handed
#: exactly these strings, and a list it can enumerate is what makes it work
#: offline.
PHRASES: dict[str, tuple[str, ...]] = {
    "start": (
        "scribe start recording",
        "scribe start the recording",
        "scribe record this",
        "scribe start",
    ),
    "stop": (
        "scribe stop recording",
        "scribe stop the recording",
        "scribe stop",
    ),
    "mark": (
        "scribe mark this",
        "scribe mark that",
        "scribe bookmark this",
    ),
}

#: Why something heard was not acted on. Three different problems with
#: three different answers - and being told the wrong one is worse than
#: being told nothing, because it sends you off correcting the wrong thing.
UNSURE = "unsure"
NO_TITLE = "no-title"
UNKNOWN = "unknown"

#: What the recogniser calls the prefix-then-dictation grammar. It reports
#: which grammar matched with every result, which is worth more than
#: inferring it back out of the words afterwards.
NAMING_GRAMMAR = "naming"

#: Naming is free text, so it is a fixed prefix followed by dictation. The
#: prefix has to be unambiguous: anything the engine hears after it becomes
#: the meeting's title.
NAMING_PREFIXES: tuple[str, ...] = (
    "scribe call this",
    "scribe name this",
)

#: Longest title a spoken name may produce, matching the speaker-name cap.
MAX_TITLE = 60

#: How long to wait for the recogniser to report that it has the microphone.
#: Loading the grammars and opening the device takes about a second.
START_TIMEOUT_S = 15.0

_LISTEN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine

$choices = New-Object System.Speech.Recognition.Choices
$choices.Add([string[]]($env:SCRIBE_VOICE_PHRASES -split '\|'))
$builder = New-Object System.Speech.Recognition.GrammarBuilder
$builder.Append($choices)
$commands = New-Object System.Speech.Recognition.Grammar($builder)
$commands.Name = 'command'
$engine.LoadGrammar($commands)

foreach ($prefix in ($env:SCRIBE_VOICE_PREFIXES -split '\|')) {
  if (-not $prefix) { continue }
  $naming = New-Object System.Speech.Recognition.GrammarBuilder
  $naming.Append($prefix)
  $naming.AppendDictation()
  $grammar = New-Object System.Speech.Recognition.Grammar($naming)
  $grammar.Name = 'naming'
  $engine.LoadGrammar($grammar)
}

if ($env:SCRIBE_VOICE_WAVE) {
  $engine.SetInputToWaveFile($env:SCRIBE_VOICE_WAVE)
} else {
  $engine.SetInputToDefaultAudioDevice()
}
[Console]::Out.WriteLine('{"ready":true}')
[Console]::Out.Flush()

while ($true) {
  try { $result = $engine.Recognize() } catch { break }
  if ($null -eq $result) { continue }
  $line = @{
    text = $result.Text
    confidence = [math]::Round($result.Confidence, 3)
    grammar = $result.Grammar.Name
  } | ConvertTo-Json -Compress
  [Console]::Out.WriteLine($line)
  [Console]::Out.Flush()
}
"""

_PROBE_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; "
    "[System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()"
    " | ForEach-Object { $_.Culture.Name }"
)

#: Speaks a phrase into a wave file, so the recogniser can be proved end to
#: end without anybody having to say anything. Windows' own voice is a
#: generous test subject, so this shows the chain works - not how well it
#: hears *you*.
_SYNTH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile($env:SCRIBE_VOICE_WAVE)
$synth.Speak($env:SCRIBE_VOICE_SAY)
$synth.Dispose()
"""


@dataclass(frozen=True)
class VoiceCommand:
    """One instruction heard out loud.

    Attributes:
        kind: ``"start"``, ``"stop"``, ``"mark"`` or ``"name"``.
        text: What was actually heard, for the log and for the page.
        confidence: What the recogniser thought of it, 0 to 1.
        title: For ``"name"``, the title that was dictated.
    """

    kind: str
    text: str
    confidence: float
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "title": self.title,
        }


def _clean_title(said: str) -> str:
    """Tidy dictated words into something worth calling a meeting."""
    title = " ".join(said.split()).strip(" .,;:!?-")
    if not title:
        return ""
    return (title[0].upper() + title[1:])[:MAX_TITLE]


def command_of(text: str, confidence: float = 1.0) -> VoiceCommand | None:
    """Turn a recognised phrase into a command, or ``None`` if it is neither.

    Naming is checked first: "scribe call this the finance sync" begins like
    nothing else, and a title is whatever follows.
    """
    said = " ".join(text.lower().split())
    if confidence < MIN_CONFIDENCE:
        return None
    for prefix in NAMING_PREFIXES:
        if said.startswith(prefix):
            title = _clean_title(text[len(prefix) :])
            return VoiceCommand("name", text, confidence, title) if title else None
    for kind, phrases in PHRASES.items():
        if said in phrases:
            return VoiceCommand(kind, text, confidence)
    return None


def unsure_reason(said: str, grammar: str = "") -> str:
    """Why *said* was heard and not acted on.

    Worth getting right rather than approximating, because each answer sends
    somebody somewhere different: repeat yourself, finish the sentence, or
    say something else entirely. The engine only ever returns phrases from
    the grammars it was handed - a fixed list, plus a prefix followed by
    dictation for naming - so "that is not one of the phrases" is close to
    unreachable in practice, and handing it out for the common cases would
    have been telling people to stop saying the thing that does work.
    """
    words = " ".join(said.lower().split())
    for prefix in NAMING_PREFIXES:
        if words.startswith(prefix):
            # "Scribe, call this" and then nothing is not a mishearing, and
            # saying it again more slowly will not help.
            return UNSURE if _clean_title(said[len(prefix):]) else NO_TITLE
    if any(words in phrases for phrases in PHRASES.values()):
        return UNSURE
    if grammar == NAMING_GRAMMAR:
        # Dictation matched but no prefix here did, so this list and the
        # grammar the recogniser was given have drifted apart.
        return NO_TITLE
    return UNKNOWN


def recogniser_executable() -> str:
    """The program the recogniser runs in, resolved the way Windows will.

    Scribe's call detection works by executable path, and listening means
    holding the microphone: without this, Scribe would notice its own
    recogniser and offer to record the call it thinks somebody is on.
    """
    return shutil.which("powershell.exe") or "powershell.exe"


def voice_available() -> tuple[bool, str]:
    """Whether this machine can listen, and what to say about it either way.

    Returns rather than raises, because "no speech recogniser here" is a
    normal answer the page has to render, not an error.
    """
    if sys.platform != "win32":
        return False, "Voice commands need Windows' own speech recogniser."
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _PROBE_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not ask Windows about speech recognition: {exc}"
    cultures = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not cultures:
        return False, (
            "Windows has no speech recogniser installed. Add one under "
            "Settings > Time & language > Speech."
        )
    return True, f"Windows speech recognition is available ({', '.join(cultures)})."


class VoiceListener:
    """Holds the microphone and reports the phrases it was told to listen for.

    The recogniser runs in a child process; this class starts it, reads a line
    of JSON per recognition, and hands whatever it understood to a callback.
    The callback runs on the reader thread, so anything that has to happen on
    an event loop must be posted to it.
    """

    def __init__(
        self,
        on_command: Callable[[VoiceCommand], None],
        *,
        on_unsure: Callable[[str, float, str], None] | None = None,
        spawn: Callable[[], Any] | None = None,
        min_confidence: float = MIN_CONFIDENCE,
        wave_file: str = "",
    ) -> None:
        """Create a listener.

        Args:
            on_command: Called with each understood command.
            on_unsure: Called with what was heard but not acted on - the
                text, the confidence, and one of :data:`UNSURE`,
                :data:`NO_TITLE` or :data:`UNKNOWN`. Without this a rejected
                command is indistinguishable from a dead microphone, which is
                the most confusing way for voice control to fail.
            spawn: Starts the recogniser process. Replaced in tests, which
                have no microphone and must not wait on one.
            min_confidence: Below this, a match is discarded as mishearing.
            wave_file: Listen to this recording instead of the microphone.
                Used by :func:`check_recogniser` to prove the chain works
                without anybody speaking.
        """
        self._on_command = on_command
        self._on_unsure = on_unsure
        self._wave_file = wave_file
        self._spawn = spawn or self._spawn_recogniser
        self._min_confidence = min_confidence
        self._process: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = False
        self._detail = "Not listening."

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Open the microphone and start listening. Returns whether it did."""
        if self._process is not None:
            return self.listening
        try:
            self._process = self._spawn()
        except OSError as exc:
            self._detail = f"Could not start the recogniser: {exc}"
            logger.warning(self._detail)
            self._process = None
            return False
        self._stopping = False
        self._thread = threading.Thread(
            target=self._read, name="voice-listener", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            # It may still come up, but saying "listening" before the
            # microphone is open would be a promise the page cannot keep.
            self._detail = "The recogniser did not report for duty."
            logger.warning(self._detail)
            return False
        self._detail = f'Listening for "{WAKE_WORD.title()}, start recording".'
        logger.info("Voice commands: %s", self._detail)
        return True

    def stop(self) -> None:
        """Let go of the microphone. Idempotent."""
        self._stopping = True
        process, self._process = self._process, None
        if process is None:
            return
        with _suppress_process_errors():
            process.terminate()
        with _suppress_process_errors():
            process.wait(timeout=5)
        self._ready.clear()
        self._detail = "Not listening."

    @property
    def listening(self) -> bool:
        return self._process is not None and self._ready.is_set()

    @property
    def detail(self) -> str:
        """One line for the page, whether or not it is working."""
        return self._detail

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the recogniser to run out of audio. Only a fixed recording
        ever does; a microphone does not end."""
        if self._thread is not None:
            self._thread.join(timeout)

    # -- internals ---------------------------------------------------------

    def _spawn_recogniser(self) -> Any:
        env = {
            **os.environ,
            "SCRIBE_VOICE_PHRASES": "|".join(
                phrase for phrases in PHRASES.values() for phrase in phrases
            ),
            "SCRIBE_VOICE_PREFIXES": "|".join(NAMING_PREFIXES),
            "SCRIBE_VOICE_WAVE": self._wave_file,
        }
        return subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _LISTEN_SCRIPT,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _report_unsure(self, said: str, sure: float, grammar: str = "") -> None:
        """Say what was heard and not acted on, and why.

        These failures look identical from across the room and to each other:
        nothing happens. They need opposite responses, so the reason goes out
        with the text rather than leaving somebody to guess.
        """
        if not said.strip() or sure < MIN_REPORT:
            return
        reason = unsure_reason(said, grammar)
        logger.info("Heard %r (%.2f) but did not act: %s", said, sure, reason)
        if self._on_unsure is None:
            return
        with contextlib.suppress(Exception):
            self._on_unsure(said, sure, reason)

    def _read(self) -> None:
        """Read recognitions until the process ends."""
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # The engine writes nothing but JSON; anything else is a
                # PowerShell warning, which is worth seeing but not obeying.
                logger.debug("Voice listener said: %s", line[:200])
                continue
            if record.get("ready"):
                self._ready.set()
                continue
            said = str(record.get("text", ""))
            sure = float(record.get("confidence", 0.0) or 0.0)
            command = command_of(said, sure)
            if command is None:
                self._report_unsure(said, sure, str(record.get("grammar", "")))
                continue
            logger.info(
                "Heard %r (%.2f) -> %s",
                record.get("text", ""),
                float(record.get("confidence", 0.0) or 0.0),
                command.kind,
            )
            try:
                self._on_command(command)
            except Exception:  # noqa: BLE001 - a bad handler must not deafen us
                logger.exception("Voice command handler failed")
        if not self._stopping:
            stderr = ""
            if process.stderr is not None:
                with _suppress_process_errors():
                    stderr = process.stderr.read() or ""
            self._ready.clear()
            self._detail = (
                f"The recogniser stopped: {stderr.strip()[:200]}"
                if stderr.strip()
                else "The recogniser stopped."
            )
            logger.warning("Voice commands: %s", self._detail)


class _suppress_process_errors:
    """Terminating a process that has already gone is not a failure."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> bool:
        return kind is not None and issubclass(
            kind, (OSError, ValueError, subprocess.TimeoutExpired)
        )


def check_recogniser(say: str = "scribe start recording") -> tuple[bool, str]:
    """Prove the chain - grammar, engine, parsing - without anybody speaking.

    Windows says the phrase into a wave file, and the recogniser is pointed at
    that instead of at the microphone. It shows the machinery works. How well
    it hears *you* is a different question that only your own voice answers.
    """
    ready, detail = voice_available()
    if not ready:
        return False, detail

    wave = Path(tempfile.gettempdir()) / "scribe-voice-check.wav"
    env = {**os.environ, "SCRIBE_VOICE_WAVE": str(wave), "SCRIBE_VOICE_SAY": say}
    try:
        spoken = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _SYNTH_SCRIPT,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not speak a test phrase: {exc}"
    if spoken.returncode != 0 or not wave.exists():
        problem = (spoken.stderr or spoken.stdout).strip()[:200]
        return False, f"Could not speak a test phrase: {problem}"

    heard: list[VoiceCommand] = []
    listener = VoiceListener(heard.append, wave_file=str(wave))
    try:
        if not listener.start():
            return False, listener.detail
        listener.wait(60)
    finally:
        listener.stop()
        with contextlib.suppress(OSError):
            wave.unlink()

    if not heard:
        return False, f"Windows did not recognise its own voice saying {say!r}."
    first = heard[0]
    return True, (
        f"Heard {first.text!r} at {first.confidence:.2f} confidence - "
        "voice commands are working."
    )


__all__ = [
    "MAX_TITLE",
    "MIN_CONFIDENCE",
    "NO_TITLE",
    "UNKNOWN",
    "UNSURE",
    "unsure_reason",
    "NAMING_PREFIXES",
    "PHRASES",
    "WAKE_WORD",
    "VoiceCommand",
    "VoiceListener",
    "check_recogniser",
    "command_of",
    "recogniser_executable",
    "voice_available",
]
