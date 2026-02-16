# Connectors

A connector bridges an external platform (Discord, Telegram, Slack, email, etc.) and kiso's API. Lives in `~/.kiso/connectors/{name}/`.

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

Same convention as skills: `KISO_CONNECTOR_{NAME}_{KEY}`. See [skills.md — Env Var Naming](skills.md#env-var-naming). These are deploy secrets — always in env vars, never in config files.

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

No secrets. Deploy secrets come from env vars declared in `kiso.toml`.

## What a Connector Does

1. **On startup**: registers sessions via `POST /sessions` with its webhook URL and description. Session IDs are chosen by the connector (opaque strings, e.g. `discord_dev`, `discord_dm_anna`). The connector decides the naming convention.
2. Connects to the platform (Discord WebSocket, Telegram polling, etc.)
3. Listens for messages
4. POSTs to kiso's `/msg` endpoint:
   - `session`: mapped from platform context (e.g. Discord channel → session name via `channel_map`)
   - `user`: the platform identity as-is (e.g. `"Marco#1234"`) — kiso resolves it to a Linux username via `aliases.{token_name}` in `config.toml` (see [security.md — Connector Aliases](security.md#connector-aliases))
   - `content`: message text
5. Receives webhook callbacks from kiso (at the URL set in `POST /sessions`)
6. Sends responses back to the platform
7. **Polling fallback**: if no webhook callback arrives within a reasonable timeout after sending a message, polls `GET /status/{session}?after={last_task_id}` to recover missed responses. This is a **protocol requirement** — connectors must implement it for reliability.

## deps.sh

Same as skills: optional, idempotent, installs system-level deps inside the container. See [skills.md — deps.sh](skills.md#depssh).

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

Unofficial repos trigger a confirmation prompt before install. Use `--no-deps` to skip `deps.sh`. See [security.md — Unofficial Package Warning](security.md#8-unofficial-package-warning) for the full warning text.

### Naming Convention

| Source | Name |
|---|---|
| Official (`kiso connector install discord`) | `discord` |
| Unofficial URL | `{domain}_{namespace}_{repo}` |
| Explicit `--name` | whatever you pass |

### Install Flow

Same as [skills.md — Install Flow](skills.md#install-flow) (with `.installing` marker, validation, deps, uv sync). One additional step: if `config.example.toml` exists and `config.toml` doesn't, copy it.

### Via the Agent (manual install)

A user can ask the agent to install a connector. The planner generates exec tasks replicating the CLI install flow (git clone → uv sync → deps.sh → copy config.example.toml) with a final `msg` listing next steps (set env vars, edit config, add aliases, run).

The agent cannot start the connector but can set env vars via exec tasks (`kiso env set ... && kiso env reload`) if the user is an admin.

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

Connectors run as daemon subprocesses managed by kiso:

```bash
kiso connector discord run             # start as daemon
kiso connector discord stop            # stop the daemon
kiso connector discord status          # check if running
```

Spawns as a background process, tracks PID, manages restarts. Logs: `~/.kiso/connectors/{name}/connector.log`.

Under the hood: `.venv/bin/python ~/.kiso/connectors/{name}/run.py &` with a management loop that monitors the PID and respawns with backoff.

### Restart Policy

Exponential backoff on crash (hardcoded thresholds). Stops after repeated failures. For custom restart policies, run the connector externally (systemd, supervisord).
