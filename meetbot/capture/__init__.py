"""Browser-side audio capture and the bridge to Deepgram."""

from __future__ import annotations

from pathlib import Path

from meetbot.capture.bridge import MIXED_CHANNEL, AudioBridge, BridgeStats
from meetbot.capture.deepgram import (
    DeepgramError,
    DeepgramLiveClient,
    TranscriptSegment,
    build_stream_url,
    parse_results_message,
)

#: Absolute path to the browser-side hook injected into the Meet page.
INJECT_SCRIPT_PATH = Path(__file__).parent / "inject.js"


def load_inject_script() -> str:
    """Read the browser-side capture hook from disk.

    Raises:
        FileNotFoundError: If the packaged ``inject.js`` is missing, which
            usually means the package was installed without its data files.
    """
    if not INJECT_SCRIPT_PATH.exists():
        raise FileNotFoundError(
            f"Browser hook not found at {INJECT_SCRIPT_PATH}. "
            "Reinstall the package (inject.js must ship with meetbot.capture)."
        )
    return INJECT_SCRIPT_PATH.read_text(encoding="utf-8")


__all__ = [
    "AudioBridge",
    "BridgeStats",
    "DeepgramError",
    "DeepgramLiveClient",
    "INJECT_SCRIPT_PATH",
    "MIXED_CHANNEL",
    "TranscriptSegment",
    "build_stream_url",
    "load_inject_script",
    "parse_results_message",
]
