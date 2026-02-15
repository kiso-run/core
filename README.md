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
├── main.py                         # FastAPI, /msg, /status, /pub endpoints
├── llm.py                          # LLM client, routes calls to configured providers
├── brain.py                        # planner + reviewer
├── worker.py                       # consumes tasks from queue, one per session
├── store.py                        # SQLite: sessions, messages, secrets, meta, published
├── skills.py                       # skill discovery and loading
├── config.py                       # loads and validates ~/.kiso/config.json
└── cli.py                          # interactive client + management commands

~/.kiso/                            # user data (outside the repo)
├── config.json                     # providers, models, settings
├── store.db                        # SQLite database (5 tables)
├── server.log                      # server-level log
├── roles/                          # system prompt for each LLM role
│   ├── planner.md
│   ├── reviewer.md
│   ├── worker.md
│   └── summarizer.md
├── skills/                         # bot capabilities (git clone)
│   └── {name}/
│       ├── kiso.toml               # manifest (required)
│       ├── pyproject.toml          # dependencies (uv-managed)
│       ├── run.py                  # entry point (required)
│       ├── SKILL.md                # docs for the planner (required)
│       ├── deps.sh                 # system deps installer (optional)
│       └── .venv/                  # created by uv on install
├── connectors/                     # platform bridges (git clone)
│   └── {name}/
│       ├── kiso.toml               # manifest (required)
│       ├── pyproject.toml          # dependencies (uv-managed)
│       ├── run.py                  # entry point (required)
│       ├── config.example.json     # example config (in repo)
│       ├── config.json             # actual config (gitignored, no secrets)
│       ├── deps.sh                 # system deps installer (optional)
│       └── .venv/                  # created by uv on install
└── sessions/                       # per-session data
    └── {session_id}/
        ├── session.log             # execution log
        ├── pub/                    # published/downloadable files
        └── ...                     # working files (exec cwd)
```

## Packages

Skills and connectors share the same package structure. Each has a `kiso.toml` manifest that declares its type, dependencies, and metadata. See [skills.md](docs/skills.md) and [connectors.md](docs/connectors.md).

Official packages live in the `kiso-run` GitHub org:
- Skills: `kiso-run/skill-{name}` (topic: `kiso-skill`)
- Connectors: `kiso-run/connector-{name}` (topic: `kiso-connector`)

Unofficial packages: any git repo with a valid `kiso.toml`.

## Docker

Kiso runs in Docker by default. The container comes with Python, `uv`, and common tools pre-installed. Skills and connectors install their system deps inside the container via `deps.sh`.

```
Dockerfile
├── python + uv preinstalled
├── kiso core
└── ~/.kiso/ mounted as volume (persistence)
```

## Minimal Setup

Set your API key as an environment variable:

```bash
export KISO_OPENROUTER_API_KEY="sk-or-..."
```

Everything else has sensible defaults. See [config.md](docs/config.md).

## Design Documents

- [config.md](docs/config.md) - Configuration, providers, defaults
- [database.md](docs/database.md) - Database schema (5 tables)
- [llm-roles.md](docs/llm-roles.md) - The 4 LLM roles, their prompts, and what context each receives
- [flow.md](docs/flow.md) - Full message lifecycle
- [skills.md](docs/skills.md) - Skill system (subprocess, isolated venv)
- [connectors.md](docs/connectors.md) - Platform bridges
- [api.md](docs/api.md) - API endpoints
- [cli.md](docs/cli.md) - Terminal client and management commands
- [security.md](docs/security.md) - Authentication, permissions, secrets
- [logging.md](docs/logging.md) - Logs
