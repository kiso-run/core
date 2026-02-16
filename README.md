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
├── brain.py                        # planner + reviewer
├── worker.py                       # consumes tasks from queue, one per session
├── store.py                        # SQLite: sessions, messages, tasks, facts, secrets, published
├── skills.py                       # skill discovery and loading
├── config.py                       # loads and validates ~/.kiso/config.toml
└── cli.py                          # interactive client + management commands

~/.kiso/                            # user data (outside the repo)
├── config.toml                     # providers, tokens, models, settings
├── store.db                        # SQLite database (6 tables)
├── server.log                      # server-level log
├── roles/                          # system prompt for each LLM role
│   ├── planner.md
│   ├── reviewer.md
│   ├── worker.md
│   └── summarizer.md
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

### 1. Clone and build

```bash
git clone git@github.com:kiso-run/core.git
cd core
docker compose build
```

### 2. Create config

```bash
mkdir -p ~/.kiso
```

Create `~/.kiso/config.toml`:

```toml
[tokens]
cli = "your-secret-token"          # generate with: openssl rand -hex 32

[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"

[users.marco]                      # your Linux username ($(whoami))
role = "admin"
```

Only whitelisted users get responses (unknown users' messages are saved for audit but not processed). See [config.md](docs/config.md) for user roles and skill permissions.

### 3. Set up secrets

Kiso never stores API keys in config files. Keys go in environment variables.

Create a `.env` file in the project root (gitignored):

```bash
# Provider API keys (required — at least one provider)
KISO_OPENROUTER_API_KEY=sk-or-v1-...

# Skill env vars (only if you install skills that need them)
# KISO_SKILL_SEARCH_API_KEY=...

# Connector env vars (only if you install connectors)
# KISO_CONNECTOR_DISCORD_BOT_TOKEN=...
```

Naming: providers use whatever `api_key_env` says; skills use `KISO_SKILL_{NAME}_{KEY}`; connectors use `KISO_CONNECTOR_{NAME}_{KEY}`. All declared in their respective `kiso.toml`.

### 4. Create role prompts

```bash
mkdir -p ~/.kiso/roles
```

Create one `.md` file per LLM role in `~/.kiso/roles/`: `planner.md`, `reviewer.md`, `worker.md`, `summarizer.md`. These are the system prompts for each role. See [llm-roles.md](docs/llm-roles.md) for what each role does.

### 5. Start

```bash
docker compose up -d
```

Kiso starts on port `8333`. Check it's running:

```bash
curl http://localhost:8333/health
```

## Quickstart

Once kiso is running:

```bash
# Send a message via curl
curl -X POST http://localhost:8333/msg \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"session": "test", "user": "'"$(whoami)"'", "content": "hello"}'

# Check status
curl -H "Authorization: Bearer your-secret-token" \
  http://localhost:8333/status/test
```

### Install a skill (admin only)

```bash
# Enter the container
docker exec -it kiso bash

# Install an official skill
kiso skill install search

# Set the skill's env var (add to .env, restart container)
# KISO_SKILL_SEARCH_API_KEY=...
```

### Install a connector (admin only)

```bash
docker exec -it kiso bash

# Install and configure
kiso connector install discord
# Edit ~/.kiso/connectors/discord/config.toml with your settings
# Set KISO_CONNECTOR_DISCORD_BOT_TOKEN in .env, restart container

# Start the connector
kiso connector discord run
```

### Adding secrets at runtime

Users can give the bot credentials during conversation (e.g. "here's my GitHub token: ghp_abc123"). The planner extracts these and stores them per-session in the database. Skills that declare `session_secrets` in their `kiso.toml` receive only the secrets they declared — nothing more.

These **session secrets** are different from **deploy secrets** (env vars, set once by admin). See [security.md — Secrets](docs/security.md#4-secrets) for the full comparison.

See [config.md](docs/config.md) for full configuration reference.

## Design Documents

- [config.md](docs/config.md) - Configuration, providers, tokens
- [database.md](docs/database.md) - Database schema (6 tables)
- [llm-roles.md](docs/llm-roles.md) - The 4 LLM roles, their prompts, and what context each receives
- [flow.md](docs/flow.md) - Full message lifecycle
- [skills.md](docs/skills.md) - Skill system (subprocess, isolated venv)
- [connectors.md](docs/connectors.md) - Platform bridges
- [api.md](docs/api.md) - API endpoints
- [cli.md](docs/cli.md) - Terminal client and management commands
- [security.md](docs/security.md) - Authentication, permissions, secrets
- [docker.md](docs/docker.md) - Docker setup, volumes, pre-installing packages
- [logging.md](docs/logging.md) - Logs
