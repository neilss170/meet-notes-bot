# Consent and disclosure

Read this before recording anybody. It is the part of this project with legal weight rather than technical.

[&larr; back to the README](../README.md)

---

**Local capture makes this more your responsibility, not less.** The bot
announced itself: it appeared in the participant list with a name saying it
was recording. Recording from your own machine announces nothing to anybody.
Nobody in the meeting can tell it is happening, which means telling them is
now entirely on you. Do it.

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
