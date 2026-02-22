# Kiso

Minimal agent bot. Single HTTP endpoint, per-session task queue, LLM via OpenRouter (or any OpenAI-compatible provider).

KISO (基礎) = foundation in Japanese.

## Philosophy

- **Minimal, barebone, essential** — the core does only what's strictly necessary; everything else you install separately
- One config file, one database file, easily customizable role behaviors
- No separate user management — authentication piggybacks on the Linux user system
- Skills and connectors installable via git, each isolated in its own venv (managed by `uv`)
- Runs in Docker — controlled environment, reproducible deps, no host pollution
- **No magic** — if something isn't configured, Kiso errors out. No implicit fallbacks, no guessing, no behavior that isn't explicitly described

## Project Structure

```
kiso/                               # installable python package
├── main.py                         # FastAPI, /msg, /status, /pub, /health
├── llm.py                          # LLM client, routes calls to configured providers
├── brain.py                        # planner + reviewer + curator
├── worker.py                       # consumes tasks from queue, one per session
├── store.py                        # SQLite: sessions, messages, plans, tasks, facts, learnings, pending, published
├── skills.py                       # skill discovery and loading
├── config.py                       # loads and validates ~/.kiso/config.toml
└── cli.py                          # interactive client + management commands

~/.kiso/                            # user data (outside the repo)
├── config.toml                     # providers, tokens, models, settings
├── .env                            # deploy secrets (managed via `kiso env`)
├── store.db                        # SQLite database
├── server.log                      # server-level log
├── audit/                          # LLM call logs, task execution logs
├── roles/                          # system prompt for each LLM role
│   ├── planner.md
│   ├── reviewer.md
│   ├── worker.md
│   ├── summarizer.md
│   └── curator.md
├── skills/                         # bot capabilities (git clone)
│   └── {name}/
│       ├── kiso.toml               # manifest: identity, args schema, deps
│       ├── pyproject.toml          # python dependencies (uv-managed)
│       ├── run.py                  # entry point
│       ├── deps.sh                 # system deps installer (optional)
│       └── .venv/                  # created by uv on install
├── connectors/                     # platform bridges (git clone)
│   └── {name}/
│       ├── kiso.toml               # manifest: identity, env vars, deps
│       ├── pyproject.toml          # python dependencies (uv-managed)
│       ├── run.py                  # entry point
│       ├── config.example.toml     # example config (in repo)
│       ├── config.toml             # actual config (gitignored, no secrets)
│       ├── deps.sh                 # system deps installer (optional)
│       └── .venv/                  # created by uv on install
└── sessions/                       # per-session data
    └── {session_id}/
        ├── session.log             # execution log
        ├── pub/                    # published/downloadable files
        └── ...                     # working files (exec cwd)
```

## Packages

Skills and connectors share the same base packaging format: `kiso.toml` manifest + `pyproject.toml` + `run.py` + optional `deps.sh`. Each `kiso.toml` declares its type, dependencies, and metadata. See [skills.md](docs/skills.md) and [connectors.md](docs/connectors.md).

Official packages live in the `kiso-run` GitHub org:
- Skills: `kiso-run/skill-{name}` (topic: `kiso-skill`)
- Connectors: `kiso-run/connector-{name}` (topic: `kiso-connector`)

Unofficial packages: any git repo with a valid `kiso.toml`.

## Docker

Kiso runs in Docker by default. The container comes with Python, `uv`, and common tools pre-installed. All user data lives in a single volume (`~/.kiso/`). Skills and connectors can be pre-installed in the Dockerfile or installed at runtime into the volume.

See [docker.md](docs/docker.md).

## Installation

### Prerequisites

- **Docker** with Docker Compose v2 (`docker compose`)
- **git**
- An **OpenRouter API key** (or any OpenAI-compatible provider key)

### Quick install (one-liner)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
```

This clones the repo to a temp dir, builds the Docker image, and cleans up after.

### Install from cloned repo

```bash
git clone https://github.com/kiso-run/core.git
cd core
./install.sh
```

When run from inside the repo, the installer uses it directly (no temp clone).

### What the installer does

1. Checks prerequisites (docker, docker compose, git)
2. If `~/.kiso/config.toml` or `~/.kiso/.env` already exist, asks whether to keep or overwrite
3. If rebuilding, cleans up root-owned files from previous Docker runs
4. Asks for username, bot name, and API key (interactive) — writes `config.toml` and `.env`
5. Builds the Docker image, writes a self-contained `docker-compose.yml` in `~/.kiso/`, starts the container
6. Waits for healthcheck (`/health` endpoint on port 8333)
7. Appends `[models]` section (all commented out) to `config.toml` with current model defaults
8. Installs the `kiso` wrapper script to `~/.local/bin/kiso` and shell completions

### Non-interactive mode

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh) \
  --user marco --api-key sk-or-v1-...
```

### Clean reinstall

To start completely fresh, remove the data directory and rerun:

```bash
# Stop and remove the container
docker rm -f kiso

# Remove all data (config, database, sessions, skills, connectors)
# Uses Docker because some files are root-owned from the container
docker run --rm -v ~/.kiso:/mnt/kiso alpine rm -rf /mnt/kiso/*
rm -rf ~/.kiso

# Reinstall
./install.sh
```

Or use the `--reset` flag to factory-reset during install (keeps config and API key, wipes sessions/knowledge/skills):

```bash
./install.sh --reset
```

### After installation

- Config: `~/.kiso/config.toml` — see [config.md](docs/config.md) for all options
- API key: `~/.kiso/.env` — managed separately from config
- Role prompts: `~/.kiso/roles/*.md` — optional overrides, sensible defaults built in. See [llm-roles.md](docs/llm-roles.md)
- `~/.local/bin/kiso` must be in your `PATH`

## Usage

```bash
kiso                      # interactive chat
kiso msg "hello"          # send a message, get a response, exit
kiso help                 # show all commands
kiso logs                 # follow container logs
kiso status               # check if running + healthy
kiso health               # hit /health endpoint
kiso restart              # restart the container
kiso down                 # stop
kiso up                   # start
kiso shell                # bash inside the container
kiso skill install search # install a skill
kiso env set KEY VALUE    # set a deploy secret
kiso env reload           # hot-reload secrets
```

Users can share credentials during conversation — the planner extracts them as **ephemeral secrets** (in-memory only, lost on worker shutdown). See [security.md — Secrets](docs/security.md#5-secrets).

## Design Documents

- [config.md](docs/config.md) — Configuration, providers, tokens
- [database.md](docs/database.md) — Database schema
- [llm-roles.md](docs/llm-roles.md) — LLM roles, their prompts, and what context each receives
- [flow.md](docs/flow.md) — Full message lifecycle
- [skills.md](docs/skills.md) — Skill system (subprocess, isolated venv)
- [connectors.md](docs/connectors.md) — Platform bridges
- [api.md](docs/api.md) — API endpoints
- [cli.md](docs/cli.md) — Terminal client and management commands
- [testing.md](docs/testing.md) — Testing strategy, fixtures, coverage
- [security.md](docs/security.md) — Authentication, permissions, secrets, prompt injection defense
- [docker.md](docs/docker.md) — Docker setup, volumes, pre-installing packages
- [audit.md](docs/audit.md) — Audit trail (JSONL logs, secret masking)
- [logging.md](docs/logging.md) — Logs
