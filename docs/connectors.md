# Connectors

A connector bridges an external platform (Discord, Slack, Telegram,
email, …) and kiso's HTTP API. It is an **external process that kiso
supervises** — kiso starts it, restarts it on crash, and captures its
stdout/stderr to a log file. Kiso does **not** install connector
binaries; you bring your own via `uvx`, `pip`, `docker`, a local clone,
or any other mechanism of your choosing.

Connectors are declared in `config.toml` under `[connectors.<name>]`,
structurally parallel to `[mcp.<name>]`.

## Config shape

```toml
[connectors.discord]
command = "uvx"
args    = ["kiso-discord-connector"]

# Optional: per-connector environment (${env:FOO} is expanded from the
# kiso process env at config load; KISO_* keys are reserved).
env = { DISCORD_TOKEN = "${env:DISCORD_TOKEN}" }

# Optional: working directory the command runs in.
# cwd = "/opt/discord"

# Optional: per-connector API token used when the connector POSTs to
# kiso's /msg endpoint. Resolved against config.toml [tokens].
# token = "${env:KISO_CONNECTOR_DISCORD_TOKEN}"

# Optional: webhook URL kiso posts results to. HMAC-signed with
# config.webhook_secret.
# webhook = "http://localhost:9001/kiso-results"

# Optional: disable without removing.
# enabled = false
```

Required: `command`. Everything else is optional.

Validation happens at config load and at `kiso connector add`:
non-empty `command`, list-of-strings `args`, no `KISO_*` keys in
`env`, valid `${env:VAR}` references.

## CLI

Kiso owns the **supervisor lifecycle**, not the install flow:

```
kiso connector list                     # configured connectors + run state
kiso connector start  <name>            # spawn as a daemon, restart on crash
kiso connector stop   <name>            # SIGTERM, wait 5s, SIGKILL fallback
kiso connector status <name>            # running | stopped | gave up
kiso connector logs   <name> [-n 50]    # tail connector.log
kiso connector add    <name> --command X [--args ...] [--env K=V ...] \
                             [--cwd P] [--token T] [--webhook U]
kiso connector migrate                  # print suggested config blocks
                                        # for legacy ~/.kiso/connectors/<n>/
                                        # installs (pre-v0.10 only)
```

There is **no** `kiso connector install/update/remove/search/test` —
the connector binary is not kiso's concern.

## Supervisor state

When you run `kiso connector start <name>`, kiso lazily creates
`~/.kiso/connectors/<name>/` and writes three files there for as long
as the supervisor is alive:

```
~/.kiso/connectors/<name>/
├── .pid            # supervisor PID
├── .status.json    # restart count, consecutive failures, gave_up flag
└── connector.log   # merged stdout + stderr
```

Restart policy:

- **Clean exit (code 0)** — supervisor exits, no restart.
- **Crash (non-zero)** — restart with exponential backoff
  (1s → 2s → 4s → … → 60s cap).
- **Stable-run reset** — if the child ran for ≥ 60s before crashing,
  the consecutive-failure counter resets.
- **Give up** — after 5 consecutive quick failures the supervisor
  stops trying and writes `gave_up: true` to `.status.json`.
- **SIGTERM** — forwarded to the child; supervisor exits cleanly.

## Protocol contract

A connector is any process that speaks kiso's HTTP API. At minimum it
must:

1. **Authenticate.** Use the `--token` you declared (or read
   `config.toml` directly). All requests to `/msg`, `/sessions`,
   `/status/...` must carry an `Authorization: Bearer <token>` header.
2. **Register sessions.** `POST /sessions` with at least `session`
   (opaque string — the connector picks its own naming) and `webhook`
   (the URL kiso will callback).
3. **Submit messages.** `POST /msg` with `session`, `user`, `content`.
4. **Receive results.** When kiso finishes a plan it calls your
   `webhook` with an HMAC signature in `X-Kiso-Signature` (HMAC-SHA256
   over the raw body, keyed on `config.webhook_secret`). Verify before
   acting.
5. **Poll on webhook drop.** If a webhook doesn't arrive within a
   reasonable timeout, `GET /status/<session>?after=<last_task_id>` to
   recover missed responses. This is a **protocol requirement** — kiso
   may drop callbacks transiently under load.

## Minimal example

A ~40-line Python stub that registers a session, submits a message,
and verifies the HMAC on the callback. No manifest, no venv, no
`deps.sh` — everything you need is the config block above and this
script.

```python
# runner.py — run with: uvx --from httpx httpx && python runner.py
import hashlib, hmac, json, os, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

API = "http://localhost:8333"
TOKEN = os.environ["KISO_CONNECTOR_DEMO_TOKEN"]
SECRET = os.environ["KISO_WEBHOOK_SECRET"].encode()
WEBHOOK = "http://localhost:9001/kiso"


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        sig = self.headers.get("X-Kiso-Signature", "")
        expect = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            self.send_response(401); self.end_headers(); return
        payload = json.loads(body)
        print("kiso →", payload.get("reply") or payload)
        self.send_response(204); self.end_headers()


def serve() -> None:
    HTTPServer(("0.0.0.0", 9001), H).serve_forever()


threading.Thread(target=serve, daemon=True).start()

headers = {"Authorization": f"Bearer {TOKEN}"}
httpx.post(f"{API}/sessions", json={"session": "demo", "webhook": WEBHOOK}, headers=headers)
httpx.post(f"{API}/msg", json={"session": "demo", "user": "alice", "content": "hello"}, headers=headers)
threading.Event().wait()
```

Config:

```toml
[connectors.demo]
command = "python"
args    = ["/home/me/demo/runner.py"]
env     = { KISO_CONNECTOR_DEMO_TOKEN = "${env:DEMO_TOKEN}", KISO_WEBHOOK_SECRET = "${env:KISO_WEBHOOK_SECRET}" }
```

Then `kiso connector start demo`, and the connector is up under
supervision.

## Migrating from the pre-v0.10 layout

If you have `~/.kiso/connectors/<name>/` dirs from a previous install
(with `kiso.toml`, `pyproject.toml`, `run.py`, `.venv/`), kiso v0.10
will log a one-line warning at startup. Run `kiso connector migrate`
to see a suggested `[connectors.<name>]` block you can paste into your
`config.toml`. The old directory is otherwise ignored — kiso no longer
reads `kiso.toml` from it, and you can delete it once the config block
is in place.
