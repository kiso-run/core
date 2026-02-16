# Connectors

A connector bridges an external platform (Discord, Telegram, Slack, email, etc.) and kiso's API. Lives in `~/.kiso/connectors/{name}/`.

Connectors are long-running daemon processes managed by kiso inside the same container/environment.

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

The manifest. Same base format as skills (`kiso.toml` + `pyproject.toml` + `run.py`), different type and sections.

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

**These are deploy secrets — always in env vars, never in config files.** See [security.md](security.md).

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
   - `session`: mapped from platform context (e.g. Discord channel → session name via `channel_map`)
   - `user`: the platform identity (e.g. `"Marco#1234"`) — kiso resolves it to a Linux username via aliases (see [security.md](security.md))
   - `content`: message text
   - `webhook`: callback URL the connector exposes to receive responses
4. Receives webhook callbacks from kiso
5. Sends responses back to the platform

The connector does **not** need to know about Linux usernames. It sends the platform identity as-is. Kiso resolves it using the `aliases.{connector_name}` field in `config.toml`, where the connector name matches the token name.

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

### Via the Agent (manual install)

A user can ask the running agent to install a connector. The planner generates exec tasks that replicate the CLI install flow:

```
User: "install the Discord connector"

Planner generates:
1. exec: git clone git@github.com:kiso-run/connector-discord.git ~/.kiso/connectors/discord/
2. exec: cd ~/.kiso/connectors/discord && uv sync
3. exec: test -f ~/.kiso/connectors/discord/deps.sh && bash ~/.kiso/connectors/discord/deps.sh
4. exec: test -f ~/.kiso/connectors/discord/config.example.toml && cp ~/.kiso/connectors/discord/config.example.toml ~/.kiso/connectors/discord/config.toml
   (review: true, expect: "connector directory set up with venv and config")
5. msg: "Discord connector installed. Next steps:
        1. Set KISO_CONNECTOR_DISCORD_BOT_TOKEN in the container environment
        2. Edit ~/.kiso/connectors/discord/config.toml with your channel mapping
        3. Add aliases.discord to your users in config.toml
        4. Run: kiso connector discord run"
```

The agent cannot start the connector or set env vars (those require container restart or host-level access). It installs the files and tells the user what to do next.

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

Kiso manages connectors as daemon subprocesses:

```bash
kiso connector discord run             # start as daemon
kiso connector discord stop            # stop the daemon
kiso connector discord status          # check if running
```

Kiso spawns the connector as a background process, tracks its PID, and manages restarts automatically. Logs go to `~/.kiso/connectors/{name}/connector.log`.

Under the hood (simplified — the actual implementation includes a management loop that monitors the PID and respawns with backoff):

```bash
# start
.venv/bin/python ~/.kiso/connectors/discord/run.py &

# stop
kill <pid>
```

### Restart Policy

Exponential backoff on crash:

| Crash # | Wait before restart |
|---|---|
| 1 | 1s |
| 2 | 2s |
| 3 | 4s |
| 4 | 8s |
| ... | doubles each time |
| cap | 60s max |

- If the connector stays up for **60s without crashing**, the backoff counter resets to 0.
- After **10 consecutive crashes**, kiso stops the connector and logs: `connector {name} failed 10 times, stopped — run 'kiso connector {name} run' to retry`.
- These values are hardcoded. If you need custom restart policies, run the connector externally (systemd, supervisord, or a separate Docker container) and point it at kiso's API.
