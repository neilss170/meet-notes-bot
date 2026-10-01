# Known limitations and failure behaviour

What is fragile, why, and what happens when each piece breaks.

[&larr; back to the README](../README.md)

---

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
