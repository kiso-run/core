# Architecture

## What Kiso Is

Kiso is a general-purpose agent runtime for real execution.

It is designed around a simple idea: agent systems become reliable when the
important boundaries are enforced by runtime structure, not only by prompt
instructions.

That means Kiso is not just:

- a chatbot with tool calls bolted on
- a shell script wrapping an LLM
- a fixed workflow engine

It sits in the middle:

- open-ended enough to stay general-purpose
- structured enough to survive multi-step execution, recovery, and memory

## The Core Model

Kiso runs a message through a sequence of explicit roles and runtime layers:

1. intake
2. context assembly
3. planning
4. task-contract normalization
5. execution
6. review
7. replan when needed
8. delivery
9. knowledge/memory updates

The crucial detail is that these phases do not communicate only through free
text. The runtime carries structured state between them.

## Architecture At A Glance

```text
user / connector / CLI
        |
        v
   message intake
        |
        v
  context assembly
  - recent conversation
  - workspace state
  - operational memory
  - semantic memory
  - wrappers / policies
        |
        v
      planner
        |
        v
   TaskContract[]
        |
        v
      worker
  - exec / mcp / msg / replan
  - file refs / artifact refs
  - dependency-aware handoff
        |
        +--------------------+
        |                    |
        v                    v
    reviewer              delivery
        |              CLI / webhook / API
        v
   replan or continue
        |
        v
  TaskResult history
        |
        v
 memory + audit updates
```

The shortest useful reading of Kiso is:

- planner decides what to try
- worker turns it into real execution
- reviewer decides whether the result is trustworthy enough to continue
- memory and runtime state make the next step smarter than the previous one

## Why This Matters

Most agent failures are handoff failures, not "the model had a bad sentence".

Typical failure classes:

- an MCP tool edited a file but the next step tested the wrong path
- a previous failure is described vaguely, so the planner retries the same idea
- user-facing output gets mixed into internal action tasks
- memory retrieval returns the wrong kind of context because execution state and semantic facts are blended together

Kiso tries to localize those problems with explicit runtime boundaries.

## Main Components

### Planner

The planner receives:

- the new user message
- recent message context
- session summary
- pending items
- semantic knowledge
- available wrappers
- workspace-visible files
- role and policy context

It returns a plan with a goal and a list of tasks.

The planner is still natural-language friendly, because open-ended planning is
part of what makes Kiso general-purpose. But the runtime does not execute raw
planner prose blindly.

### Task Contracts

Before execution, raw planner tasks are normalized into `TaskContract` objects.

These contracts carry the semantics the runtime actually depends on, including:

- task type and intent
- MCP server + method name and structured args
- delivery mode
- verification mode
- declared inputs
- expected outputs
- allowed repair scope
- inferred dependencies on prior files/artifacts

This is one of the main architectural upgrades in Kiso. It reduces the amount
of coordination that depends on prompt interpretation alone.

### Worker

The worker is the orchestrator for a session.

It:

- persists plan/task state
- executes each task in order
- manages plan outputs and runtime state
- applies narrow runtime repairs where a failure is structurally detectable
- enforces cancellation, policy checks, and delivery ordering

The worker is where Kiso stops being "just prompts" and becomes a runtime.

### Task Results

Each completed step is reconstructed into a canonical `TaskResult`.

`TaskResult` carries more than raw `output`:

- status
- stderr
- exit code
- reviewer summary
- retry hint
- failure class
- file refs
- artifact refs
- contract linkage

These results are then used by:

- downstream tasks
- dependency-aware handoff
- replans
- user-facing message composition

This is how Kiso reduces drift between "what happened" and "what the next phase
thinks happened".

### Reviewer

`exec` and `mcp` tasks do not automatically count as success just
because they produced output.

The reviewer decides whether the task:

- succeeded
- failed but should be retried locally
- failed in a way that needs a replan

This creates an explicit gate between execution and continuation. Without this
step, agent systems often continue on top of broken assumptions.

### Replanning

Replans are first-class, not an embarrassing fallback.

Kiso carries forward:

- what was tried
- what failed
- task results
- retry hints
- failure classes
- replan history

This gives the planner a better chance to change strategy instead of rewording
the same broken one.

### Memory

Kiso separates memory into two layers:

- operational memory
- semantic memory

Operational memory is about execution state:

- recent tasks
- plan outputs
- blockers
- delivery obligations
- recent summaries

Semantic memory is about durable knowledge:

- facts
- entities
- tags
- behaviors
- safety rules

This split matters because a runtime should not treat "what just happened in
this plan" the same as "what we know about the project".

### Skills, MCP, and Connectors

Kiso stays general-purpose by reasoning about **two orthogonal
extension primitives** plus a delivery layer:

- **Agent Skills** (`~/.kiso/skills/<name>/`) — packaged planner
  instructions on *how to think* about a class of problem. Skills
  are installed from URLs, optionally bundle scripts / references,
  and project role-scoped sections into planner / worker / reviewer
  / messenger prompts.
- **MCP servers** — the standardised protocol for *what to call*.
  Browser automation, OCR, code editing, external APIs, and
  domain-specific tools are all MCP servers in v0.10. Kiso is a
  consumer: it handles install-from-URL, per-session client pools,
  schema validation, trust policy, and recovery.
- **Connectors** — intake and delivery bridges: CLI, Discord,
  email, anything else that can create sessions and receive
  outputs. Connectors run under a supervisor-config model, not as
  installable plug-ins.

Skills and MCP servers are the two capability surfaces. Exec
remains the universal fallback for unstructured shell commands.

## End-to-End Flow

In simplified form:

1. a message arrives
2. Kiso assembles role-appropriate context
3. the planner produces tasks
4. the worker normalizes them into contracts
5. tasks execute with structured carry-forward state
6. reviewed tasks either pass or trigger replan
7. user-facing tasks deliver progress/results
8. memory and audit state are updated

For the full mechanics, see [flow.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/flow.md).

## When Kiso Is The Right Shape

Kiso is a strong fit when you need:

- open-ended planning with real execution
- multi-step recovery and replanning
- durable context across sessions or projects
- plug-in extension through skills, MCP servers, and connectors
- a runtime that remains general-purpose while still enforcing structure

Kiso is a weak fit when the problem is already:

- fully deterministic and better expressed as ordinary code
- a tiny single-purpose automation script
- pure chat with no execution or stateful workflow

## Design Principles

### Runtime over prompt

When a failure mode is structurally detectable, fix it in runtime logic instead
of relying on prompt nudges.

### General-purpose over workflow rigidity

Kiso should be able to handle arbitrary user goals. The architecture should add
reliability without collapsing into a narrow workflow engine.

### Explicit phase boundaries

Planning, execution, review, memory, and delivery should have distinct roles
and explicit contracts.

Each LLM step in kiso is a separately-named role with its own model, its own
prompt file in `~/.kiso/roles/`, and its own narrow output schema. The full
catalogue (currently 11 roles: classifier, briefer, planner, reviewer,
worker, messenger, summarizer, curator, consolidator, paraphraser,
sampler) is defined in `kiso/brain/roles_registry.py` —
the single source of truth for role metadata. Default models are derived
from `kiso/config.py:_MODEL_METADATA` at access time, so the registry and
the config cannot drift. The user-facing entry point is `kiso roles`
(see [cli.md — Role Management](cli.md#role-management)).

Splitting the work this way is what makes kiso debuggable: every LLM call has
exactly one role label, one prompt file, one model, and one output schema. A
failure can be replayed in isolation without re-running the entire loop.

The role loader is **self-healing** at runtime: if a user role file is missing
or empty (corruption, ephemeral volume, fresh install) the loader copies the
bundled default into the user dir, logs a warning, and continues. The user dir
remains the runtime source of truth — the bundle is the factory seed. This
mirrors what `_init_kiso_dirs()` does at server boot, just deferred to first
access. See [llm-roles.md — Self-healing role loader](llm-roles.md#self-healing-role-loader-m1296)
for the full contract.

Subprocess timeouts are also self-cleaning: every code path that wraps
`proc.communicate()` in a deadline goes through
`kiso._subprocess_utils.communicate_with_timeout`. On timeout the helper
SIGKILLs the **entire process group** (so a shell that forked children does
not leave orphans holding the parent's pipes), drains the communicate task
naturally so the StreamReaders unwind cleanly, and reaps the OS process via
`proc.wait()` before re-raising `TimeoutError`. Callers must create the
subprocess with `start_new_session=True` so the helper has a real process
group to target. Without it, hook and MCP-subprocess timeouts would
leave orphan subprocesses and leaked
`_UnixSubprocessTransport` instances that would later fire `__del__` on a
closed event loop.

### Durable state over ephemeral narration

Important state should be persisted or reconstructable. The system should not
depend on one prompt remembering the exact wording of the previous one.

### Safety through enforcement

Use real boundaries where possible:

- isolated execution
- policy validation
- review gates
- secret scrubbing
- audit trails

## Extension surfaces

Kiso has two extension primitives plus exec as the fallback:

- **Agent Skills** — reusable planner (+ worker / reviewer /
  messenger) instructions bundled as a standard
  [`agentskills.io`](https://agentskills.io) package. Installed
  from any URL via `kiso skill install --from-url <...>`.
- **MCP servers** — standard [MCP](https://modelcontextprotocol.io)
  capability providers. Installed from any URL via
  `kiso mcp install --from-url <...>`. Kiso is a consumer and
  does not publish or curate a registry.
- **Exec** — unstructured shell commands. Universal fallback for
  anything not covered by a skill or MCP method.

The boundary rule: if the capability is a way of *thinking* about a
problem, it is a skill. If it is a way of *calling* an external
capability, it is an MCP server. Otherwise, it is exec.

See [skills.md](skills.md) for skill authoring,
[mcp.md](mcp.md) for the MCP consumer guide, and
[extensibility.md](extensibility.md) for the full decision tree.

## Session Lifecycle

Each session owns:

- a row in the `sessions` table plus the per-session rows in
  `messages`, `plans`, `tasks`, `facts`, `learnings` (all keyed by
  `session` column), and
- a workspace directory at
  `~/.kiso/instances/<name>/sessions/<id>/` holding `pub/`,
  `uploads/`, and any files exec or MCP tasks produced.

Both sides round-trip through
`kiso/session_export.py::pack_session` /
`unpack_session`. `kiso session export <id>` produces a
deterministic `.tar.gz`; `kiso session import <file> [--as
<new_id>]` restores it on any other machine running the same (or
newer) schema version. This is the mechanism users rely on to
archive completed sessions, migrate between machines, or hand off
a debugging context.

## Where To Go Next

- [README.md](/home/ymx1zq/Documents/software/kiso-run/core/README.md) for the product overview
- [skills.md](skills.md) and [mcp.md](mcp.md) for the two v0.10 extension primitives
- [extensibility.md](extensibility.md) for the skill-vs-MCP-vs-exec decision tree
- [flow.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/flow.md) for the full execution lifecycle
- [database.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/database.md) for persistence and runtime-state mapping
- [llm-roles.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/llm-roles.md) for role-specific prompt responsibilities
- [testing.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/testing.md) for the test strategy and confidence model
