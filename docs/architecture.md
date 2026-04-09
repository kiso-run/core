# Architecture

## What Kiso Is

Kiso is a general-purpose agent runtime for real execution.

It is designed around a simple idea: agent systems become reliable when the
important boundaries are enforced by runtime structure, not only by prompt
instructions.

That means Kiso is not just:

- a chatbot with tool calls
- a shell wrapper around an LLM
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
  - tools / policies
        |
        v
      planner
        |
        v
   TaskContract[]
        |
        v
      worker
  - exec / tool / search / msg
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

- a tool edited a file but the next step tested the wrong path
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
- available tools
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
- tool name and structured args
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

`exec`, `tool`, and `search` tasks do not automatically count as success just
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

### Tools and Connectors

Kiso stays general-purpose by treating tools and connectors as plugins.

Tools extend execution abilities:

- browser automation
- OCR
- code editing
- external API calls
- domain-specific operators

Connectors extend intake and delivery:

- CLI
- Discord
- email
- anything else that can create sessions and receive outputs

Each plugin runs in its own isolated environment, which keeps the core smaller
and reduces coupling.

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
- plugin-based extension through tools and connectors
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
catalogue (currently 12 roles: classifier, inflight-classifier, briefer,
planner, reviewer, worker, messenger, searcher, summarizer, curator,
consolidator, paraphraser) is defined in `kiso/brain/roles_registry.py` —
the single source of truth for role metadata. Default models are derived
from `kiso/config.py:_MODEL_METADATA` at access time, so the registry and
the config cannot drift. The user-facing entry point is `kiso roles`
(see [cli.md — Role Management](cli.md#role-management)).

Splitting the work this way is what makes kiso debuggable: every LLM call has
exactly one role label, one prompt file, one model, and one output schema. A
failure can be replayed in isolation without re-running the entire loop.

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

## Where To Go Next

- [README.md](/home/ymx1zq/Documents/software/kiso-run/core/README.md) for the product overview
- [flow.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/flow.md) for the full execution lifecycle
- [database.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/database.md) for persistence and runtime-state mapping
- [llm-roles.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/llm-roles.md) for role-specific prompt responsibilities
- [testing.md](/home/ymx1zq/Documents/software/kiso-run/core/docs/testing.md) for the test strategy and confidence model
