# Kiso

Kiso is a general-purpose agent runtime that can actually do work without
collapsing into prompt soup.

It plans, executes, reviews, replans, remembers, and reports through a runtime
that is built around explicit contracts, isolated execution, and durable state.
You can use it as a CLI assistant, wire it into connectors, give it wrappers, and
let it run real workflows across files, shells, APIs, and multi-step recovery.

Most agent bots are still "one big prompt plus vibes". Kiso is not.

`KISO` (基礎) means `foundation` in Japanese. That is the point: a strong base
for building real agentic behavior, not a demo loop.

## Why Kiso Wins

### It is structurally reliable, not just prompt-guided

Kiso does not rely on a giant system prompt to keep the runtime coherent.
Instead it uses:

- explicit planning and review phases
- normalized `TaskContract` and `TaskResult` runtime objects
- file and artifact identity across steps
- classified failure modes instead of generic "something went wrong"
- deterministic recovery boundaries instead of blind retry loops

This matters because the hard problems in agent systems are usually not raw
generation quality. They are handoff problems:

- the planner thinks a file exists, but the executor cannot find it
- a wrapper edits something, but the next step tests the wrong path
- the model retries the same broken strategy with different wording
- memory gets stuffed into prompts with no distinction between facts and recent execution state

Kiso is built to reduce those failures at the runtime boundary, not only at the
prompt boundary.

### It executes in the real world

Kiso is not a chat wrapper that stops at advice. It can:

- run shell commands in isolated workspaces
- install and call wrappers packaged as plugins
- search, inspect files, produce reports, and publish artifacts
- continue across multiple plans when a workflow needs investigation first
- report progress and final outputs back through CLI or connectors

The system is designed for real execution, not just "here is what I would do".

### It stays general-purpose

Kiso is not locked to coding, research, support, or one vertical workflow.
It is a runtime for open-ended agent tasks with enough structure to stay sane.

That means:

- open-ended planner outputs
- wrappers and connectors as plugins
- persistent knowledge and behavior rules
- project/session scoping
- room for strict safety rules without turning the system into a brittle workflow engine

## What Kiso Does

At a high level, Kiso:

1. receives a user message through CLI or an API-backed connector
2. builds role-aware context from recent conversation, knowledge, rules, wrappers, and workspace state
3. asks the planner for a task graph
4. normalizes that plan into executable task contracts
5. runs tasks one by one, carrying forward structured results, file refs, artifact refs, and dependency links
6. reviews non-trivial execution steps before continuing
7. replans when the current strategy is wrong instead of pretending partial failure is success
8. delivers user-facing updates and final outputs
9. stores knowledge and execution traces so the next plan starts from a better state

If you want the full runtime walkthrough, start with [architecture.md](docs/architecture.md) and then go deeper into [flow.md](docs/flow.md).

## Why This Architecture Is Better Than A Simple Agent Shell

Most simple agent shells have the same pattern:

- take a message
- ask one model what to do
- maybe execute a command
- print the answer

That can work for toy tasks, but it breaks down under real orchestration.

Kiso is designed around the actual failure surfaces:

- execution needs isolation
- wrappers need contracts, not just best-effort JSON
- multi-step workflows need durable state
- replans need memory of what was already tried
- memory needs semantic knowledge separated from recent operational context
- user-facing messaging should not be mixed with internal execution steps

That is why Kiso can support workflows that are longer, messier, and more
recoverable than a single-turn coding agent loop.

## Core Capabilities

- Structured planning, execution, review, and replan loops
- Per-session workspaces with published artifacts and uploads directories
- Wrapper and connector plugins, each in its own isolated environment
- Runtime file/artifact identity and dependency-aware handoff
- Knowledge system with facts, entities, tags, confidence, decay, and curation
- Behavior rules and safety constraints carried into planning
- Ephemeral in-memory secrets that never need to hit disk
- Webhook and API delivery for connector-driven usage
- Cron scheduling and recurring automation
- Execution hooks, audit logs, and operational introspection

## Example Workflows

### Engineering and repo automation

```text
"Inspect this repo, find the slowest test module, propose a fix, patch it,
run the targeted tests, and summarize the tradeoffs."
```

Kiso can inspect the workspace, run shell commands, edit files through wrappers,
execute verification steps, and report exactly what changed.

### Research and artifact production

```text
"Search for the latest EU AI Act implementation guidance, compare three
sources, write a short internal brief, and publish it as a markdown file."
```

Kiso can search, collect evidence, structure outputs, and deliver both a user
message and a file artifact.

### Ops and recurring checks

```text
"Every weekday at 9:00, check competitor pricing, flag meaningful changes,
and send the summary to the marketing session."
```

Kiso can schedule recurring work, keep session-scoped context, and reuse the
same runtime for operational automation instead of one-off chats.

### Multi-step investigation before action

```text
"Figure out why the staging deploy is failing, inspect logs, check config
differences, and only then propose or apply the smallest safe fix."
```

Kiso can investigate first, replan with the evidence it found, and avoid
pretending that the first guessed strategy was correct.

## When To Use Kiso

Use Kiso when you need an agent that must:

- carry work across multiple execution and review steps
- touch real files, commands, wrappers, or connectors
- recover from partial failure without losing the thread
- keep durable knowledge and session/project context
- stay general-purpose instead of being locked to one workflow template

## When Not To Use Kiso

Do not use Kiso when you only need:

- a simple chat assistant with no execution
- a single hard-coded workflow with no open-ended planning
- a tiny embedded helper where Docker, sessions, and runtime state would be overkill
- deterministic business logic that should just be plain application code

## Quick Start

```bash
# Install
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)

# Open an interactive session
kiso

# Or send a single message
kiso msg "find all Python files larger than 1MB and summarize what they do"

# Install a wrapper
kiso wrapper install browser

# Create a recurring task
kiso cron add "0 9 * * *" "check competitor prices" --session marketing
```

## Installation

**Prerequisites:** Docker with Compose v2, git, and an OpenRouter API key.

### One-liner

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
```

### From a clone

```bash
git clone https://github.com/kiso-run/core.git
cd core
./install.sh
```

### Non-interactive

```bash
./install.sh --user marco --api-key sk-or-v1-...
```

The installer builds the Docker image, writes config to `~/.kiso/`, starts the
container, and installs the `kiso` CLI to `~/.local/bin/`.

## Example Commands

```bash
# Core interaction
kiso
kiso msg "hello"
kiso cancel
kiso status

# Sessions
kiso sessions
kiso session create dev

# Plugins
kiso wrapper install search
kiso plugin list
kiso preset install performance-marketer

# Knowledge and rules
kiso knowledge add "Uses Flask" --entity my-app --tags python
kiso knowledge search "database"
kiso behavior add "always use metrics"
kiso rules add "never delete /data"

# Scheduling
kiso cron add "0 9 * * *" "check prices" --session marketing
```

## Runtime Shape

```text
message -> planner -> task contracts -> worker execution -> review/replan -> user delivery
                   \-> memory + wrappers + workspace state ->/
```

The key point is that Kiso does not treat text as the only handoff boundary.
The runtime carries forward structured contracts and results so later phases can
reason about what actually happened, not just what a previous prompt said.

## Docs Map

- [architecture.md](docs/architecture.md) — What Kiso is, why the architecture works, and how the core pieces fit together
- [flow.md](docs/flow.md) — Full message lifecycle and runtime sequencing
- [config.md](docs/config.md) — Configuration, providers, models, tokens
- [wrappers.md](docs/wrappers.md) — Wrapper system and packaging
- [connectors.md](docs/connectors.md) — Platform bridges and connector model
- [api.md](docs/api.md) — HTTP API
- [cli.md](docs/cli.md) — Terminal client and management commands
- [security.md](docs/security.md) — Authentication, permissions, secrets, prompt-injection defense
- [llm-roles.md](docs/llm-roles.md) — LLM roles, prompts, context assembly
- [database.md](docs/database.md) — Database schema and runtime-state mapping
- [docker.md](docs/docker.md) — Docker setup, volumes, packaging
- [hooks.md](docs/hooks.md) — Execution hooks
- [audit.md](docs/audit.md) — Audit trail and logging
- [testing.md](docs/testing.md) — Test strategy and confidence model

## Project Structure

```text
kiso/                               # installable python package
├── main.py                         # FastAPI app, lifespan, boot
├── api/                            # REST API routes
│   ├── runtime.py                  # /msg, /status endpoints
│   ├── sessions.py                 # session management
│   ├── knowledge.py                # facts, entities, tags
│   ├── admin.py                    # admin operations
│   └── projects.py                 # multi-project support
├── brain/                          # LLM role orchestration
│   ├── planner.py                  # plan generation + deterministic validation
│   ├── reviewer.py                 # task output review
│   ├── curator.py                  # knowledge curation + entity assignment
│   ├── text_roles.py               # messenger, summarizer, exec translator
│   └── common.py                   # shared LLM call infra, schemas, retry
├── worker/                         # per-session execution runtime
│   ├── loop.py                     # message processing, replan loop
│   ├── message_flow.py             # messenger + curator + summarizer flow
│   ├── review_flow.py              # review step orchestration
│   ├── exec.py / wrapper.py / search.py  # task handlers
│   ├── replan.py                   # replan context building
│   └── state.py / utils.py         # execution state, workspace helpers
├── store/                          # SQLite persistence
│   ├── knowledge.py                # facts, entities, tags
│   ├── plans.py / sessions.py      # plans, tasks, sessions
│   └── shared.py / setup.py        # queries, schema migrations
├── llm.py                          # LLM client (SSE streaming, retry)
├── config.py                       # config loading and validation
├── sysenv.py                       # system environment detection
├── wrappers.py / connectors.py        # plugin discovery and loading
└── roles/*.md                      # LLM role prompts (planner, reviewer, etc.)

~/.kiso/instances/{name}/           # per-instance state
├── config.toml
├── store.db
├── wrappers/{name}/
├── connectors/{name}/
└── sessions/{sid}/                 # workspace, pub/, uploads/
```

## Package Model

Wrappers and connectors use the same packaging shape: `kiso.toml` manifest,
`pyproject.toml`, and `run.py`. Each runs in its own isolated environment.

Official packages follow the `kiso-run/wrapper-{name}` and
`kiso-run/connector-{name}` naming pattern, but any git repo with a valid
`kiso.toml` can participate.
