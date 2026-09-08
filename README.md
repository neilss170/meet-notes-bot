# Scribe — Google Meet transcription & meeting intelligence

[![tests](https://github.com/neilss170/meet-notes-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/neilss170/meet-notes-bot/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A bot that joins a Google Meet call, captures what the other participants say,
produces a speaker-labelled transcript as the meeting happens, and writes up
the summary, decisions, action items and tone once it ends.

It runs entirely on your own machine. Audio goes to Deepgram for transcription
and the finished transcript goes to one LLM call for the summary; nothing else
leaves the host, and no OS-level virtual audio device is involved.

**It needs a Google account of its own.** Meet increasingly refuses anonymous
guests outright, so the bot signs in once (`meetbot login`) and reuses that
browser profile. Treat that profile like a password — see
[Secrets](#secrets).

> The package, module and CLI are still named `meetbot`; only the product name
> is Scribe. Renaming the module touches every import and is a separate job.

---

## Quickstart

```bash
# 1. Get the code
git clone https://github.com/neilss170/meet-notes-bot.git
cd meet-notes-bot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install
pip install -r requirements.txt
pip install -e ".[serve]"
playwright install chromium

# 4. Configure
cp .env.example .env             # then add your Deepgram and LLM keys

# 5. Sign the bot into Google (once)
python -m meetbot login

# 6. Confirm everything works before you need it to
python -m meetbot check

# 7. Start the web UI
python -m meetbot serve --open
```

Step 6 is the one worth keeping. It opens a real Deepgram stream with *your*
configured options, sends one trivial LLM completion, and checks the browser
profile is still signed in — then prints `Ready to record.` or tells you
exactly which command fixes what is broken.

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
    format.py         # Markdown / plain-text / LLM-prompt rendering
  analysis/
    llm.py            # Claude and GPT adapters behind one JSON interface
    summarize.py      # prompts, map-reduce, parsing, Markdown rendering
tests/
```

---

## Setup

Requires **Python 3.11+**. The commands are in [Quickstart](#quickstart) above.

You need a [Deepgram](https://console.deepgram.com/) API key for
transcription, plus an Anthropic **or** OpenAI key for the analysis step
(`--no-analysis` skips it entirely and needs neither). Deepgram's free tier
covers roughly 400 hours of meetings, and Groq's is free outright, so a pilot
costs nothing.

Two settings are worth getting right before your first real meeting, because
both meaningfully change how accurate the transcript is:

| Setting | Why |
|---|---|
| `DEEPGRAM_LANGUAGE` | Use the variety actually being spoken — `en-IN`, `en-GB`, `en-US`. Transcribing Indian English as American English garbles it badly. |
| `DEEPGRAM_KEYTERMS` | Comma-separated names and jargon. ASR fails hardest on exactly the words a summary needs most — people's names, product names — because they are rare in the language model. Put your team's names here. |

Verify the install without joining anything:

```bash
python -m meetbot check
```

`check` asks every question a live meeting would answer the hard way:

| Check | Why it is worth the seconds |
|---|---|
| **Configuration** | Internally coherent — sample rate, provider, ports. |
| **Deepgram** | Opens a stream with **your** configured options, not defaults. A generic probe once passed while every real stream was rejected, because the failure lived entirely in options it never sent. |
| **AI notes** | One trivial completion. The summary runs *after* the meeting, so a dead key would otherwise surface when the material is already gone. |
| **Google sign-in** | Whether the profile is still signed in. Google expires these on its own schedule, and an expired one is invisible until Meet turns the bot away with people already waiting. |
| **Playwright** | Chromium is installed and launches. |

Each failure prints the command that fixes it, and `check` exits non-zero:

```
  OK    Configuration    valid
  OK    Deepgram         accepts nova-3/en-IN, 11 key term(s)
  OK    AI notes         key accepted, openai/gpt-oss-120b replied 'OK'
  FAIL  Google sign-in   The profile is signed out; Meet will refuse the bot
To fix Google sign-in: python -m meetbot login

Not ready - fix the items above, then run check again.
```

The same checks run when the service starts and are shown in the web UI,
which refuses to send the bot while something is broken. One module
(`meetbot/preflight.py`) backs all three, so "check says it is fine" and "the
bot will actually work" cannot drift apart.

Pass `--offline` to skip everything that touches the network.

---

## Running it

```bash
python -m meetbot run --url https://meet.google.com/abc-defg-hij
```

The bot joins, waits to be admitted if the meeting requires approval, and
records until the meeting ends, it is removed, it is left alone for
`ALONE_TIMEOUT_S`, or `MAX_MEETING_DURATION_S` is hit. `Ctrl+C` shuts it down
cleanly — the transcript is flushed and the analysis still runs.

### Signing the bot in (usually required)

By default the bot joins as an anonymous guest. **Meet often refuses anonymous
guests outright**: instead of the pre-join screen it shows

> You can't join this video call
> No one can join a meeting unless invited or admitted by the host

and the host is never asked to admit anything — no knock, no prompt, nothing to
approve. That page has no join button, which looks like a broken selector but
is not: no selector can fix a refusal. Observed live on 2026-09-07 against
meetings that the same code had joined successfully three days earlier, so
treat anonymous joining as unreliable rather than broken-once.

Sign in once:

```bash
python -m meetbot login
```

A real browser window opens. Sign in to the Google account the bot should join
as, and leave the window alone — it detects the session and closes itself. Then
put the printed path in `.env`:

```
CHROME_PROFILE_DIR=/home/you/.meetbot/chrome-profile
```

Every run after that reuses the session, and the bot appears as a named
participant rather than an anonymous guest.

**The sign-in is manual on purpose.** Google actively blocks scripted logins,
and a stored password would be both fragile and a far worse thing to keep on
disk than a session cookie.

**That directory is a credential.** It holds live Google session cookies —
anyone who copies it is signed in as that account. It is gitignored, and it
should not go on a shared machine. Use a dedicated account for the bot rather
than a personal one, and one with access only to what it needs.

### Without a terminal: the web UI

Running a command per meeting does not scale past demoing it yourself. `serve`
starts a small local service instead — paste a link, click a button:

```bash
python -m meetbot serve --open
```

That opens `http://127.0.0.1:8080`, where you can send the bot into a call,
watch the transcript build up live, stop it early, and read the notes from any
past meeting. The bot still runs on this machine; the UI is just the control
surface.

#### Accounts

Every route requires a signed-in account. On an empty database the service
generates a first admin and prints the password **once**, in the startup log:

```
No accounts existed, so an admin was created:
    username: admin
    password: UKe_H9NryCfZb3cm
Sign in and change it. This is shown only once.
```

Two roles. A **member** sends the bot and sees only the meetings they started;
an **admin** sees every meeting and manages accounts from a panel in the UI.
Meetings recorded before accounts existed have no owner and are visible to
admins only — guessing who they belonged to would be worse than showing
nobody.

Passwords are hashed with scrypt. Sessions are HMAC-signed cookies, so
restarting the service does not sign everyone out, and accounts are re-read
per request, so deleting or demoting someone takes effect immediately rather
than whenever their cookie lapses.

If the only admin password is ever lost, the UI cannot help — `meetbot users`
can:

```bash
python -m meetbot users list
python -m meetbot users add priya --role member
python -m meetbot users passwd admin
python -m meetbot users delete priya
```

It binds to **loopback only** by default. `--host` serves it to the network
and warns when you use it: accounts are required, but sessions travel as
cookies, so put TLS in front of it or passwords cross the wire in clear.

Things it handles that the one-shot CLI never had to:

- **Several meetings at once.** Each job gets its own audio-bridge port; a
  fixed one fails on the second simultaneous meeting.
- **Stopping a specific bot** from the UI, rather than Ctrl+C stopping whatever
  the process happens to be doing.
- **Surviving a restart.** Past runs are read back from `recordings/`, so
  restarting the server does not appear to erase history.
- **One bad meeting not killing the server.** A crashed run marks its own job
  failed and leaves the others alone.

| Endpoint | |
|---|---|
| `GET /api/meetings` | List every meeting, live and past |
| `POST /api/meetings` | `{"meet_url": "..."}` — send the bot |
| `GET /api/meetings/{id}` | Status plus live transcript |
| `POST /api/meetings/{id}/stop` | Leave the call and write the summary |
| `GET /api/meetings/{id}/artifact/{name}` | `transcript.md`, `analysis.md`, … |
| `GET /api/health` | What is working, and the command that fixes what is not |
| `POST /api/health/refresh` | Re-run the checks after fixing something |
| `GET /api/users` | Admin only: list accounts |
| `POST /login` / `POST /logout` | Session in, session out |

All of them require a session; `POST /api/meetings` also returns **409** when
the checks say the bot could not record the meeting anyway.

### Output

Each run creates `recordings/<UTC timestamp>-<meeting code>/`:

| File | What it is |
|---|---|
| `transcript.jsonl` | Source of truth. One JSON record per line, flushed as the meeting happens. |
| `transcript.md` | Readable transcript, grouped into speaker turns. |
| `analysis.md` | Summary, key points, **decisions**, action items, sentiment. |
| `meetbot.log` | Full DEBUG log for the run, including browser-side messages. |

`analysis.md` looks like this:

```markdown
## Summary
Neil opened the meeting to review the pipeline, budget and timeline. Priya
reported the pipeline is running end to end...

## Key Discussion Points
- Pipeline is finished and running end-to-end
- Cost is roughly 1200 rupees a month

## Decisions
- The pilot will run for three weeks, starting Monday

## Action Items
- [ ] **Priya Menon** - Send the cost breakdown _(due: Friday)_

## Sentiment
**Overall tone:** positive
```

**Decisions and action items are kept apart on purpose.** A decision is what
the meeting settled; an action item is what somebody will now go and do. The
model will happily list the same thing as both if you let it, which pads the
notes and makes a meeting look more conclusive than it was — so the prompt
forbids it, and an empty section says *"Nothing was decided in this meeting"*
rather than inventing something.

The same restraint applies throughout: the prompt is explicit that it must not
invent owners, dates or numbers, and that an unassigned task stays unassigned.
A note-taker that fabricates commitments is worse than none, because you stop
being able to trust the parts it got right.

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
| `--no-speaker-names` | Don't turn on Meet's captions; leave speakers as `Speaker 0` / `Speaker 1`. |
| `--anonymise-analysis` | Send `Speaker A` / `Speaker B` to the LLM instead of real names. The local transcript keeps them. |
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

**On speaker names and captions specifically:**

- Turning Meet's live captions on is a **per-viewer setting**. It changes what
  the bot's own browser renders; it does not enable captions, transcription or
  recording for anybody else, and other participants see no change.
- Naming speakers makes the transcript **personal data** in a way that
  `Speaker 0` is not. That raises the stakes of where the notes are stored and
  who can read them — say so when you announce the bot.
- The transcript is sent to a third-party LLM to be summarised. Use
  `--anonymise-analysis` to replace real names with `Speaker A` / `Speaker B`
  in that request; the local transcript keeps the real names, so you lose
  nothing locally. Consider it the default for anything sensitive.
- `--no-speaker-names` disables caption reading entirely.

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

### Speaker names come from Meet's captions

Deepgram's diarization gives `Speaker 0`, `Speaker 1`, … — not names. Nothing
in the page links a WebRTC audio track to a participant: Meet attaches remote
`<audio>` elements directly to `<body>`, with no path to the tile showing whose
audio it is. Roughly 135 lines of DOM heuristics were written to try, tested
against a live call, and deleted — they returned things like "Sign in" and
"11:32".

Meet's **caption panel** is a different surface. Google does speaker
attribution server-side and renders the name next to each caption line, so the
names the audio path cannot see are already in the DOM. That is where
`--speaker-names` (on by default) gets them:

1. The bot turns Meet's live captions on **for its own browser only**.
2. It polls the caption panel and records *who was speaking, and when*.
3. Each Deepgram utterance is labelled with whoever the captions say was
   talking across its time window.

Only the name and the timing are used. **The caption text is discarded** — Meet
rewrites a caption in place as its recogniser refines it, so reconstructing a
transcript from that stream means fighting duplicates and truncations, and the
result is still less accurate than Deepgram's. The words stay Deepgram's; only
the name comes from captions.

That in-place rewriting is also what makes the attribution work: an entry whose
text *changed* between polls belongs to whoever is speaking now, while an
unchanged entry is just history still on screen. Without that distinction a
speaker who finished ten seconds ago would keep stealing the current utterance.

One match names the speaker's whole meeting. Deepgram's diarization ids are
stable within a stream, so when captions do name an utterance, the id that
produced it is remembered and every other utterance carrying that id - before
and after - takes the same name. Utterances with no caption overlap of their
own (a short reply, speech between polls) are named anyway.

A name has to win a majority and be seen at least twice before it is trusted:
diarization drifts and captions can land on the wrong side of a boundary, and
a wrong name on *every* one of a speaker's utterances is far worse than the
generic label it replaced.

The transcript log is append-only for crash durability, so it is never
rewritten. The mapping is recorded as an event and applied when the transcript
is read back, which also means `python -m meetbot analyze` on an old
transcript sees the names.

When captions are unavailable, or a speaker is never identified, the label
falls back to `Speaker 0`. **A generic label is the correct answer when
we do not know** — a wrong name is worse than no name, so attribution declines
rather than guesses.

Turn it off with `--no-speaker-names`.

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
