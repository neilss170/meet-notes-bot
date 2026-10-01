# How Scribe works

The pipeline, and where each part of it lives in the tree.

[&larr; back to the README](../README.md)

---

```
  ┌──────────────────────────── one process ─────────────────────────────┐
  │                                                                       │
  │  cli.py ──▶ runner.py ──┬──▶ join.py ──▶ Playwright ──▶ Chromium      │
  │                         │                                  │          │
  │                         │            capture/inject.js ◀───┘          │
  │                         │            (wraps RTCPeerConnection,        │
  │                         │             taps remote audio tracks)       │
  │                         │                       │ PCM over ws://      │
  │                         │                       ▼                     │
  │                         ├──▶ capture/bridge.py ──▶ capture/deepgram.py│
  │                         │            │                   │            │
  │                         │            │              Deepgram live API │
  │                         │            ▼                                │
  │                         │    transcript/store.py  ──▶ transcript.jsonl│
  │                         │                                             │
  │                         └──▶ analysis/summarize.py ──▶ analysis.md    │
  │                                     │                                 │
  │                                Claude / GPT                           │
  └───────────────────────────────────────────────────────────────────────┘
```

The trick that makes this work without a virtual audio device: `inject.js` is
added with Playwright's `add_init_script`, so it wraps `RTCPeerConnection`
*before* Meet's own JavaScript runs. Every remote audio track Meet negotiates
passes through our `track` listener, gets routed into a Web Audio graph,
resampled to 16 kHz, converted to 16-bit PCM and pushed over a loopback
WebSocket to the Python bridge.

### Layout

```
meetbot/
  cli.py              # argparse entrypoint: run / serve / login / users / analyze / format / check
  runner.py           # end-to-end orchestration and failure containment
  join.py             # Playwright join flow and meeting lifecycle
  selectors.py        # ALL Google Meet DOM selectors live here
  config.py           # env/.env loading, validation, disclosure enforcement
  captions.py         # speaker names from Meet's caption panel
  preflight.py        # what must be true before a meeting, shared by CLI + service
  profile.py          # private per-run copies of the signed-in Chromium profile
  logging_setup.py    # console + per-run file logging
  service/
    app.py            # FastAPI endpoints + the web UI
    auth.py           # accounts, scrypt passwords, signed session cookies
    jobs.py           # concurrent meeting jobs: start, follow, stop
    ui.html           # the single-page control surface
    login.html        # the sign-in page
  capture/
    inject.js         # browser-side WebRTC audio hook
    bridge.py         # loopback WebSocket server, channel routing, timeline
    deepgram.py       # Deepgram live streaming client (raw WebSocket)
  transcript/
    store.py          # crash-durable JSONL read/write
    notes.py          # the notepad on disk: notes, enhanced notes, chat
    format.py         # Markdown / plain-text / LLM-prompt rendering
  analysis/
    llm.py            # Claude and GPT adapters behind one JSON interface
    summarize.py      # prompts, map-reduce, parsing, Markdown rendering
    notepad.py        # note enhancement, meeting templates, ask-a-question
tests/
```

---
