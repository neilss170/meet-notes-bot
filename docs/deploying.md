# Deploying Scribe

Three shapes, depending on whose audio you are recording. The first is what
almost everybody wants.

[&larr; back to the README](../README.md)

---

## What has to survive a restart

Wherever you run it, four things are state and the rest is replaceable:

| Path | Set by | What you lose without it |
|---|---|---|
| `.env` | — | Your keys and every setting |
| `recordings/` | `OUTPUT_DIR` | Every transcript, summary and note |
| `~/.meetbot/` | `SERVICE_STATE_DIR` | Web-UI accounts (`users.json`) and the session signing key (`session.key`) — everyone is signed out and has to be re-created |
| `chrome-profile/` | `CHROME_PROFILE_DIR` | The bot's Google sign-in, so bot mode stops working until you run `login` again |

`~/.meetbot/` is the one people forget. It is outside the project directory on
purpose — it is per-user, not per-checkout — so a deployment that only backs
up the repo loses its accounts.

The log is capped: `scribe-server.log` rotates at 2 MB and keeps three older
copies, so an unattended tray cannot fill a disk.

---

## 1. On your own machine

The normal case, and the only one that can record meetings *you* are in.

```bash
python -m meetbot shortcut --startup
```

That puts Scribe on the Desktop and starts it when you sign in; both run
`meetbot tray`, which serves the UI on `127.0.0.1:8080` and puts an icon by
the clock. Nothing else to configure.

First start prints a generated `admin` password **once**. Started from the
Desktop shortcut there is no window to print it to, so look for it in
`scribe-server.log` — it is the block of `=` signs near the top. If it has
already rotated away:

```bash
python -m meetbot users passwd admin
```

---

## 2. Shared on a network

Worth being clear about what this does and does not give you, because the
name is misleading: **a shared Scribe records the machine it runs on, not the
machine you are browsing from.** Local capture reads the server's sound card.
Pointing four people at one instance does not give four people a notetaker —
it gives four people a view of one room's audio.

So this is for one shared recording machine (a meeting room PC), or for
reading and searching past meetings from your phone. For recording your own
calls, run your own copy.

```bash
python -m meetbot users add alice --role member
python -m meetbot serve --host 0.0.0.0 --port 8080
```

Accounts are enforced on every route — there is no anonymous access, and no
read-only bypass. But **there is no TLS**, so on anything larger than a
trusted LAN put it behind a reverse proxy that terminates HTTPS and forwards
to `127.0.0.1:8080`. Without one, session cookies cross the network in clear.

**Copy** degrades slightly outside `localhost`: browsers only give
`navigator.clipboard` to a secure context, and a LAN address is not one, so
the button falls back to `document.execCommand("copy")`. It works, but some
browsers prompt where the loopback path does not.

**Voice commands** are not affected, and not useful either. They run Windows'
own speech engine against the *server's* default microphone, so a shared
instance listens to the room it is sitting in, not to whoever is browsing —
the same point as above, in a different disguise.

---

## 3. A server with no sound card

A headless Linux box can run everything *except* recording itself, because
local capture is WASAPI loopback and that is Windows-only. What it can do is
bot mode: send a browser into a Google Meet call and record from inside it.

```bash
pip install -e ".[serve,anthropic]"
playwright install --with-deps chromium
```

`check` will report local capture as unavailable and say "Ready to send the
bot" — that is the correct answer here, not a failure.

The catch is the Google sign-in. `meetbot login` opens a visible browser and
waits for you to complete it by hand, including whatever 2FA the account has,
so it cannot be done over SSH. Sign in on a desktop, then copy the profile
directory across and point `CHROME_PROFILE_DIR` at it. Treat it like a
password: it holds live session cookies, and anyone with it is signed in as
that account.

Google expires these sessions on its own schedule, so a server running bot
mode needs `python -m meetbot check` on a timer — an expired session is
invisible until a meeting starts without the bot.

### There is no Dockerfile

Deliberately, for now. Local capture cannot work in a container on any
platform, so an image would only ever serve bot mode, and that path needs the
signed-in Chrome profile mounted in and kept fresh — which is the hard part,
and not one a Dockerfile solves. Writing one that had never been built would
be worse than not having one.

If you want to build it yourself, what it needs is: a Playwright base image
(it carries the browser and system libraries), `pip install -e ".[serve,anthropic]"`,
`SERVICE_STATE_DIR` and `OUTPUT_DIR` pointed at volumes, the Chrome profile
mounted read-write, and `meetbot serve --host 0.0.0.0`. Ports: one, 8080.

---

## Upgrading

```bash
git pull
pip install -e ".[all]"       # picks up new dependencies
python -m meetbot setup       # reports any new required setting; never overwrites .env
python -m meetbot check
```

`setup` is safe on an existing install — that is what it is for. It reads your
`.env` rather than replacing it, and tells you only about what is missing.

New settings that appear in `.env.example` are **not** added to your `.env`
automatically; they fall back to their defaults, which are chosen so an
un-upgraded `.env` keeps working. [Configuration](configuration.md) lists them
all.
