# kiso — Vision

## What kiso is

A **general-purpose B2B knowledge-work** AI agent runtime that
prioritizes **robustness, determinism, auditability, and
efficiency** over leaderboard placement. The domain is the daily
work of PMI knowledge workers — marketers, project managers,
administrative, sales — not consumer chat and not coding-only.
Designed to be a foundation other projects can build on:
**`kiso-run/plugins`** (sibling repo: docreader, ocr, transcriber,
search) already runs in production inside cerase; **`kiso-run/core`**
(this repo) is the parallel runtime project sharing the same B2B
mission.

## Five questions

### 1. What does kiso do that no one else does?

- **Install consent as a state machine** (`needs_install` → user
  approves → exec literal). Authority is the state machine, not
  an LLM permission judge. Goose has an LLM judge; ForgeCode has
  allow-all by default. Kiso has a contract.
- **Cross-session knowledge** (F23): facts curated per-entity,
  retrievable across sessions and projects.
- **MCP + skills + connectors taxonomy**: one typed extension
  surface for capabilities, not a coding-only tool catalog.
- **General-purpose B2B default**: msg-only, kb_answer,
  awaits_input are first-class plan shapes. The agent is built to
  assist knowledge workers — email drafting, document Q&A, report
  writing, meeting summary, invoice processing, CRM enrichment,
  policy lookup — not just coders.

### 2. What does kiso do that others do better?

Loop efficiency on coding-shaped tasks. Goose, ForgeCode, Claude
Code run leaner LLM round-trips with parallel tool fan-out and
prompt caching. The gap is real and worth closing — by absorbing
their efficient patterns into kiso's structurally guarded loop,
**not** by adopting their architecture wholesale.

### 3. What does kiso NOT try to do?

- Compete for Terminal-Bench / SWE-bench leaderboard placement as
  an end in itself. Those benchmarks measure coding-shaped tasks;
  they are not kiso's domain. Relevant benchmarks are knowledge-
  work-shaped (document Q&A recall/precision, summary
  faithfulness, structured extraction F1, email tone/accuracy).
- Replace consumer chat clients (ChatGPT, Claude Desktop).
- Be coding-only. Coding is *one* possible workload among the B2B
  knowledge-work portfolio, not the primary one.

### 4. Why does kiso exist?

To be a **reusable runtime substrate for B2B knowledge-work
agents** that other projects can build on with confidence.

The kiso project today is two repos sharing one mission:
- **`kiso-run/plugins`** — Apache 2.0 MCP servers (docreader,
  ocr, transcriber, search) **already in production** inside
  cerase (`architecture.md` §9.3). Validated.
- **`kiso-run/core`** — this repo, the runtime. Today it runs
  standalone; cerase has chosen Goose as its agent runtime in
  v0.x M13 (`cerase/architecture.md` §3.1). `kiso-run/core` is
  the natural runtime candidate for cerase when its differentiators
  (state-machine consent, cross-session knowledge, structured
  always-on audit) become load-bearing — or when another consumer
  with comparable B2B-EU-compliance requirements adopts it.

The success criterion is third-party usability: documented APIs,
stable contracts, recoverable failure modes, and an audit trail
a downstream operator can trust without reading kiso source.

### 5. What are the non-negotiable invariants?

Four properties define kiso. Every architectural decision is
judged against them. A feature that improves one at the cost of
another is an explicit trade, documented in the devplan.

**I. Robustness.** Every known failure mode — tool failure,
context overflow, planner schema violation, install state
corruption, MCP unresponsive, model hallucination — has a
deterministic recovery path. `validate_plan`'s 22+ checks, the
circular replan detector, the install state machine, the
deterministic safety-pattern redaction are recovery
infrastructure, not accumulated patches. Kiso fails *predictably*,
not catastrophically.

**II. Determinism.** For any decision that can be made by rule,
the rule wins; LLM is the fallback, not the primary. Filtering,
ranking, routing, validation are rule-based. Tool output schemas
are forced (native function calling), not parsed from
JSON-in-text. Prompt caching is mandatory on static prefixes.
The same input yields the same output ≥95% of the time.

**III. Auditability.** For every turn, an operator can
reconstruct: which LLM calls were made and in what order; the
full input (system + user + tool schemas) and output of each
call; which guardrail or rule approved/rejected each decision and
why; which deterministic transitions fired. Audit is always-on,
structured, queryable — not opt-in via env var. The downstream
team running kiso in production must be able to answer "why did
the agent do X at turn N of session Y" from logs alone.

**IV. Efficiency.** Wall-clock latency and token consumption are
first-class constraints, not optimizations deferred to "later".
On the common turn, the LLM-call critical path is **two calls
(architect + editor pattern)**. Prompt caching is first-class.
Parallel tool dispatch is structural. Prompt size is budgeted and
size-tested in CI. An efficient kiso is a deployable kiso. An
inefficient kiso is a research project.

## Reference architectures kiso learns from

- **Aider** — minimal architect+editor split as the productive
  baseline. Two LLM calls per turn, no role bloat.
- **Goose** (block) — single-agent ReAct with parallel tool
  fan-out (`stream::select_all`), progressive context shedding
  (`[0, 10, 20, 50, 100]` middle-out), schema-forced output, MCP
  as primary tool surface.
- **ForgeCode** (antinomyhq) — deterministic safety rails
  (`max_tool_failure_per_turn`, `max_requests_per_turn`,
  signature-based doom-loop detector), lifecycle hooks
  architecture.

## What kiso does not copy

- ForgeCode's allow-all default policy. Kiso's state-machine
  consent is stronger.
- Goose's LLM-judge permission system. Same reason.
- Multi-agent fixed pipelines (CAMEL/AutoGen/MetaGPT family).
  Research-stage; production-grade systems are single-agent ReAct
  with deterministic guards and optional on-demand subagents.

## What this implies (succinct)

- The pipeline collapses on the **LLM-call axis** (today 4 calls
  on the common path: briefer + planner + worker translator +
  reviewer → target 2: architect + editor) and expands on the
  **guardrail axis** (validate_plan, install state machine,
  audit trail become more explicit, not less).
- Briefer and worker-translator as separate LLM calls disappear,
  folded into the architect with slim prompt + tool schemas and
  deterministic pre/post-filtering.
- The reviewer becomes end-of-turn, not per-task. Safety pattern
  enforcement stays.
- `planner.md` target ≤ 8 KB. Decision Tree branches become tool
  schemas with `validate_plan` as post-hoc structural check.
- Every milestone in v0.13+ is justified explicitly against the
  four invariants. No "while we're at it" features.

## How to use this document

When a milestone is proposed:
1. Which invariant does it serve? (robustness / determinism /
   auditability / efficiency)
2. Does it cost any of the other three? If yes, document the
   trade.
3. Does it violate "what kiso does not try to do"? If yes, reject
   or rewrite.

When a feature is removed:
1. Is it serving any invariant that another mechanism cannot?
2. If no, remove and document the rationale in the closing
   milestone.

This document is the contract. The devplan is the schedule.
