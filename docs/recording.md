# Recording a meeting

Every way to start a recording, what each one produces, and the flags worth knowing about.

[&larr; back to the README](../README.md)

---

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

### Recording without a bot

This is the default, and the reason the bot exists only as a fallback.

Windows exposes **WASAPI loopback**: a tap on the render endpoint, which is
everything your speakers are playing. That is every remote participant, in
any application. Your microphone is captured separately, and the two are kept
apart all the way to Deepgram:

| Channel | Source | Speaker label |
|---|---|---|
| 0 | your microphone | `You` |
| 1 | your speakers | `Speaker 0`, `Speaker 1`, … |

Keeping them separate is not tidiness. Diarization *guesses* who spoke from
voice characteristics; your microphone channel is definitionally you, so at
least one speaker is labelled from where the audio came from rather than from
a model's inference. Only the far side needs guessing at.

```bash
python -m meetbot record --title "Weekly sync"
python -m meetbot record --list-devices        # pick devices by index
python -m meetbot record --speakers 10 --microphone 9
python -m meetbot record --no-microphone       # capture only the far side
```

Two things are worth knowing before trusting it:

**Loopback taps the mix *after* the volume control.** Muted speakers, or a
volume of zero, record silence — and nothing looks broken while it happens.
You get a perfectly well-formed empty transcript. Scribe checks for this
before recording and warns during it, but if a recording comes back empty,
that is the first thing to check.

**It captures everything the machine plays**, not only the meeting. A video
playing in another tab is recorded too. That is the trade for not needing a
bot, an account, or anyone's permission to join.

Compared with the bot:

| | Local capture | Bot |
|---|---|---|
| Joins the call | no | yes, visibly |
| Google account | not needed | required, expires every few hours |
| Meeting tools | any | Google Meet only |
| Breaks when Meet redesigns | no | yes |
| Your own voice | labelled `You` | diarized like everyone else |
| Captures other apps' audio | yes | no |
| Platform | Windows | anywhere |

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

#### Not even a command prompt

A notetaker you have to remember to start from a terminal is one that is not
running when the call begins, which is the only moment it is worth anything.

```bash
python -m meetbot shortcut            # a Scribe icon on the Desktop
python -m meetbot shortcut --startup  # ...and start it when you sign in
python -m meetbot shortcut --remove   # take both away again
```

The icon runs `meetbot launch`, which is safe to press twice: when Scribe is
already listening it opens the page rather than starting a second server that
cannot bind the port. The Startup entry runs `meetbot tray`, because signing in
should not throw a browser window at you.

Both run under `pythonw.exe`, so no console window appears. The server's
output goes to `scribe-server.log` beside the recordings, which is also where
the admin password printed on the very first run can be read back.

#### The icon by the clock

A windowless server has no presence. Minimise the page and Scribe is gone:
nothing in the taskbar, no way to tell whether it is listening, and stopping it
means picking the right `pythonw.exe` out of Task Manager. So while it runs it
sits in the notification area:

```bash
python -m meetbot tray          # serve, with the icon
python -m meetbot tray --open   # ...and open the page as well
```

- **Hover it** and it says what is happening — `Recording · Standup · 4:07`,
  `Writing up · Standup`, `Not recording`, or which setup check is failing.
  The tile turns red while a meeting is being captured — beside a row of
  monochrome system glyphs it is the only red thing on the taskbar.
- **Click it** to bring the page back.
- **Right-click it** for the things worth doing without the page open: start or
  stop a recording, jump straight to the notes of the call that just finished,
  open the recordings folder or the server log.
- **Quit** closes Scribe's window, stops the recording and waits for its
  write-up, rather than killing the process mid-sentence and losing the
  summary.

Scribe opens its page as a **window of its own** — Chromium's `--app` mode, in
your ordinary browser profile, so your session and extensions come with it.
That is what lets Quit close it: a tab you opened yourself cannot be closed by
the program that served it, which is a browser security rule and a good one.
The window is found again by its title, because an app window is titled
`Scribe` where a tabbed one is titled `Scribe - Google Chrome` — so quitting
closes that window and leaves every other tab alone. With no Chromium browser
installed the page opens as an ordinary tab, and that tab says *Scribe has
stopped* rather than sitting there looking live.

The icon lives in the same process as the server on purpose: it reads the job
list directly, so nothing has to be authenticated to draw a tooltip. It needs
`pystray`, which comes with `pip install -r requirements.txt`; without it
`meetbot tray` just serves, because a Scribe you cannot see still records the
call.

Windows 11 hides icons it has not seen before behind the `^`, which for this
one would defeat the point, and there is no API for it — the setting is a
registry value under `HKCU\Control Panel\NotifyIconSettings` or a
drag with the mouse. So Scribe writes that one value, for the single entry
carrying both its own tooltip and this interpreter's path. Drag the icon back
into the flyout if you would rather it stayed hidden.

Windows only so far: it asks Windows where the Desktop and Startup folders
actually are, because a machine signed into OneDrive has its Desktop
redirected and a shortcut written to `%USERPROFILE%\Desktop` never appears.
Elsewhere, `meetbot serve --open` is the same thing with a terminal, and your
desktop environment's own startup applications list will run it at sign-in.

#### The notepad

The transcript answers *what was said*. It cannot know what **you** thought
mattered — two people in the same call want different notes out of it. So the
notes tab is a plain textarea on the left and the finished notes on the right:

- **Type as the meeting runs.** Fragments are the point. `api behind?? 2wks`,
  `priya -> migration script, fri`. It autosaves about a second after you stop
  typing, and leaving the tab or closing the page flushes it immediately.
- **Press Enhance** (or `Ctrl`/`Cmd`+`Enter`). Your notes are sent with the
  transcript, and what comes back follows *your* structure with the detail
  filled in — the numbers, owners and dates you abbreviated or missed.
- **Your notes are never overwritten.** `notes.md` keeps exactly what you
  typed; the enhanced version is a separate file. If enhancement produces
  something worse, the original is still there, and you can re-enhance.

**Empty notes are a supported case, not a degenerate one.** The bot often runs
unattended, so enhancing a meeting you never typed into writes the notes from
the transcript alone, shaped by whichever template you picked.

Templates change the shape of the output, not the machinery:

| Template | Produces |
|---|---|
| General | Summary, notes, decisions, action items |
| One-to-one | Discussed, wins, blockers, feedback, for next time |
| Standup | Grouped per person, then blockers collected in one place |
| Client call | What they asked for, concerns raised, commitments, follow-up |
| Interview | Background, questions and answers, signals, follow-up questions |
| Planning | The question, options with arguments, where it landed, open questions |

The prompt is explicit that a note the transcript cannot support is left
exactly as you wrote it rather than elaborated into something plausible — you
may have been looking at a screen the bot cannot hear. Enhanced notes that
contain invented detail would be worse than no notes at all.

#### Ask

The **Ask** tab answers questions about one meeting from its transcript and
notes, and nothing else. "What did I miss?", "What was decided?", "List every
action item and who owns it." If the meeting did not cover it, the answer says
so rather than reasoning outwards into what was probably meant. Questions and
answers are kept per meeting in `chat.jsonl`, and earlier turns are replayed
so follow-ups work.

Both features go through the same LLM provider as the summary, so they cost
the same free-tier quota and are unavailable when `ANALYSIS_ENABLED=false` —
notes still save, they just do not get enhanced.

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
restarting the service does not sign everyone out, and every request is
authorised against the live account list, so adding someone, demoting them,
removing them or changing their password from the UI takes effect
immediately rather than whenever their cookie lapses.

Each row of that panel has a **Password** button, which opens a field
underneath it. The new password works at the next sign-in; it does not sign
anybody out, because the session cookie is signed over the username and an
expiry with no password material in it. To end the sessions already open as
well, delete `~/.meetbot/session.key` and restart — that invalidates every
cookie the old key signed, for everyone.

`meetbot users` does the same four jobs from a terminal, and is the only
route left if the last admin password is lost:

```bash
python -m meetbot users list
python -m meetbot users add priya --role member
python -m meetbot users passwd admin
python -m meetbot users delete priya
```

**These write `users.json`, and a service that is already running will not
notice.** The account list is read once, when the service starts. So a
password changed this way is correct on disk and ignored by the process doing
the authenticating, with nothing to warn you — the command succeeded, it just
did not reach the running server. Restart it afterwards, or use the panel,
which changes the list the server is actually reading from.

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
| `POST /api/recordings` | `{"title": "..."}` — record this machine, no bot |
| `GET /api/audio-devices` | Speakers and microphones that can be captured |
| `POST /api/audio-devices/probe` | Listen briefly; catches muted output |
| `GET /api/meetings/{id}` | Status plus live transcript |
| `POST /api/meetings/{id}/stop` | Leave the call and write the summary |
| `PUT /api/meetings/{id}/title` | `{"title": "..."}` — call it what it actually was |
| `DELETE /api/meetings/{id}` | Delete the meeting, its recording and everything asked about it |
| `GET /api/meetings/{id}/artifact/{name}` | `transcript.md`, `analysis.md`, `notes.md`, `enhanced.md`, … |
| `GET /api/meetings/{id}/notepad` | Notes, enhanced notes, template, chat |
| `GET /api/meetings/{id}/transcript` | The whole transcript, in the lines citations point at |
| `PUT /api/meetings/{id}/speakers` | `{"names": {"Speaker 0": "Priya"}}` — real names, once, everywhere |
| `PUT /api/meetings/{id}/notes` | `{"notes": "..."}` — autosave the notepad |
| `POST /api/meetings/{id}/enhance` | `{"template": "standup"}` — write the notes up |
| `POST /api/meetings/{id}/ask` | `{"question": "..."}` — ask about this meeting |
| `DELETE /api/meetings/{id}/chat` | Forget the questions asked about it |
| `GET` / `POST /api/chat` | Ask across every meeting this account can see |
| `DELETE /api/chat` | Forget those questions |
| `GET /api/calls` | Calls started on this machine and not yet answered (admin only) |
| `POST /api/calls/{id}/dismiss` | Not this call |
| `POST /api/calls/mute` | `{"app_id": "..."}` — never offer for this app |
| `GET /api/voice` | Whether voice commands are listening, and the last one heard |
| `GET /api/templates` | The note shapes the UI offers |
| `GET /api/health` | What is working, and the command that fixes what is not |
| `POST /api/health/refresh` | Re-run the checks after fixing something |
| `GET /api/users` | Admin only: list accounts |
| `POST /api/users` | Admin only: `{"username": "...", "password": "...", "role": "member"}` |
| `POST /api/users/{name}/password` | Admin only: `{"password": "..."}` — effective at once, signs nobody out |
| `DELETE /api/users/{name}` | Admin only: remove an account. Not your own |
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
| `notes.md` | Exactly what you typed in the notepad. Never rewritten by the model. |
| `enhanced.md` | Your notes filled in from the transcript. Overwritten each time you enhance. |
| `notepad.json` | Which template was used, and when things were last written. |
| `chat.jsonl` | One record per question asked about the meeting. |
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

# Serve with an icon in the notification area, which is what the shortcuts run.
python -m meetbot tray
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
