"""meetbot - a Google Meet transcription and meeting-intelligence bot.

The bot joins a Google Meet call as a *guest* (no Google account login),
captures remote participant audio via a browser-side WebRTC hook, streams it
to Deepgram for live speaker-labeled transcription, and produces an
LLM-generated post-meeting analysis.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
