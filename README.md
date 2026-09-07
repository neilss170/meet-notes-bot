# meetbot — Google Meet transcription & meeting intelligence

An in-house bot that joins a Google Meet call **as a guest**, captures the
remote participants' audio, produces a live speaker-labeled transcript, and
generates a post-meeting summary with key points, action items and sentiment.

No Google account. No stored credentials. No OS-level virtual audio device.

---

## How it works

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
  cli.py              # argparse entrypoint: run / analyze / format / check
  runner.py           # end-to-end orchestration and failure containment
  join.py             # Playwright guest-join flow and meeting lifecycle
  selectors.py        # ALL Google Meet DOM selectors live here
  config.py           # env/.env loading, validation, disclosure enforcement
  logging_setup.py    # console + per-run file logging
  capture/
    inject.js         # browser-side WebRTC audio hook
    bridge.py         # loopback WebSocket server, channel routing, timeline
    deepgram.py       # Deepgram live streaming client (raw WebSocket)
  transcript/
    store.py          # crash-durable JSONL read/write
    format.py         # Markdown / plain-text / LLM-prompt rendering
  analysis/
    llm.py            # Claude and GPT adapters behind one JSON interface
    summarize.py      # prompts, map-reduce, parsing, Markdown rendering
tests/
```

---

## Setup

Requires **Python 3.11+**.

```bash
git clone <this repo> && cd meet-notes-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium                          # one-time browser download

cp .env.example .env                                 # then fill in your keys
```

You need a [Deepgram](https://console.deepgram.com/) API key for
transcription, plus an Anthropic **or** OpenAI key for the analysis step
(`--no-analysis` skips it entirely and needs neither).

Verify the install without joining anything:

```bash
python -m meetbot check
```

`check` makes two real (brief) API calls so credentials fail here rather than
on a live call:

- **Deepgram** — opens a streaming connection to confirm the key, model and
  language are accepted.
- **LLM** — sends one trivial completion to confirm the key, model id and
  endpoint work. This matters because the summary is generated *after* the
  meeting ends, so a dead key would otherwise only surface once the call is
  over and the material is gone.

Pass `--offline` to skip both and make no network calls.

---

## Running it

```bash
python -m meetbot run --url https://meet.google.com/abc-defg-hij
```

The bot joins, waits to be admitted if the meeting requires approval, and
records until the meeting ends, it is removed, it is left alone for
`ALONE_TIMEOUT_S`, or `MAX_MEETING_DURATION_S` is hit. `Ctrl+C` shuts it down
cleanly — the transcript is flushed and the analysis still runs.

### Output

Each run creates `recordings/<UTC timestamp>-<meeting code>/`:

| File | What it is |
|---|---|
| `transcript.jsonl` | Source of truth. One JSON record per line, flushed as the meeting happens. |
| `transcript.md` | Readable transcript, grouped into speaker turns. |
| `analysis.md` | Summary, key points, action items, sentiment. |
| `meetbot.log` | Full DEBUG log for the run, including browser-side messages. |

### Other subcommands

```bash
# Re-run the analysis over a transcript from an earlier run.
# This is the recovery path when the live run captured audio but the LLM
# call failed (bad key, rate limit, outage).
python -m meetbot analyze --transcript recordings/20260904-101500-abc-defg-hij/transcript.jsonl

# Re-render a stored transcript.
python -m meetbot format --transcript <path>/transcript.jsonl --format text

# Validate config + Playwright without joining a call.
python -m meetbot check
```

### Useful flags

| Flag | Why you'd use it |
|---|---|
| `--no-headless` | **The** debugging tool. Watch the join flow and inspect the DOM when selectors break. |
| `--per-participant` | One Deepgram stream per participant instead of diarizing a mixed stream — much better speaker separation, one connection per person. |
| `--live-captions-overlay` | Show the running transcript **inside the call**, as a screen-share tile everyone can see. See below. |
| `--no-analysis` | Transcript only; no LLM key needed. |
| `--llm-provider openai` | Switch the analysis to GPT. |
| `--admission-timeout 600` | Wait longer in the lobby. |
| `--log-level DEBUG` | Verbose console output. It is a **global** flag: it goes before the subcommand, not after. |

Everything is also settable via `.env` — see `.env.example` for the full list.

---

### Live captions in the call

`--live-captions-overlay` makes the transcript visible to every participant,
without ever turning the bot's camera on:

1. The hook paints recent utterances onto an offscreen 1280×720 canvas.
2. `navigator.mediaDevices.getDisplayMedia` is patched to return that canvas
   as a video stream, so the browser's own screen-picker never appears.
3. The bot clicks Meet's share control once, and Meet presents the canvas as
   an ordinary screen-share tile.

Two details are load-bearing:

- **The canvas is repainted on a timer, not only when someone speaks.** A
  canvas capture stream emits a frame only when the canvas is actually
  painted — it does not resample a static one. Painting only on new speech
  made the stream stall at a single frame, so the shared tile stayed black.
  `tests/test_overlay_browser.py` covers this against a real Chromium.
- **Success is measured by the page hook recording Meet's `getDisplayMedia`
  call**, not by a click appearing to work. A wrong selector is reported as an
  error, with every real label on the page logged, rather than silently
  leaving the captions invisible for the whole meeting.

---

## Consent and disclosure — read this before using it

**Recording a meeting is a legal act, not just a technical one.** This tool is
built so that it cannot run invisibly, and you should not try to make it.

- **Most jurisdictions require that participants be told a meeting is being
  recorded.** Some — including "all-party consent" US states such as
  California, Florida, Illinois, Pennsylvania and Washington, and much of the
  EU under GDPR — require the **consent of everyone on the call**, not just
  the organiser. Recording without it can be a criminal offence, not merely a
  policy violation.
- **The bot's display name must announce what it is.** `config.py` enforces
  this: any configured name that does not contain a disclosure word
  (`record`, `transcri`, `notetaker`, …) gets `(Recording)` appended
  automatically, and a warning is logged. Do not remove this. It is the only
  signal other participants get before transcription starts.
- **The bot appears in the participant list** like any other guest, and in
  approval-gated meetings a human must actively admit it.
- **Google Meet shows its own on-screen indicator** when recording or
  transcription is active, and announces it to participants. This bot's
  capture happens client-side, so Meet's *own* indicator may not fire for it —
  which makes the display name and your verbal disclosure more important, not
  less.

**What we recommend operationally:**

1. Announce the bot verbally at the start of the meeting, and say where the
   notes will be stored and who can read them.
2. Give anyone who objects a real way to opt out — pause the bot, or don't run
   it.
3. Do not run this on meetings involving people outside the company, or on
   HR, legal, medical, or other sensitive conversations, without explicit
   sign-off.
4. Treat `recordings/` as sensitive data: it contains verbatim speech.
   Apply your normal retention and access controls, and delete on a schedule.

Confirm your obligations with whoever owns legal/compliance at your company
before this touches a real meeting. Nothing here is legal advice.

---

## Known limitations

Be realistic about what this does and does not do.

### Meet DOM selectors are fragile

Google changes the Meet UI with no notice, no versioning, and obfuscated class
names. **The join flow will break periodically.** This is inherent to browser
automation against a product with no API for this.

Mitigations built in: every selector lives in `meetbot/selectors.py`, each one
is a *list of candidates* tried in order (accessibility attributes first, text
second, structural last), and failures log the stage that broke. When it
breaks:

```bash
python -m meetbot --log-level DEBUG run --url <url> --no-headless
```

watch where it stalls, inspect the element, and add a new candidate at the
**front** of the relevant list in `selectors.py`. Leave the old ones — Google
rolls changes out gradually and several variants are often live at once.

### Speaker labels are generic by default

In the default mixed-stream mode, Deepgram's diarization gives you
`Speaker 0`, `Speaker 1`, … — not names. Diarization also drifts: the same
person can get a new id after a long silence, and overlapping speech gets
attributed unpredictably.

`--per-participant` gives each remote audio track its own Deepgram stream
(diarization off), which is far more reliable for *separation*. But the
channel labels are still `participant-1`, `participant-2` — **mapping a WebRTC
track back to a Meet display name is not implemented**, because Meet does not
expose a stable track↔participant mapping to page scripts. `bridge.py` and
`inject.js` both support relabelling a channel (`set_channel_label` /
`window.__meetbot.labelChannel`), so the hook is there if someone wants to
attempt roster correlation later.

In practice: the LLM analysis often infers real names from the conversation
itself ("Thanks, Priya"), which is why the participant list is passed as
prompt context.

### Headless mode caveats

Headless Chromium is the default and works, but:

- WebRTC and Web Audio behave differently headless than headed. If audio
  capture works headed and not headless, that is a real class of bug — check
  the `meetbot.browser` log lines for `worklet tap installed`.
- Meet occasionally serves a different UI to headless browsers, so a selector
  can work headed and fail headless.
- `AudioWorklet` is loaded from a `blob:` URL. If Meet's CSP blocks it, the
  code falls back to the deprecated `ScriptProcessorNode` — functional, higher
  latency (~256 ms buffers). The log says which path was taken.
- Chromium is launched with `--use-fake-device-for-media-stream` so
  `getUserMedia` resolves without hardware. The bot's mic and camera are muted
  before joining regardless, via both the UI toggles and Meet's keyboard
  shortcuts.

### Everything else

- **Single meeting per process.** No orchestration, no scaling. Run one
  process per meeting.
- **Google Meet only.** No Zoom, no Teams.
- **Guest join only.** Meetings that block guests entirely, or that require a
  Google Workspace account, cannot be joined.
- **The bot cannot consent on anyone's behalf** and cannot detect that a
  participant objected. That is a human process.
- **Transcription quality** depends on Deepgram, audio quality, accents,
  crosstalk and jargon. Names and acronyms are the usual casualties. The
  analysis prompt tells the model the transcript is ASR output and may contain
  errors, but it cannot recover what was never transcribed.
- **The analysis is LLM output.** It can be wrong. Verify anything you plan to
  act on, especially action-item owners.
- **Deepgram reconnects reset the stream clock.** `bridge.py` rebases
  timestamps onto the meeting timeline, but audio recorded during a long
  outage is buffered and may be dropped (oldest first) rather than silently
  delaying the whole transcript. Drops are logged.
- **No audio is written to disk.** Only text. That is deliberate — it limits
  the blast radius of a leak — but it means you cannot re-transcribe a meeting
  with a better model later.

---

## Failure behaviour

Deliberate design choices worth knowing:

- **The transcript is written first and closed before analysis runs.** A
  failing LLM call, a missing key or a network outage costs you the summary
  and nothing else — and `meetbot analyze` re-runs it later.
- **Every utterance is flushed to disk immediately.** A hard kill mid-meeting
  loses at most the line being written; the reader tolerates a truncated final
  line.
- **Deepgram disconnects reconnect** with exponential backoff. Authentication
  failures are fatal — retrying a bad key forever helps nobody.
- **A failed chunk in the map-reduce analysis** is recorded in the notes and
  the analysis continues rather than aborting.
- **No bare `except: pass`.** Every swallowed exception is logged with context.

---

## Development

```bash
pip install -r requirements.txt
python -m pytest              # 156 tests, no network, no browser
python -m pytest -q --tb=short
```

The test suite covers the transcript store (durability, corrupt lines, schema
evolution), the formatters, Deepgram result parsing, the bridge's frame
decoding and timeline rebasing, config validation and disclosure enforcement,
the analysis module (with mocked LLM clients), and the runner's failure
containment. Deepgram and both LLM SDKs are mocked throughout.

**Integration against a real Meet call is manual and out of scope for the
automated tests.** To do it: start a Meet call, then

```bash
python -m meetbot --log-level DEBUG run --url <url> --no-headless
```

and check that `remote track mixed` / `worklet tap installed` appear in the
log within a few seconds of someone speaking.

### A note on the Deepgram client

We talk to `wss://api.deepgram.com/v1/listen` directly rather than through
`deepgram-sdk`. The SDK's client surface has changed shape across major
versions; the streaming WebSocket protocol has not. Depending on the protocol
means a `pip install` a year from now does not silently break the audio path.
The cost is that keepalive, reconnect and result parsing are ours to maintain —
that is `capture/deepgram.py`, and it is unit-tested.

---

## Secrets

Keys are read from the environment (optionally seeded from `.env`) and never
written to disk by this package. `.env` is gitignored; `.env.example` is the
committed template. `Config.redacted()` is what gets logged — it replaces
every `*_api_key` with a length marker.
