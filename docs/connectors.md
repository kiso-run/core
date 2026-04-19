# Connectors

A connector bridges an external platform (Discord, Telegram, Slack, email, etc.) and kiso's API. Lives in `~/.kiso/instances/{instance}/connectors/{name}/`.

## Structure

```
~/.kiso/instances/{instance}/connectors/
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

The manifest. Same base format as wrappers (`kiso.toml` + `pyproject.toml` + `run.py`), different type and sections.

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

Deploy secrets live in env vars named `KISO_CONNECTOR_{NAME}_{KEY}` — never in config files.

## config.toml

Structural, non-secret, deployment-specific configuration. The repo ships `config.example.toml`, the real `config.toml` is gitignored and created by the user post-install.

```toml
kiso_api = "http://localhost:8333"   # matches the instance's server port
session_prefix = "discord"
webhook_port = 9001                  # auto-assigned from the instance's connector range

[channel_map]
general = "discord-general"
dev = "discord-dev"
```

No secrets. Deploy secrets come from env vars declared in `kiso.toml`.

`webhook_port` is auto-assigned by the wrapper when the connector is installed (`kiso connector install`). The assigned port is from the instance's connector range (see [docker.md — Ports](docker.md#ports)) and is the same inside and outside the container.

## What a Connector Does

1. **On startup**: registers sessions via `POST /sessions` with its webhook URL and description. Session IDs are chosen by the connector (opaque strings, e.g. `discord_dev`, `discord_dm_anna`). The connector decides the naming convention.
2. Connects to the platform (Discord WebSocket, Telegram polling, etc.)
3. Listens for messages
4. POSTs to kiso's `/msg` endpoint:
   - `session`: mapped from platform context (e.g. Discord channel → session name via `channel_map`)
   - `user`: the platform identity as-is (e.g. `"Marco#1234"`) — kiso resolves it to a Linux user via `aliases.{token_name}` in `config.toml` (see [security.md — Connector Aliases](security.md#connector-aliases))
   - `content`: message text
5. Receives webhook callbacks from kiso (at the URL set in `POST /sessions`)
6. Sends responses back to the platform
7. **Polling fallback**: if no webhook callback arrives within a reasonable timeout after sending a message, polls `GET /status/{session}?after={last_task_id}` to recover missed responses. This is a **protocol requirement** — connectors must implement it for reliability.

## File Attachments

When a platform message includes file attachments (images, documents, audio, etc.), the connector should write them to the session's `uploads/` directory before — or alongside — posting the message:

```
~/.kiso/instances/{instance}/sessions/{session}/uploads/{filename}
```

The directory always exists (created automatically when the session workspace is initialised). The connector can derive the path from the `session` ID it chose on registration. Wrappers and exec tasks can then read from `uploads/` via the `workspace` input field.

No upload API exists yet — write directly to the filesystem (connectors run inside the container and share the same paths).

## deps.sh

Optional, idempotent shell script that installs system-level dependencies inside the container. Runs after `git clone`, before `uv sync`. Non-zero exit aborts the install.

## Installation

Only admins can install connectors.

### Via CLI

```bash
# official (resolves from kiso-run org)
kiso connector install discord
# → clones git@github.com:kiso-run/connector-discord.git
# → ~/.kiso/instances/{instance}/connectors/discord/

# unofficial (full git URL)
kiso connector install git@github.com:someone/my-connector.git
# → ~/.kiso/instances/{instance}/connectors/github-com_someone_my-connector/

# unofficial with custom name
kiso connector install git@github.com:someone/my-connector.git --name custom
# → ~/.kiso/instances/{instance}/connectors/custom/
```

### Unofficial Repo Warning

Unofficial repos trigger a confirmation prompt before install. Use `--no-deps` to skip `deps.sh`. See [security.md — Unofficial Package Warning](security.md#8-unofficial-package-warning) for the full warning text.

### Naming Convention

- `kiso connector install <name>` → resolves to `git@github.com:kiso-run/<name>-connector.git` (or the equivalent public URL) and installs to `~/.kiso/instances/{instance}/connectors/<name>/`.
- `kiso connector install <git-url>` → installs to `~/.kiso/instances/{instance}/connectors/<sanitized-url>/`.
- `kiso connector install <git-url> --name custom` → installs to `~/.kiso/instances/{instance}/connectors/custom/`.

### Install Flow

1. Validate target directory doesn't exist (or is a stale `.installing` marker).
2. `git clone <url> <target>.installing`.
3. Validate the repo has a `kiso.toml` with a `[kiso]` table and a plausible connector entry point.
4. Run `deps.sh` if present.
5. `uv sync` inside the target.
6. If `config.example.toml` exists and `config.toml` does not, copy it.
7. Rename `<target>.installing` → `<target>`.

Failures at any step abort and clean up the `.installing` directory.

### Via the Agent (manual install)

A user can ask the agent to install a connector. The planner generates exec tasks replicating the CLI install flow (git clone → uv sync → deps.sh → copy config.example.toml) with a final `msg` listing next steps (set env vars, edit config, add aliases, run).

The agent cannot start the connector but can set env vars via exec tasks (`kiso env set ... && kiso env reload`) if the user is an admin.

### Update / Remove / Search

```bash
kiso connector update discord          # git pull + deps.sh + uv sync
kiso connector update all
kiso connector remove discord
kiso connector list
kiso connector search [query]              # local search across installed connectors
```

## Running

Connectors run as daemon subprocesses managed by kiso:

```bash
kiso connector run discord             # start as daemon
kiso connector stop discord            # stop the daemon
kiso connector status discord          # check if running
```

Spawns as a background process, tracks PID, manages restarts. Logs: `~/.kiso/instances/{instance}/connectors/{name}/connector.log`.

Under the hood: `.venv/bin/python KISO_DIR/connectors/{name}/run.py &` with a management loop that monitors the PID and respawns with backoff.

### Restart Policy

Exponential backoff on crash (hardcoded thresholds). Stops after repeated failures. For custom restart policies, run the connector externally (systemd, supervisord).
