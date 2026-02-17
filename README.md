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

```bash
git clone git@github.com:kiso-run/core.git
cd core
docker compose build
mkdir -p ~/.kiso ~/.kiso/roles
```

Create `~/.kiso/config.toml` (minimal — see [config.md](docs/config.md) for all options):

```toml
[tokens]
cli = "your-secret-token"          # generate with: openssl rand -hex 32

[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"

[users.marco]                      # your Linux username ($(whoami))
role = "admin"
```

Set deploy secrets (never in config files — see [security.md — Deploy Secrets](docs/security.md#deploy-secrets)):

```bash
docker compose up -d
docker exec -it kiso kiso env set KISO_OPENROUTER_API_KEY sk-or-v1-...
docker exec -it kiso kiso env reload
```

Create role prompts in `~/.kiso/roles/`: `planner.md`, `reviewer.md`, `worker.md`, `summarizer.md`, `curator.md`. The paraphraser reuses the summarizer — no separate file. See [llm-roles.md](docs/llm-roles.md).

Verify: `curl http://localhost:8333/health`

## Quickstart

```bash
# Send a message
curl -X POST http://localhost:8333/msg \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"session": "test", "user": "'"$(whoami)"'", "content": "hello"}'

# Check status
curl -H "Authorization: Bearer your-secret-token" \
  http://localhost:8333/status/test
```

Install skills and connectors via CLI (admin only — see [cli.md](docs/cli.md)):

```bash
docker exec -it kiso kiso skill install search
docker exec -it kiso kiso env set KISO_SKILL_SEARCH_API_KEY sk-...
docker exec -it kiso kiso env reload
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
- [security.md](docs/security.md) — Authentication, permissions, secrets, prompt injection defense
- [docker.md](docs/docker.md) — Docker setup, volumes, pre-installing packages
- [audit.md](docs/audit.md) — Audit trail (JSONL logs, secret masking)
- [logging.md](docs/logging.md) — Logs
