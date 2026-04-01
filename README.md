# Kiso

Minimal agent bot. Single HTTP endpoint, per-session task queue, LLM via OpenRouter (or any OpenAI-compatible provider). One command to install, one config file to manage.

KISO (基礎) = foundation in Japanese.

## Why Kiso

### Structural safety, not prompt safety

Most agent bots rely on prompt instructions to stay safe. Kiso enforces safety through code:

- **OS-level sandbox.** User commands run as a restricted Linux user — not filtered in application code, but isolated by the kernel. Four-layer prompt injection defense (paraphrasing → random boundary fencing → prompt hierarchy → structured output).
- **Review gates.** A reviewer LLM evaluates every exec, tool, and search task before the next one runs. Errors don't cascade.
- **No silent installs.** Tool/package installs require explicit user approval — enforced by plan validation, not prompts.
- **Safety rules.** Admin-defined constraints (`kiso rules add "never delete /data"`) are always in the planner context. The reviewer blocks violations.

### Multi-tool orchestration

Kiso chains tools across plans in a single conversation. A real example:

```
User: "Go to example.com, screenshot it, extract the text, write a word counter, run it"

Plan 1: browser → navigate + screenshot → screenshot.png
Plan 2: ocr → extract text from screenshot.png → raw text
Plan 3: aider → write word_count.py from spec
Plan 4: exec → run script → msg → deliver results
```

Each tool runs in its own venv. Files carry across plans automatically. The planner discovers what's available in the registry, proposes installs, and routes file types to the right tool.

### Production-ready features

- **Parallel execution.** Independent tasks run via asyncio.gather. A 5-source research job takes the time of one source.
- **Smart replanning.** Shrinking task budget on retries, circular detection, reviewer hints. The bot recovers from failures without spiraling.
- **In-flight triage.** Stop commands caught in milliseconds. Updates modify the running plan. Conflicts replace it. Independent requests queue with an ack.
- **Cron scheduling.** `kiso cron add "0 9 * * *" "check prices" --session marketing` — the bot wakes, executes, reports, sleeps.
- **Knowledge system.** Curated facts with entities, tags, confidence, decay, and consolidation. A curator LLM filters noise. Import/export markdown.
- **Cross-session projects.** Facts and behaviors scoped to projects. Team isolation built in.
- **Presets.** `kiso preset install performance-marketer` bundles tools, knowledge, and behavioral rules into a persona.
- **Ephemeral secrets.** Credentials shared in conversation stay in-memory only — never touch disk.
- **Execution hooks.** Pre/post exec hooks for custom validation, audit logging, or blocking commands.
- **Knowledge consolidation (dream).** Periodic fact review and deduplication — runs on a configurable schedule.
- **kiso config command.** Change settings at runtime with hot reload — no manual config editing needed.
- **Bot persona.** Configurable messenger personality via `bot_persona` setting.
- **Cost display.** Per-message cost estimate shown in the CLI after each plan completes.

## Quick Start

```bash
# Install (one command)
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)

# Chat
kiso

# Or send a single message
kiso msg "find all Python files larger than 1MB and summarize what they do"

# Install a tool
kiso tool install browser

# Schedule a recurring task
kiso cron add "0 9 * * *" "check competitor prices" --session marketing
```

## Installation

**Prerequisites:** Docker with Compose v2, git, an OpenRouter API key.

### One-liner

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
```

### From cloned repo

```bash
git clone https://github.com/kiso-run/core.git && cd core && ./install.sh
```

### Non-interactive

```bash
./install.sh --user marco --api-key sk-or-v1-...
```

The installer builds the Docker image, writes config to `~/.kiso/`, starts the container, and installs the `kiso` CLI to `~/.local/bin/`. Use `--reset` to factory-reset (keeps config/key, wipes sessions/knowledge/tools).

### After installation

- Config: `~/.kiso/config.toml` — see [config.md](docs/config.md)
- API key: `~/.kiso/.env`
- Role prompts: `~/.kiso/roles/*.md` — optional overrides. See [llm-roles.md](docs/llm-roles.md)

## Usage

```bash
# Core
kiso                      # interactive chat
kiso msg "hello"          # single message
kiso cancel               # cancel active job
kiso sessions             # list sessions
kiso session create dev   # named session

# Plugins
kiso tool install search  # install a tool
kiso plugin list          # list installed plugins
kiso preset install performance-marketer

# Knowledge
kiso knowledge add "Uses Flask" --entity my-app --tags python
kiso knowledge search "database"
kiso knowledge import context.md

# Behaviors & Rules
kiso behavior add "always use metrics"
kiso rules add "never delete /data"

# Scheduling
kiso cron add "0 9 * * *" "check prices" --session marketing

# Projects
kiso project create my-app --description "Main SaaS product"
kiso project bind dev my-app

# System
kiso env set KEY VALUE    # deploy secret
kiso status               # health check
```

## Project Structure

```
kiso/                               # installable python package
├── main.py                         # FastAPI, /msg, /status, /pub, /health
├── brain.py                        # planner + reviewer + curator
├── llm.py                          # LLM client (OpenRouter / OpenAI-compatible)
├── worker/                         # per-session asyncio worker
│   ├── loop.py                     # message processing, plan orchestration
│   ├── exec.py / tool.py / search.py  # task handlers
│   └── utils.py                    # subprocess, workspace management
├── store.py                        # SQLite: sessions, plans, tasks, facts
├── tools.py / connectors.py        # plugin discovery
└── config.py                       # config loading and validation

~/.kiso/instances/{name}/           # per-instance data
├── config.toml                     # providers, models, settings
├── store.db                        # SQLite database
├── tools/{name}/                   # installed tools (git clone + venv)
├── connectors/{name}/              # platform bridges
└── sessions/{sid}/                 # per-session workspace + pub/ files
```

## Packages

Tools and connectors use the same packaging format: `kiso.toml` manifest + `pyproject.toml` + `run.py`. Each runs in its own isolated venv. See [tools.md](docs/tools.md) and [connectors.md](docs/connectors.md).

Official packages: `kiso-run/tool-{name}`, `kiso-run/connector-{name}`. Unofficial: any git repo with a valid `kiso.toml`.

## Design Documents

- [flow.md](docs/flow.md) — Full message lifecycle
- [config.md](docs/config.md) — Configuration, providers, tokens
- [tools.md](docs/tools.md) — Tool system (subprocess, isolated venv)
- [connectors.md](docs/connectors.md) — Platform bridges
- [api.md](docs/api.md) — API endpoints
- [cli.md](docs/cli.md) — Terminal client and management commands
- [security.md](docs/security.md) — Authentication, permissions, secrets, prompt injection defense
- [llm-roles.md](docs/llm-roles.md) — LLM roles, prompts, context
- [database.md](docs/database.md) — Database schema
- [docker.md](docs/docker.md) — Docker setup, volumes, pre-installing packages
- [safety.md](docs/safety.md) — Safety rules, job cancellation, in-flight handling
- [hooks.md](docs/hooks.md) — Execution hooks (pre/post exec validation)
- [audit.md](docs/audit.md) — Audit trail (JSONL logs, secret masking)
- [testing.md](docs/testing.md) — Testing strategy, fixtures, coverage
- [logging.md](docs/logging.md) — Logs
