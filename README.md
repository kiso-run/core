# Kiso

Minimal agent bot. Single HTTP endpoint, per-session task queue, LLM via OpenRouter (or any OpenAI-compatible provider).

KISO (基礎) = foundation in Japanese.

## Philosophy

Kiso installs and configures in one command. It's built to be trusted with real users running real tasks.

**Security designed in, not bolted on.** Most bots protect the inbound channel. Kiso protects execution: OS-level sandbox (real Linux users, not path filtering), four-layer prompt injection defense (paraphrasing → random boundary fencing → prompt hierarchy → structured output), ephemeral secrets that never touch disk, SSRF protection on webhooks.

**Real multi-user enforcement.** Admin and user roles are enforced at the OS level — user commands literally run as a restricted Linux user with scoped permissions on their workspace. Not filtered in application code.

**Review gates on every risky step.** A reviewer evaluates each exec, tool, and search task before the next one runs. Automatic and non-blocking, but structurally sound — errors don't cascade.

**Runtime kill switch.** Any running job can be cancelled instantly — via CLI (`kiso cancel`), REST API (`POST /sessions/{sid}/cancel`), or programmatically from a wrapper. No session destruction required, no process kill. The bot confirms what was completed, what was cancelled.

**In-flight message triage.** New messages arriving during an active job aren't blindly queued. A fast-path catches stop commands ("ferma", "STOP", "cancel") in milliseconds without LLM calls. Everything else is classified: updates modify the running plan, conflicts replace it, independent requests wait their turn with an immediate ack.

**No silent installs.** If a task needs a tool, connector, or package that isn't installed, the bot asks first and offers alternatives. The install only happens after the user confirms — enforced structurally by plan validation, not just by prompt instructions.

**Safety rules.** Persistent, admin-defined constraints (`kiso rules add "never delete /data"`) that are always injected into the planner — not gated by the briefer, not subject to decay or compression. The reviewer flags violations as stuck, blocking execution.

**Knowledge that doesn't rot.** Curated facts (user/project/tool/general) with confidence scores, decay, consolidation, and session scoping. A curator evaluates learnings before promoting them. Memory stays signal, not noise.

**Knowledge you control.** Add facts directly (`kiso knowledge add "..." --entity my-app --tags python,backend`), import from markdown (`kiso knowledge import context.md`), export for backup or migration. Structured entities, tags, FTS5 search, confidence scoring, and automatic decay keep the knowledge base sharp.

**Parallel execution.** Independent tasks run simultaneously — multiple searches, data fetches, or tool calls execute via asyncio.gather while dependent tasks wait their turn. A 5-source research job takes the time of one source, not five.

**Cross-session projects.** Facts, learnings, and behaviors belong to projects — not to the whole instance. Team A's architecture decisions don't pollute Team B's context. Member and viewer roles control who can act vs. who can observe.

**Behavioral guidelines.** Admin-defined preferences (`kiso behavior add "always use concrete metrics"`) that shape how the bot responds — injected into both planner and messenger. Softer than safety rules, but always present. The bot adapts to your working style.

**Presets.** Install a persona in one command: `kiso preset install performance-marketer`. Bundles tools, knowledge facts, and behavioral rules. Transform a generic instance into a specialized assistant — SEO analyst, backend developer, project manager.

**Cron scheduling.** Recurring tasks via standard cron expressions: `kiso cron add "0 9 * * *" "check competitor prices" --session marketing`. The bot wakes up, executes the full pipeline (plan → exec → review → report), and goes back to sleep.

**Smart replanning.** When a plan fails, the replanner gets a shrinking task budget — forcing focused recovery instead of sprawling retry-everything approaches. Circular replan detection catches infinite loops. The extend_replan mechanism lets the planner request extra attempts when it's close to solving.

**Fail loud.** Missing config → explicit error with the exact field name. No silent defaults, no undocumented fallbacks.

One config file, one database, git-installable tools and connectors each in their own isolated venv, runs in Docker.

## Project Structure

```
kiso/                               # installable python package
├── main.py                         # FastAPI, /msg, /status, /pub, /health
├── llm.py                          # LLM client, routes calls to configured providers
├── brain.py                        # planner + reviewer + curator
├── worker/                         # per-session asyncio worker package
│   ├── loop.py                     # message processing, plan orchestration
│   ├── exec.py / tool.py / search.py  # task handlers
│   └── utils.py                    # subprocess execution, workspace management
├── store.py                        # SQLite: sessions, messages, plans, tasks, facts, learnings
├── tools.py / connectors.py        # plugin discovery
├── security.py / auth.py           # permission enforcement
└── config.py                       # config loading and validation

~/.kiso/                            # user data (outside the repo)
├── instances.json                  # instance registry (name → ports)
└── instances/
    └── {name}/                     # per-instance data (see docker.md for full layout)
        ├── config.toml             # providers, tokens, models, settings
        ├── .env                    # deploy secrets (managed via `kiso env`)
        ├── store.db                # SQLite database
        ├── audit/                  # LLM call logs, task execution logs
        ├── roles/                  # system prompt overrides per LLM role
        ├── tools/                  # bot capabilities (git clone)
        │   └── {name}/
        │       ├── kiso.toml       # manifest: identity, args schema, deps
        │       ├── run.py          # entry point
        │       └── .venv/          # created by uv on install
        ├── connectors/             # platform bridges (git clone)
        │   └── {name}/
        │       ├── kiso.toml       # manifest: identity, env vars, deps
        │       ├── run.py          # entry point
        │       ├── config.toml     # connector config (gitignored)
        │       └── .venv/          # created by uv on install
        └── sessions/               # per-session workspaces
            └── {session_id}/
                ├── pub/            # published/downloadable files
                ├── uploads/        # files received from connectors or the user
                └── ...             # working files (exec cwd)
```

## Packages

Tools and connectors share the same base packaging format: `kiso.toml` manifest + `pyproject.toml` + `run.py` + optional `deps.sh`. Each `kiso.toml` declares its type, dependencies, and metadata. See [tools.md](docs/tools.md) and [connectors.md](docs/connectors.md).

Official packages live in the `kiso-run` GitHub org:
- Tools: `kiso-run/tool-{name}` (topic: `kiso-tool`)
- Connectors: `kiso-run/connector-{name}` (topic: `kiso-connector`)

Unofficial packages: any git repo with a valid `kiso.toml`.

## Docker

Kiso runs in Docker by default. The container comes with Python, `uv`, and common tools pre-installed. All user data lives in a single volume (`~/.kiso/`). Tools and connectors can be pre-installed in the Dockerfile or installed at runtime into the volume.

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
4. Asks for user, bot name, provider name, provider URL, API key, and per-role model selection (interactive) — writes `config.toml` and `.env`
5. Builds the Docker image, writes a self-contained `docker-compose.yml` in `~/.kiso/`, starts the container
6. Waits for healthcheck (`/health` endpoint on port 8333)
7. Installs the `kiso` wrapper script to `~/.local/bin/kiso` and shell completions

### Non-interactive mode

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh) \
  --user marco --api-key sk-or-v1-... \
  --base-url https://openrouter.ai/api/v1 --provider openrouter
```

When both `--user` and `--api-key` are set, model prompts are skipped (defaults used). `--base-url` and `--provider` are optional and default to `https://openrouter.ai/api/v1` and `openrouter`.

### Clean reinstall

To start completely fresh, remove the data directory and rerun:

```bash
# Stop and remove the container
docker rm -f kiso

# Remove all data (config, database, sessions, tools, connectors)
# Uses Docker because some files are root-owned from the container
docker run --rm -v ~/.kiso:/mnt/kiso alpine rm -rf /mnt/kiso/*
rm -rf ~/.kiso

# Reinstall
./install.sh
```

Or use the `--reset` flag to factory-reset during install (keeps config and API key, wipes sessions/knowledge/tools):

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
# Core
kiso                      # interactive chat
kiso msg "hello"          # send a message, get a response, exit
kiso cancel               # cancel the active job
kiso sessions             # list sessions
kiso session create dev   # create a named session

# Plugins
kiso tool install search  # install a tool
kiso tool install seo     # install a recipe tool
kiso plugin list          # list all installed plugins
kiso preset install performance-marketer  # install a persona bundle

# Knowledge
kiso knowledge add "Uses Flask" --entity my-app --tags python
kiso knowledge list --category project
kiso knowledge search "database"
kiso knowledge import context.md
kiso knowledge export --format md

# Behaviors & Rules
kiso behavior add "always use metrics"
kiso rules add "never delete /data"

# Scheduling
kiso cron add "0 9 * * *" "check competitor prices" --session marketing
kiso cron list
kiso cron disable <id>

# Projects
kiso project create my-app --description "Main SaaS product"
kiso project bind dev my-app
kiso project add-member bob --project my-app --role viewer

# System
kiso env set KEY VALUE    # set a deploy secret
kiso logs                 # follow container logs
kiso status               # check if running + healthy
```

Users can share credentials during conversation — the planner extracts them as **ephemeral secrets** (in-memory only, lost on worker shutdown). See [security.md — Secrets](docs/security.md#5-secrets).

## Design Documents

- [config.md](docs/config.md) — Configuration, providers, tokens
- [database.md](docs/database.md) — Database schema
- [llm-roles.md](docs/llm-roles.md) — LLM roles, their prompts, and what context each receives
- [flow.md](docs/flow.md) — Full message lifecycle
- [tools.md](docs/tools.md) — Tool system (subprocess, isolated venv)
- [connectors.md](docs/connectors.md) — Platform bridges
- [api.md](docs/api.md) — API endpoints
- [cli.md](docs/cli.md) — Terminal client and management commands
- [testing.md](docs/testing.md) — Testing strategy, fixtures, coverage
- [security.md](docs/security.md) — Authentication, permissions, secrets, prompt injection defense
- [docker.md](docs/docker.md) — Docker setup, volumes, pre-installing packages
- [audit.md](docs/audit.md) — Audit trail (JSONL logs, secret masking)
- [logging.md](docs/logging.md) — Logs
- [safety.md](docs/safety.md) — Safety rules, job cancellation, in-flight message handling
