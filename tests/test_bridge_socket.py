"""End-to-end test of the bridge's WebSocket server against a real client.

Unlike ``test_bridge.py``, which calls the frame handlers directly, this binds
a real loopback socket and speaks the same wire protocol ``inject.js`` does.
It is the check that our use of the ``websockets`` API still matches the
installed version - the handler signature and the text/binary split are the
two things that have moved between releases.

Only the Deepgram client is stubbed. No external network is used.
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest
import websockets

from meetbot.capture import bridge as bridge_module
from meetbot.capture.bridge import MIXED_CHANNEL, AudioBridge
from meetbot.config import Config
from meetbot.transcript.store import TranscriptStore

from tests.test_bridge import StubDeepgramClient, pcm_frame


@pytest.fixture
def stub_deepgram(monkeypatch):
    StubDeepgramClient.instances = []
    monkeypatch.setattr(bridge_module, "DeepgramLiveClient", StubDeepgramClient)
    return StubDeepgramClient


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    """Poll ``predicate`` until true, to avoid sleeping a fixed duration."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met within the timeout")


async def test_browser_protocol_over_a_real_socket(
    tmp_path: Path, stub_deepgram
) -> None:
    config = Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        # Port 0 asks the OS for a free port, so parallel runs never collide.
        bridge_port=0,
    )
    store = TranscriptStore(tmp_path / "transcript.jsonl")
    bridge = AudioBridge(config, store)
    await bridge.start()

    # Resolve the port the OS actually assigned.
    port = next(iter(bridge._server.sockets)).getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            await socket.send(
                json.dumps(
                    {"type": "hello", "sampleRate": 16000, "perParticipant": False}
                )
            )
            await socket.send(
                json.dumps({"type": "channel", "id": 0, "label": "mixed"})
            )
            await socket.send(pcm_frame(MIXED_CHANNEL, [100, -100, 200, -200]))
            await socket.send(pcm_frame(MIXED_CHANNEL, [1, 2]))

            await _wait_for(lambda: bridge.stats.frames >= 2)

        assert bridge.stats.browser_connections == 1
        assert bridge.received_any_audio
        assert len(stub_deepgram.instances) == 1
        assert stub_deepgram.instances[0].audio[0] == struct.pack(
            "<4h", 100, -100, 200, -200
        )
    finally:
        await bridge.stop()
        store.close()


async def test_bridge_survives_an_abrupt_client_disconnect(
    tmp_path: Path, stub_deepgram
) -> None:
    """A crashed tab must not take the bridge down with it."""
    config = Config(
        meet_url="https://meet.google.com/abc-defg-hij",
        deepgram_api_key="dg",
        anthropic_api_key="sk",
        bridge_port=0,
    )
    store = TranscriptStore(tmp_path / "transcript.jsonl")
    bridge = AudioBridge(config, store)
    await bridge.start()
    port = next(iter(bridge._server.sockets)).getsockname()[1]

    try:
        socket = await websockets.connect(f"ws://127.0.0.1:{port}")
        await socket.send(pcm_frame(MIXED_CHANNEL, [5, 5]))
        await _wait_for(lambda: bridge.stats.frames >= 1)
        # Kill the transport rather than closing politely - that is what a
        # crashed or navigated-away tab looks like to the server.
        transport = getattr(socket, "transport", None)
        if transport is not None:
            transport.abort()
        else:  # pragma: no cover - older websockets releases
            await socket.close()
        await _wait_for(lambda: bridge.stats.browser_connections == 1)

        # The server still accepts a fresh connection, as it would after a
        # page reload.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as second:
            await second.send(pcm_frame(MIXED_CHANNEL, [6, 6]))
            await _wait_for(lambda: bridge.stats.frames >= 2)

        assert bridge.stats.browser_connections == 2
    finally:
        await bridge.stop()
        store.close()
