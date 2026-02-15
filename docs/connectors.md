# Connectors

A connector bridges an external platform (Discord, Telegram, Slack, etc.) and kiso's API. Lives in `~/.kiso/connectors/{name}/`.

Connectors are standalone processes. Kiso doesn't manage their lifecycle — they're just clients of the `/msg` API.

## Structure

```
~/.kiso/connectors/
├── discord/
│   ├── kiso.toml            # manifest (required)
│   ├── pyproject.toml       # python dependencies (uv-managed)
│   ├── run.py               # entry point (required)
│   ├── config.example.json  # example config (in repo)
│   ├── config.json          # actual config (gitignored, NO secrets)
│   ├── deps.sh              # system deps installer (optional, idempotent)
│   ├── README.md            # setup instructions
│   └── .venv/               # created by uv on install
└── .../
```

A directory is a valid connector if it contains `kiso.toml` (with `type = "connector"`) and `run.py`.

## kiso.toml

The manifest. Same structure as skills, different type.

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
webhook_secret = { required = false }  # → KISO_CONNECTOR_DISCORD_WEBHOOK_SECRET

[kiso.deps]
python = ">=3.11"
```

### Env Var Naming

Env vars follow the convention `KISO_CONNECTOR_{NAME}_{KEY}`, built automatically:

| Manifest key | Env var |
|---|---|
| `bot_token` | `KISO_CONNECTOR_DISCORD_BOT_TOKEN` |
| `webhook_secret` | `KISO_CONNECTOR_DISCORD_WEBHOOK_SECRET` |

Name and key are uppercased, `-` becomes `_`.

**Secrets always go in env vars, never in config.json.**

## config.json

Structural, non-secret configuration. The repo ships `config.example.json`, the real `config.json` is gitignored and created by the user post-install.

```json
{
  "kiso_api": "http://localhost:8333",
  "session_prefix": "discord",
  "webhook_port": 9001,
  "channel_map": {
    "general": "discord-general",
    "dev": "discord-dev"
  }
}
```

No tokens, no secrets. Those come from env vars declared in `kiso.toml`.

## What a Connector Does

1. Connects to the platform (Discord WebSocket, Telegram polling, etc.)
2. Listens for messages
3. POSTs to kiso's `/msg` endpoint:
   - `session`: mapped from platform context (e.g. Discord channel ID)
   - `user`: mapped from platform user (ideally to a Linux username)
   - `content`: message text
   - `webhook`: callback URL the connector exposes to receive responses
4. Receives webhook callbacks from kiso
5. Sends responses back to the platform

## deps.sh

Optional. Installs system-level dependencies. Must be **idempotent** — safe to run on both first install and updates.

```bash
#!/bin/bash
set -e

apt-get update -qq
apt-get install -y --no-install-recommends opus-tools libffi-dev
```

Runs inside the Docker container. If it fails, kiso warns the user and suggests asking the bot to fix it.

## Installation

Only admins can install connectors.

### Via CLI

```bash
# official (resolves from kiso-run org)
kiso connector install discord
# → clones git@github.com:kiso-run/connector-discord.git
# → ~/.kiso/connectors/discord/

# unofficial (full git URL)
kiso connector install git@github.com:someone/my-connector.git
# → ~/.kiso/connectors/github-com_someone_my-connector/

# unofficial with custom name
kiso connector install git@github.com:someone/my-connector.git --name custom
# → ~/.kiso/connectors/custom/
```

### Naming Convention

Same as skills:

| Source | Name |
|---|---|
| Official (`kiso connector install discord`) | `discord` |
| Unofficial URL | `{domain}_{namespace}_{repo}` |
| Explicit `--name` | whatever you pass |

### Install Flow

```
1. git clone → ~/.kiso/connectors/{name}/
2. Validate kiso.toml (exists? type=connector? has name?)
3. Validate run.py exists
4. If deps.sh exists → run it
   ⚠ on failure: warn user, suggest "ask the bot to fix deps for connector {name}"
5. uv sync (pyproject.toml → .venv)
6. Check [kiso.deps].bin (verify with `which`)
7. Check [kiso.connector.env] vars
   ⚠ KISO_CONNECTOR_DISCORD_BOT_TOKEN not set (warn, don't block)
8. If config.example.json exists and config.json doesn't → copy it
```

### Update / Remove / Search

```bash
kiso connector update discord
kiso connector update all
kiso connector remove discord
kiso connector list
kiso connector search [query]
# → GET https://api.github.com/search/repositories?q=org:kiso-run+topic:kiso-connector
```

## Running

```bash
kiso connector discord run       # start the connector
kiso connector discord stop      # stop it
```

Or run manually:

```bash
python ~/.kiso/connectors/discord/run.py
```

Or use systemd, supervisor, etc. Kiso does not manage connector lifecycles beyond start/stop convenience commands.
