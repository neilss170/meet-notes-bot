# Development

Running the tests, what they cover, and how secrets are handled.

[&larr; back to the README](../README.md)

---

```bash
pip install -e ".[serve,dev]"
playwright install chromium   # a few tests drive a real browser
python -m pytest -q           # 895 tests, no network, no credentials
```

**No test touches the network or needs an API key.** Deepgram, both LLM SDKs
and the browser's audio hook are stubbed, so the suite is the same on a
laptop and on CI. That is deliberate: the readiness checks *do* open a real
Deepgram stream and call the LLM, so they are turned off during tests
(`preflight_on_start=False`) — a suite that depends on live credentials
passes where the keys happen to work and fails everywhere else.

A handful of tests launch real Chromium, because some of this cannot be
faked: a canvas capture stream that emits no frames looks identical to a
working one until a compositor runs, and Chromium's profile-locking
behaviour is the reason per-run profile copies exist at all. The web UI is
driven the same way wherever a test of the route cannot reach the question:
the Accounts panel rebuilds itself from the account list after every change,
and whether a rejected password survives that redraw is only answerable in a
browser. They skip themselves when Chromium is absent.

The suite covers the transcript store (durability, corrupt lines, schema
evolution), the formatters, Deepgram result parsing and stream-URL
construction, the bridge's frame decoding and timeline rebasing, caption
based speaker attribution, config validation and disclosure enforcement, the
analysis module, accounts and sessions, the readiness checks, and the
runner's failure containment.

CI runs the same command on every push: Ubuntu on Python 3.11 and 3.12, and
macOS on 3.12. Windows is the machine this is developed on, so the point of
CI is the platforms that cannot be checked from here — everything but
capturing the speakers is meant to work anywhere.

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
