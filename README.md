# Kiso

Kiso is a general-purpose agent runtime that actually does work without
collapsing into prompt soup.

It plans, executes, reviews, replans, remembers, and reports through a
runtime built around explicit contracts, isolated execution, and durable
state. Use it as a CLI assistant, wire it into connectors, extend it with
**Agent Skills** and **MCP servers**, and let it run real workflows across
files, shells, APIs, and multi-step recovery.

Most agent bots are still "one big prompt plus vibes". Kiso is not.

`KISO` (基礎) means `foundation` in Japanese. That is the point: a strong
base for building real agentic behavior, not a demo loop.

## The two extension primitives

v0.10 converges on exactly two standards — anything else is exec:

- **[Agent Skills](https://agentskills.io)** — packaged planner
  instructions on how to *think* about a class of problem. Installed
  from any URL (`kiso skill install --from-url <github|zip|raw>`),
  optionally bundling scripts and references.
- **[MCP](https://modelcontextprotocol.io)** — the standard protocol
  for *what to call*. Kiso is a consumer: you install servers from
  URL (`kiso mcp install --from-url <pulsemcp|github|npm|pypi>`), the
  planner sees them, the runtime handles schemas, sessions, trust.

Exec is the universal fallback for "just run this shell command".

That is the whole extension surface. No Kiso-maintained registry of
skills or MCP servers — you bring the URL, Kiso wires the runtime
policy around it (trust store, per-session sandbox, schema
validation, recovery).

## Single-key onboarding

The default preset is designed so that **`OPENROUTER_API_KEY` is the
only mandatory secret** for full capability. Every kiso-maintained
MCP server (codegen, web search, transcription, OCR) routes through
OpenRouter. Servers that need no key at all (filesystem, fetch,
browser, docreader) work out of the box.

Collapsed onboarding from three required keys
(`OPENROUTER_API_KEY` + `GEMINI_API_KEY` + a search API key) to
**one**.

## Quick start

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
kiso doctor
kiso msg "list the files in the workspace"
```

The installer pulls kiso, writes a config to `~/.kiso/`, starts the
container, seeds the default MCP preset, and installs the `kiso` CLI
to `~/.local/bin/`. `kiso doctor` sanity-checks every layer (API key,
providers, MCP servers, sandbox). `kiso msg` sends your first message.

For a step-by-step walk-through (first skill, first MCP from URL,
first multi-step plan) see [docs/tutorial.md](docs/tutorial.md). For
the full docs tree, start at [docs/index.md](docs/index.md).

## What makes kiso different

### Structurally reliable, not just prompt-guided

Kiso does not rely on a giant system prompt to keep the runtime
coherent. Instead:

- explicit planning and review phases
- normalized `TaskContract` and `TaskResult` runtime objects
- file and artifact identity across steps
- classified failure modes instead of generic "something went wrong"
- deterministic recovery boundaries instead of blind retry loops

The hard problems in agent systems are usually not raw generation
quality. They are handoff problems:

- the planner thinks a file exists but the executor cannot find it
- one step edits something, the next step tests the wrong path
- the model retries the same broken strategy with different wording
- memory gets stuffed into prompts with no distinction between facts
  and recent execution state

Kiso reduces those failures at the **runtime boundary**, not only at
the prompt boundary.

### It executes in the real world

- shell commands in isolated per-session workspaces
- MCP tool calls with schema validation, per-session client pools,
  and recovery when a server dies
- skill-guided plans that compose exec + MCP + planner prompt modules
- durable plan / task / result state across replan cycles
- final outputs and file artifacts published back through CLI,
  webhook, or connectors

### It stays general-purpose

Not locked to coding, research, support, or one vertical workflow:

- open-ended planner outputs
- skills and MCP servers as plug-ins, each brought in from its own URL
- persistent knowledge and behavior rules
- project / session scoping
- room for strict safety rules without collapsing into a brittle
  workflow engine

## Core capabilities

- Structured planning, execution, review, and replan loops
- Per-session workspaces with published artifacts and uploads
- Skills + MCP servers as the two extension primitives
- Runtime file / artifact identity and dependency-aware handoff
- Knowledge system with facts, entities, tags, confidence, decay,
  and curation
- Behavior rules and safety constraints carried into planning
- Ephemeral in-memory secrets that never hit disk
- Webhook and API delivery for connector-driven usage
- Cron scheduling and recurring automation
- Execution hooks, audit logs, and operational introspection

## Example workflows

### Engineering and repo automation

```text
"Inspect this repo, find the slowest test module, propose a fix,
patch it, run the targeted tests, and summarize the tradeoffs."
```

Kiso walks the workspace, runs shell commands, asks an MCP codegen
server to edit files, executes verification steps, and reports
exactly what changed.

### Research and artifact production

```text
"Search for the latest EU AI Act implementation guidance, compare
three sources, write a short internal brief, and publish it as a
markdown file."
```

Kiso drives a search-capable MCP server, collects evidence, produces
structured outputs, and delivers both a user message and a file
artifact.

### Ops and recurring checks

```text
"Every weekday at 9:00, check competitor pricing, flag meaningful
changes, and send the summary to the marketing session."
```

Kiso schedules recurring work, keeps session-scoped context, and
reuses the same runtime for operational automation instead of
one-off chats.

### Multi-step investigation before action

```text
"Figure out why the staging deploy is failing, inspect logs, check
config differences, and only then propose or apply the smallest safe
fix."
```

Kiso investigates first, replans with the evidence it found, and
avoids pretending the first guessed strategy was correct.

## When to use kiso

When you need an agent that must:

- carry work across multiple execution and review steps
- touch real files, commands, MCP servers, or connectors
- recover from partial failure without losing the thread
- keep durable knowledge and session/project context
- stay general-purpose instead of being locked to one workflow
  template

## When not to use kiso

When you only need:

- a simple chat assistant with no execution
- a single hard-coded workflow with no open-ended planning
- a tiny embedded helper where Docker, sessions, and runtime state
  would be overkill
- deterministic business logic that should just be plain application
  code

## Installation

**Prerequisites:** Docker with Compose v2, git, and
`OPENROUTER_API_KEY`.

### One-liner (single key)

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
```

### From a clone

```bash
git clone https://github.com/kiso-run/core.git
cd core
OPENROUTER_API_KEY=sk-or-v1-... ./install.sh
```

### Non-interactive (scripted)

```bash
./install.sh --user marco --api-key sk-or-v1-...
```

The installer builds the Docker image, writes config to
`~/.kiso/instances/<name>/`, starts the container, seeds the default
MCP preset, and installs the `kiso` CLI to `~/.local/bin/`.

## Example commands

```bash
# Interaction
kiso                              # start chatting
kiso msg "hello"                  # send a single message
kiso status                       # show session state
kiso doctor                       # sanity-check every layer

# Extending
kiso skill install --from-url https://github.com/acme/kiso-skill-review
kiso skill list
kiso mcp install --from-url npm:@modelcontextprotocol/server-github
kiso mcp env github set GITHUB_PERSONAL_ACCESS_TOKEN <your-token>
kiso mcp test github                          # smoke-test a server

# Sessions and knowledge
kiso sessions
kiso session create dev
kiso knowledge add "Uses Flask" --entity my-app --tags python
kiso knowledge search "database"
kiso behavior add "always use metrics"
kiso rules add "never delete /data"

# Scheduling
kiso cron add "0 9 * * *" "check prices" --session marketing
```

## Runtime shape

```text
message -> planner -> task contracts -> worker execution -> review/replan -> user delivery
                   \-> memory + skills + MCP catalog + workspace state ->/
```

The key point is that Kiso does not treat text as the only handoff
boundary. The runtime carries forward structured contracts and
results so later phases can reason about what actually happened, not
just what a previous prompt said.

## Docs

Entry point: **[docs/index.md](docs/index.md)**.

Key files:

- [docs/architecture.md](docs/architecture.md) — runtime at a glance
- [docs/flow.md](docs/flow.md) — full message lifecycle
- [docs/skills.md](docs/skills.md) — Agent Skills authoring + install
- [docs/mcp.md](docs/mcp.md) — MCP consumer guide
- [docs/default-preset.md](docs/default-preset.md) — what ships in
  `kiso init --preset default`
- [docs/cli.md](docs/cli.md) — every `kiso` subcommand
- [docs/config.md](docs/config.md) — config + settings reference
- [docs/security.md](docs/security.md) — security model
- [docs/tutorial.md](docs/tutorial.md) — first skill + first MCP
  walk-through

## Project structure

```text
kiso/                               # installable python package
├── main.py                         # FastAPI app, lifespan, boot
├── api/                            # REST API routes
├── brain/                          # LLM role orchestration
├── worker/                         # per-session execution runtime
├── store/                          # SQLite persistence
├── mcp/                            # MCP client: stdio/http, pool,
│                                   # Resources/Prompts/Sampling
├── skill_loader.py                 # standard skill package loader
├── skill_runtime.py                # role-scoped skill projection
├── llm.py                          # LLM client (SSE streaming)
├── config.py                       # config loading and validation
├── sysenv.py                       # system environment detection
└── roles/*.md                      # LLM role prompts

~/.kiso/instances/{name}/           # per-instance state
├── config.toml
├── store.db
├── skills/{name}/                  # installed skills
├── mcp.json                        # installed MCP servers
└── sessions/{sid}/                 # workspace, pub/, uploads/
```
