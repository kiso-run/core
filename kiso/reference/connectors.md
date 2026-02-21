# Connector Authoring Reference

## File Structure

```
~/.kiso/connectors/{name}/
├── kiso.toml           # manifest (required)
├── pyproject.toml      # python deps (required, uv-managed)
├── run.py              # entry point (required)
├── config.example.toml # default config (shipped in repo, copied to config.toml on install)
├── config.toml         # actual config (gitignored, NO secrets)
├── deps.sh             # system deps (optional, idempotent)
├── .gitignore
├── tests/              # tests (recommended)
│   └── test_connector.py
└── .venv/              # created by uv on install
```

No `src/` layout. Kiso's supervisor runs `.venv/bin/python run.py` as a subprocess. All code lives in `run.py` or modules imported by it.

## kiso.toml

```toml
[kiso]
type = "connector"
name = "discord"
version = "0.1.0"
description = "Discord bridge for Kiso"

[kiso.connector]
platform = "discord"

[kiso.connector.env]
bot_token = { required = true }        # → KISO_CONNECTOR_DISCORD_BOT_TOKEN
kiso_token = { required = true }       # → KISO_CONNECTOR_DISCORD_KISO_TOKEN

[kiso.deps]
python = ">=3.11"
```

### Env Var Naming

Convention: `KISO_CONNECTOR_{NAME}_{KEY}` (uppercased, `-` → `_`).

### Token name matters

The `kiso_token` env var must hold a value matching a `[tokens]` entry in `~/.kiso/config.toml`. The **token name** (e.g. `discord`) determines which `aliases.*` field kiso uses for user resolution:

```toml
# ~/.kiso/config.toml
[tokens]
discord = "tok-abc123"

[users.marco]
role = "admin"
aliases.discord = "Marco#1234"   # ← matched via token name "discord"
```

## config.toml

Non-secret, deployment-specific config. Repo ships `config.example.toml`; real file is gitignored and created on install.

```toml
kiso_api = "http://localhost:8333"
session_prefix = "discord"
webhook_port = 9001
webhook_host = "0.0.0.0"
bot_prefix = ""

[channels]
# "channel_id" = "session-name"
# "1234567890123456789" = "discord-general"
```

No secrets here — deploy secrets come from env vars declared in `kiso.toml`.

Load from `run.py`:

```python
import tomllib
from pathlib import Path

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.toml"
    if not config_path.exists():
        print("config.toml not found", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomllib.load(f)
```

## Kiso API Endpoints

Connector authenticates all calls with: `Authorization: Bearer <kiso_token>`.

### POST /sessions — Register a session

```json
{"session": "discord-general", "webhook": "http://connector:9001/callback", "description": "Discord #general"}
```

Returns 201 (created) or 200 (exists). Sets webhook URL for response delivery.
Session IDs must match: `^[a-zA-Z0-9_@.\-]{1,255}$`.

Call this **on startup** for each mapped channel.

### POST /msg — Send a user message

```json
{"session": "discord-general", "user": "Marco#1234", "content": "hello"}
```

Returns 202. `user` is the platform identity as-is — kiso resolves it via `aliases.{token_name}`.

Error responses: `401` (invalid token), `413` (content too large), `429` (queue full).
If user not recognized: `202` with `{"queued": false, "untrusted": true}` — message saved but not processed.

### GET /status/{session}?after={id} — Poll for responses

Filter for `type: "msg"` tasks — those are user-facing responses (the `output` field is the text to send).

### Webhook callback (received by connector)

Kiso POSTs to the webhook URL registered in `/sessions`:

```json
{"session": "discord-general", "task_id": 42, "type": "msg", "content": "Here are the results...", "final": true}
```

- `final: true` on the last msg task after all reviews pass
- Kiso retries 3 times with backoff (1s, 3s, 9s) on non-2xx responses
- If `X-Kiso-Signature` header present: `sha256=<hmac-sha256-hex>` over raw body

Connector must return HTTP 2xx to acknowledge.

## Connector Lifecycle

1. **Startup**: load config, validate env vars (fail fast if missing), start webhook server, connect to platform, register sessions via `POST /sessions`
2. Listen for messages → `POST /msg`
3. Receive webhook callbacks → send responses to platform
4. **Polling fallback (mandatory)**: if no webhook arrives within 30s after `POST /msg`, poll `GET /status/{session}?after={last_task_id}` every 5s. Stop on `final: true`.
5. **SIGTERM**: close platform connection, stop webhook server, exit 0

## Error Handling and Logging

- Use Python `logging` module, output to stdout (kiso captures to `connector.log`)
- **Never log secret values** (tokens, API keys, message content)
- Platform API unreachable: log error, do NOT crash — retry on next message
- Invalid webhook payload: log warning, return 400
- Unknown session in webhook: log warning, return 200 (don't make kiso retry)
- **Exit 0 on SIGTERM** — kiso's supervisor treats non-zero as a crash and restarts with backoff
- Platform message limits (e.g. Discord 2000 chars): split at paragraph boundaries (`\n\n`), fall back to hard split

## .gitignore

```
__pycache__/
*.pyc
.venv/
config.toml
.pid
.status.json
connector.log
.installing
```

## Testing

Connectors have their own venv and `pyproject.toml`, so tests live inside the connector repo. Assumes kiso is installed and the connector's venv is set up.

### pyproject.toml — add test deps

```toml
[dependency-groups]
dev = ["pytest>=8", "httpx>=0.28", "pytest-asyncio>=0.25"]
```

### Unit test — message mapping logic

```python
# tests/test_connector.py

def test_session_mapping():
    """Verify platform context maps to correct kiso session ID."""
    from run import map_channel_to_session
    assert map_channel_to_session("general") == "discord-general"

def test_webhook_payload_parsing():
    """Verify connector parses kiso webhook payloads correctly."""
    from run import parse_webhook
    payload = {
        "session": "discord-general",
        "task_id": 42,
        "type": "msg",
        "content": "Hello!",
        "final": True,
    }
    result = parse_webhook(payload)
    assert result.content == "Hello!"
    assert result.final is True
```

### Integration test — against a running kiso instance

```python
# tests/test_integration.py
import httpx
import pytest

KISO_API = "http://localhost:8333"
TOKEN = "your-test-token"

@pytest.fixture
def kiso_client():
    return httpx.Client(
        base_url=KISO_API,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

def test_register_session(kiso_client):
    resp = kiso_client.post("/sessions", json={
        "session": "test-connector",
        "description": "Integration test",
    })
    assert resp.status_code in (200, 201)

def test_send_message(kiso_client):
    resp = kiso_client.post("/msg", json={
        "session": "test-connector",
        "user": "TestUser#1234",
        "content": "hello from test",
    })
    assert resp.status_code == 202
```

### Running tests

```bash
cd ~/.kiso/connectors/{name}

# Unit tests (no kiso instance needed)
uv run --group dev pytest tests/test_connector.py -v

# Integration tests (requires running kiso)
uv run --group dev pytest tests/test_integration.py -v
```

### Tips

- Keep unit tests separate from integration tests — unit tests should run without a kiso instance
- Mock the platform SDK (discord.py, python-telegram-bot, etc.) for unit tests
- Test the polling fallback path, not just the webhook path
- Test message splitting for long responses
- Test startup validation: missing env vars → clear error

## License

Official connectors use the **MIT License**. Third-party connectors can use any license.

## Key Conventions

- Install: `kiso connector install {name|url}` (official: `kiso-run/connector-{name}`)
- If `config.example.toml` exists and `config.toml` doesn't → auto-copied on install
- Run: `kiso connector run {name}` (daemon with PID tracking, restart with backoff)
- Stop: `kiso connector stop {name}` (sends SIGTERM)
- Logs: `~/.kiso/connectors/{name}/connector.log`
- Environment: only `PATH` + declared env vars from `[kiso.connector.env]`
- Exit 0 on SIGTERM = clean shutdown. Non-zero = crash (triggers restart).
- Polling fallback is mandatory — webhooks are best-effort
- `uv` for dependency management — kiso runs `uv sync` on install
