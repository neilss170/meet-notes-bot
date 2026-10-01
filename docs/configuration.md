# Configuration

Everything that can go in `.env`. Only the first two keys are required —
every other setting here has a working default, which is why `.env.example`
is short.

[&larr; back to the README](../README.md)

---

## What is actually required

| Setting | Get it from |
|---|---|
| `DEEPGRAM_API_KEY` | <https://console.deepgram.com/> → API Keys |
| `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys> |

`python -m meetbot setup` creates `.env` and tells you which of these is
still blank. The LLM key is only for the write-up: with
`ANALYSIS_ENABLED=false` (or `--no-analysis`) you need neither it nor a
provider at all, and you still get transcripts.

Deepgram's free tier covers roughly 400 hours of meetings, and Groq's is free
outright, so a pilot costs nothing.

## Two settings worth getting right before your first real meeting

Both change how accurate the transcript is, more than anything else here.

| Setting | Why |
|---|---|
| `DEEPGRAM_LANGUAGE` | Use the variety actually being spoken — `en-IN`, `en-GB`, `en-US`. Transcribing Indian English as American English garbles it badly. |
| `DEEPGRAM_KEYTERMS` | Comma-separated names and jargon. ASR fails hardest on exactly the words a summary needs most — people's names, product names — because they are rare in the language model. Put your team's names here. It is the cheapest accuracy win available. |

---

## The full reference

### Transcription

| Setting | Default | What it does |
|---|---|---|
| `DEEPGRAM_API_KEY` | — | **Required.** |
| `DEEPGRAM_MODEL` | `nova-3` | Keyterms are a nova-3 feature. |
| `DEEPGRAM_LANGUAGE` | `en-US` | `hi` and `multi` work for Hindi/Hinglish. |
| `DEEPGRAM_KEYTERMS` | — | Comma-separated bias terms (nova-3 only). |
| `DEEPGRAM_ENDPOINTING_MS` | `800` | Silence that ends an utterance. An **accuracy** control, not just latency: at 300 ms — shorter than an ordinary pause for breath — sentences were cut in half, which reads badly and denies the model the context it uses to choose words and place punctuation. Lower for snappier live captions, raise for cleaner text. |
| `DEEPGRAM_UTTERANCE_END_MS` | `1000` | Ceiling on how long one utterance may run without a pause, so an unbroken stretch of speech is still split into readable turns. |

### The write-up

| Setting | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai`. |
| `ANTHROPIC_API_KEY` | — | Required when the provider is `anthropic`. |
| `OPENAI_API_KEY` | — | Required when the provider is `openai`. |
| `LLM_MODEL` | provider default | Blank means `claude-opus-5` / `gpt-4o`. |
| `ANALYSIS_ENABLED` | `true` | `false` gives transcripts only, and needs no LLM key. |
| `LLM_BASE_URL` | — | Points the `openai` provider at any OpenAI-compatible endpoint. |
| `AUTO_NOTES` | `true` | Write the notes up as soon as a recording ends, rather than waiting for somebody to press **Enhance notes**. Same model, same template, same result — it just happens without being asked, which is the point of a notetaker. Costs one LLM call per finished meeting, so `false` on a metered key lets you choose. |
| `ANONYMISE_ANALYSIS` | `false` | Replace real names with "Speaker A" in the transcript text **sent to the LLM**. The local transcript keeps the real names; only the third-party request is anonymised. For meetings where personal names should not leave the machine. |

**Running the analysis on a free tier.** Groq's developer tier does not train
on your data and needs no credit card:

```ini
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.groq.com/openai/v1
OPENAI_API_KEY=<your Groq key>
LLM_MODEL=openai/gpt-oss-120b
```

`LLM_MODEL` is **required** here — the blank default is `gpt-4o`, which only
exists on OpenAI. Groq retires model ids without notice, so on a 404 list the
live ones: `GET {LLM_BASE_URL}/models` with your key.

### Where things are kept

| Setting | Default | What it does |
|---|---|---|
| `OUTPUT_DIR` | `recordings` | One directory per meeting. |
| `SERVICE_STATE_DIR` | `~/.meetbot` | Web-UI accounts (`users.json`) and the session signing key (`session.key`). Outside the project on purpose — it is per-user, not per-checkout. Back it up; see [deploying](deploying.md). |
| `LOG_LEVEL` | `INFO` | Console level. The file always gets DEBUG, rotated at 2 MB with three older copies kept. |

### Recording this machine

| Setting | Default | What it does |
|---|---|---|
| `CALL_ALERTS` | `true` | When any app starts using the microphone — a Meet tab, WhatsApp, Zoom, Teams, anything — offer to record it, with a card in the page and a Windows notification. Reads the per-app microphone state Windows keeps for its own privacy indicator; no audio is opened. Windows only. |
| `VOICE_COMMANDS` | `false` | Listen for "Scribe, start recording", "Scribe, stop", "Scribe, mark this" and "Scribe, call this &lt;name&gt;" using the speech recogniser built into Windows. It runs offline: the audio never leaves the machine, nothing is written down, and it can only hear those phrases — ordinary conversation never matches one. The cost is that Scribe holds the microphone the whole time it runs, so Windows shows its microphone indicator throughout. Off by default, because that is a thing to agree to rather than discover. |
| `SAMPLE_RATE` | `16000` | What Deepgram is fed; device audio is resampled to it. |

### Bot mode

None of this matters unless you are sending the bot into a call.

| Setting | Default | What it does |
|---|---|---|
| `MEET_URL` | — | Optional default; `--url` overrides it. |
| `BOT_DISPLAY_NAME` | `Scribe (Recording)` | **Must** make it obvious to participants that this account records. A disclosure suffix is appended automatically if it does not — see [consent](consent.md). |
| `CHROME_PROFILE_DIR` | — | Persistent Chromium profile holding a signed-in Google session, created by `meetbot login`. Without it the bot joins anonymously, and Meet increasingly **refuses** anonymous guests outright — the pre-join screen becomes "You can't join this video call" and the host is never even asked. No selector can work around that; signing in is the fix. **Holds live Google session cookies: treat it like a password.** |
| `HEADLESS` | `true` | `false` to watch the browser — the way to debug broken Meet selectors. |
| `ADMISSION_TIMEOUT_S` | `300` | How long to wait in the lobby to be admitted. |
| `MAX_MEETING_DURATION_S` | `14400` | Hard cap on one meeting (4 hours). |
| `ALONE_TIMEOUT_S` | `120` | Leave after this long alone in the call. |
| `BROWSER_CHANNEL` | — | e.g. `chrome`. Blank uses bundled Chromium. |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8765` | The in-process audio bridge the page streams PCM to. |
| `PER_PARTICIPANT_AUDIO` | `false` | One Deepgram stream per participant — better speaker labels, one connection each. `false` is one mixed stream with diarization. |
| `SPEAKER_NAMES_FROM_CAPTIONS` | `true` | Turn Meet's own captions on (for **this** browser only) and use the speaker name Meet attaches to each entry instead of "Speaker 0". This is the only way to get real names: nothing in the page links an audio track to a participant, but Meet does the attribution server-side. Only the name and timing are used; the caption text is discarded. A per-viewer setting — it does not turn on captions, transcription or recording for anybody else. Degrades silently to generic labels. |
| `LIVE_CAPTIONS_OVERLAY` | `false` | Share a live-caption canvas as a screen-share, updated as speech is transcribed, visible to every participant without turning the bot's camera on. **Changes what other participants see**, so it is off by default, and the one Meet-DOM step it depends on ("Present now") is the least-tested selector in the project. |
| `PREFLIGHT_ON_START` | `true` | Run the readiness checks when the service starts. The tests turn this off; so can a server that would rather not open a Deepgram stream on every restart. |

---

## Verifying it

```bash
python -m meetbot check
```

`check` asks every question a live meeting would otherwise answer the hard
way:

| Check | Why it is worth the seconds |
|---|---|
| **Configuration** | Internally coherent — sample rate, provider, ports. |
| **Deepgram** | Opens a stream with **your** configured options, not defaults. A generic probe once passed while every real stream was rejected, because the failure lived entirely in options it never sent. |
| **AI notes** | One trivial completion. The summary runs *after* the meeting, so a dead key would otherwise surface when the material is already gone. |
| **Google sign-in** | Whether the profile is still signed in. Google expires these on its own schedule, and an expired one is invisible until Meet turns the bot away with people already waiting. |
| **Local capture** | Whether there is a loopback device, and whether it is producing silence. |
| **Playwright** | Chromium is installed and launches. Only bot mode needs it, so a missing browser is a warning, not a failure. |

Each failure prints the command that fixes it, and `check` exits non-zero
when nothing can record:

```
  OK    Configuration    valid
  OK    Deepgram         accepts nova-3/en-IN, 11 key term(s)
  OK    AI notes         key accepted, openai/gpt-oss-120b replied 'OK'
  FAIL  Google sign-in   The profile is signed out; Meet will refuse the bot
To fix Google sign-in: python -m meetbot login

Ready to record this machine: python -m meetbot record
Bot mode is unavailable - see the warnings above.
```

The same checks run when the service starts and are shown in the web UI,
which refuses to send the bot while something is broken. One module
(`meetbot/preflight.py`) backs all three, so "check says it is fine" and "the
bot will actually work" cannot drift apart.

Pass `--offline` to skip everything that touches the network.
