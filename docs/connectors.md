# Connectors

A connector bridges an external platform (Discord, Telegram, Slack, etc.) and kiso's API. Lives in `~/.kiso/connectors/{name}/`.

Connectors are long-running daemon processes managed by kiso.

## Structure

```
~/.kiso/connectors/
├── discord/
│   ├── kiso.toml            # manifest (required)
│   ├── pyproject.toml       # python dependencies (required, uv-managed)
│   ├── run.py               # entry point (required)
│   ├── config.example.toml  # example config (in repo)
│   ├── config.toml          # actual config (gitignored, NO secrets)
│   ├── deps.sh              # system deps installer (optional, idempotent)
│   ├── README.md            # setup instructions
│   └── .venv/               # created by uv on install
└── .../
```

A directory is a valid connector if it contains `kiso.toml` (with `type = "connector"`), `pyproject.toml`, and `run.py`.

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

**Secrets always go in env vars, never in config files.**

## config.toml

Structural, non-secret, deployment-specific configuration. The repo ships `config.example.toml`, the real `config.toml` is gitignored and created by the user post-install.

```toml
kiso_api = "http://localhost:8333"
session_prefix = "discord"
webhook_port = 9001

[channel_map]
general = "discord-general"
dev = "discord-dev"
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

### Unofficial Repo Warning

When installing from a non-official source (not `kiso-run` org), kiso warns:

```
⚠ This is an unofficial package from github.com:someone/my-connector.
  deps.sh will be executed and may install system packages.
  Review the repo before proceeding.
  Continue? [y/N]
```

Use `--no-deps` to skip `deps.sh` execution:

```bash
kiso connector install git@github.com:someone/my-connector.git --no-deps
```

### Naming Convention

| Source | Name |
|---|---|
| Official (`kiso connector install discord`) | `discord` |
| Unofficial URL | `{domain}_{namespace}_{repo}` |
| Explicit `--name` | whatever you pass |

### Install Flow

```
1. git clone → ~/.kiso/connectors/{name}/
2. Validate kiso.toml (exists? type=connector? has name?)
3. Validate run.py and pyproject.toml exist
4. If deps.sh exists → run it (with warning for unofficial repos)
   ⚠ on failure: warn user, suggest "ask the bot to fix deps for connector {name}"
5. uv sync (pyproject.toml → .venv)
6. Check [kiso.deps].bin (verify with `which`)
7. Check [kiso.connector.env] vars
   ⚠ KISO_CONNECTOR_DISCORD_BOT_TOKEN not set (warn, don't block)
8. If config.example.toml exists and config.toml doesn't → copy it
```

### Update / Remove / Search

```bash
kiso connector update discord          # git pull + deps.sh + uv sync
kiso connector update all
kiso connector remove discord
kiso connector list
kiso connector search [query]
# → GET https://api.github.com/search/repositories?q=org:kiso-run+topic:kiso-connector
```

## Running

Kiso manages connectors as daemon processes:

```bash
kiso connector discord run             # start as daemon
kiso connector discord stop            # stop the daemon
kiso connector discord status          # check if running
```

Kiso spawns the connector as a background process, tracks its PID, and restarts it on crash. Logs go to `~/.kiso/connectors/{name}/connector.log`.

Under the hood:

```bash
# start
.venv/bin/python ~/.kiso/connectors/discord/run.py &

# stop
kill <pid>
```
