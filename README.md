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
├── store.py                        # SQLite: sessions, messages, tasks, facts, secrets, meta, published
├── skills.py                       # skill discovery and loading
├── config.py                       # loads and validates ~/.kiso/config.toml
└── cli.py                          # interactive client + management commands

~/.kiso/                            # user data (outside the repo)
├── config.toml                     # providers, tokens, models, settings
├── store.db                        # SQLite database (7 tables)
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

Skills and connectors share the same package structure. Each has a `kiso.toml` manifest that declares its type, dependencies, and metadata. See [skills.md](docs/skills.md) and [connectors.md](docs/connectors.md).

Official packages live in the `kiso-run` GitHub org:
- Skills: `kiso-run/skill-{name}` (topic: `kiso-skill`)
- Connectors: `kiso-run/connector-{name}` (topic: `kiso-connector`)

Unofficial packages: any git repo with a valid `kiso.toml`.

## Docker

Kiso runs in Docker by default. The container comes with Python, `uv`, and common tools pre-installed. All user data lives in a single volume (`~/.kiso/`). Skills and connectors can be pre-installed in the Dockerfile or installed at runtime into the volume.

See [docker.md](docs/docker.md).

## Minimal Setup

Create `~/.kiso/config.toml` with at least a token and a provider:

```toml
[tokens]
cli = "your-secret-token"

[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"
```

Set your API key:

```bash
export KISO_OPENROUTER_API_KEY="sk-or-..."
```

See [config.md](docs/config.md) for full reference.

## Design Documents

- [config.md](docs/config.md) - Configuration, providers, tokens
- [database.md](docs/database.md) - Database schema (7 tables)
- [llm-roles.md](docs/llm-roles.md) - The 4 LLM roles, their prompts, and what context each receives
- [flow.md](docs/flow.md) - Full message lifecycle
- [skills.md](docs/skills.md) - Skill system (subprocess, isolated venv)
- [connectors.md](docs/connectors.md) - Platform bridges
- [api.md](docs/api.md) - API endpoints
- [cli.md](docs/cli.md) - Terminal client and management commands
- [security.md](docs/security.md) - Authentication, permissions, secrets
- [docker.md](docs/docker.md) - Docker setup, volumes, pre-installing packages
- [logging.md](docs/logging.md) - Logs
