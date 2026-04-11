# LLM Roles

Each LLM call has its own role. Each role has its own model (from `config.toml`), its own system prompt (from `~/.kiso/instances/{name}/roles/{role}.md` on the host, `KISO_DIR/roles/{role}.md` inside the container), and receives **only the context it needs**.

## Why kiso splits work into roles

Kiso never asks one big model to "do everything". Each step of the loop is a small role with a narrow contract:

- **Cheaper.** Routing tasks to a small fast model where possible (classifier, briefer, reviewer, summarizer, paraphraser, consolidator) keeps the expensive reasoning models reserved for the planner / worker / messenger steps that actually need them.
- **More accurate.** A role that only has to choose between 4 categories beats a role that has to plan, classify, review, and write the response in a single call. Smaller surface = fewer plausible failure modes.
- **Debuggable.** Every LLM call has a single role label, a single model, a single prompt file, and a single output schema. When something goes wrong you can read exactly which role failed and replay it in isolation.
- **User-extensible.** Role prompts live in `~/.kiso/roles/` after first boot. Editing one role does not affect the others. The bundled defaults can be restored at any time with `kiso roles reset NAME`.

## Discovering roles

The full list of roles, their default models, and their override status is exposed through the CLI:

```
$ kiso roles list
$ kiso roles show planner
$ kiso roles diff planner          # vs the bundled default
$ kiso roles reset planner         # restore the bundled default
```

Under the hood, every role has an entry in `kiso/brain/roles_registry.py`. The registry is the **single source of truth** for role metadata (name, description, model key, prompt filename, Python entry point) and the default model is derived from `kiso/config.py:_MODEL_METADATA` at access time, so the two cannot drift. Adding a new role is a two-line change: one entry in `_MODEL_METADATA`, one entry in the registry.

## Self-healing role loader (M1296)

Role files in `~/.kiso/roles/` are populated in two ways:

- **Eager seed at server startup.** `kiso.main._init_kiso_dirs()` runs on every FastAPI lifespan and additively copies the bundled defaults from `kiso/roles/` into `~/.kiso/roles/`. Existing non-empty user files are never overwritten — your customizations are safe across restarts.
- **Lazy self-heal at first access.** `kiso.brain.prompts._load_system_prompt()` checks the user file at every cache miss. If the file is missing or empty (deleted, truncated, never created, or part of an isolated test KISO_DIR), the loader copies the bundled default into the user dir atomically (`tmp + rename`), logs a `WARNING`, and reads the seeded file. The user dir remains the runtime source of truth — the bundled file is the **factory seed**, not an alternate read path. After self-heal the file lives in the user dir and subsequent reads are unaffected.

This two-layer scheme is what makes kiso resilient to role-file corruption at runtime: a file deleted by mistake or lost on an ephemeral container volume does not kill the server. The next LLM call self-heals it and continues. The trade-off: if a user customized a role and then deletes the file, self-heal silently restores the bundled default and the customization is lost (the warning log is the only signal). The alternative — hard-failing the server on the next LLM call — was the M1289 behavior and was strictly worse for hosted deployments where CLI access is not always available.

`FileNotFoundError` is raised by the loader **only** when both the user file and the bundled default are missing — i.e., the kiso installation itself is corrupted and reinstalling is the only fix.

## Context per Role

| Context piece | Classifier | Briefer | Planner | Reviewer | Worker | Messenger | Searcher | Summarizer | Curator | Paraphraser |
|---|---|---|---|---|---|---|---|---|---|---|
| User message (raw) | yes | - | - | - | - | - | - | - | - | - |
| Session summary | - | yes | briefer-filtered | - | - | briefer-filtered | - | yes (existing) | yes | - |
| Last N raw messages | - | yes | briefer-filtered | - | - | - | - | - | - | - |
| Recent msg outputs | - | yes | briefer-filtered | - | - | - | - | - | - | - |
| Paraphrased untrusted messages | - | yes | briefer-filtered | - | - | - | - | - | - | generates |
| New message | - | yes | yes | - | - | - | - | - | - | - |
| Facts (session-scoped; admin sees all) | - | yes | briefer-filtered | - | - | briefer-filtered | - | - | yes | - |
| Pending items (global + session) | - | yes | briefer-filtered | - | - | - | - | - | yes | - |
| Allowed wrapper summaries + args schemas | - | yes | briefer-filtered | - | - | - | - | - | - | - |
| Caller role (admin/user) | - | - | yes | - | - | - | - | - | - | - |
| System environment | - | yes | briefer-filtered | - | yes | - | - | - | - | - |
| Capability analysis | - | yes | briefer-filtered | - | - | - | - | - | - | - |
| Plan context (goal) | - | - | generates | yes (as background) | - | - | - | - | - | - |
| Preceding plan outputs (fenced) | - | yes (msg) | - | - | yes | briefer-filtered | yes | - | - | - |
| Current task detail | - | - | - | yes | yes | yes | yes | - | - | - |
| Current task expect | - | - | - | yes | - | - | - | - | - | - |
| Current task output (fenced) | - | - | - | yes | - | - | - | - | - | - |
| Original user request | - | - | - | yes | - | - | - | - | - | - |
| Messages to compress + their msg outputs | - | - | - | - | - | - | - | yes | - | - |
| Pending learnings | - | - | - | - | - | - | - | - | yes | - |
| Completed tasks + outputs (fenced) | - | - | replan only | - | - | - | - | - | - | - |
| Remaining tasks | - | - | replan only | - | - | - | - | - | - | - |
| Failure reason | - | - | replan only | - | - | - | - | - | - | - |
| Replan history | - | - | replan only | - | - | - | - | - | - | - |
| Confirmed facts | - | - | replan only | - | - | - | - | - | - | - |
| Raw untrusted messages (batch) | - | - | - | - | - | - | - | - | - | yes |

Key principle: the planner must put everything the messenger / worker needs into the task `detail` — neither will see the raw conversation (see [Why the messenger doesn't see the raw conversation](#why-the-messenger-doesnt-see-the-raw-conversation)). For `exec` tasks, `detail` is a natural-language description; the **worker** role (an LLM step) converts it to the actual shell command before execution (architect/editor pattern).

---

## Briefer

**When**: before the planner and messenger LLM calls, when `briefer_enabled` is true.

**Input**: the full context pool (summary, facts, recent messages, wrappers, system environment, capability analysis, plan outputs). **Output**: JSON selecting what each downstream consumer actually needs.

**Purpose**: context intelligence layer. Reads the full context pool (large, cheap model with 1M context) and produces a focused briefing for each consumer. Downstream models receive only relevant information, reducing token costs and improving accuracy.

**Model**: use a large-context, fast model — the briefer reads everything but produces small output. See [config.md](config.md) for the default.

### Output Schema

```json
{
  "modules": ["web", "data_flow"],
  "wrappers": ["browser: navigate, screenshot, text — browser automation"],
  "context": "User wants to visit gazzetta.it and get news. Browser is installed.",
  "output_indices": [2, 3],
  "relevant_tags": ["web", "browser"]
}
```

- **modules**: prompt modules to inject into the consumer's system prompt (from 11 available modules). Empty array when only core rules are needed (e.g., simple lookups).
- **wrappers**: relevant wrapper descriptions (copied verbatim from context pool). Filters the full wrapper list to only what's relevant.
- **context**: synthesized briefing replacing raw summary, facts, and history. Preserves specific values (names, versions, paths, URLs).
- **output_indices**: which plan_output entries to include (for messenger/worker). Filters irrelevant setup/install outputs.
- **relevant_tags**: fact tags for additional retrieval by semantic topic.

### Planner Modules

The planner prompt is modular — `<!-- MODULE: name -->` markers divide it into 11 optional sections plus a core. The briefer selects which modules to include:

| Module | When to include |
|--------|----------------|
| `planning_rules` | Non-trivial plans (2+ tasks) |
| `kiso_native` | User asks for capabilities that might need wrappers/connectors |
| `tools_rules` | Plan will use wrappers |
| `web` | URLs, websites, or web content mentioned |
| `data_flow` | Tasks produce large output for later tasks |
| `scripting` | Data processing or code generation needed |
| `replan` | Replan context only |
| `tool_recovery` | Wrapper is broken or has failed |
| `kiso_commands` | Kiso administration (wrapper/connector/env management) |
| `user_mgmt` | Users, roles, or aliases |
| `plugin_install` | Wrapper/connector not installed (also force-injected when no wrappers exist) |

### Fallback

When `briefer_enabled` is false or the briefer fails, the system falls back to keyword-based module selection and full context injection (original behavior).

**Prompt file**: `kiso/roles/briefer.md`

---

## Classifier

**When**: a new message arrives and `fast_path_enabled` is true.

**Input**: the user message (raw text), recent conversation snippet, known entity names — assembled by `build_classifier_messages` in `kiso/brain/common.py`. The system prompt is `kiso/roles/classifier.md`.

**Output**: `"category:Language"` where *category* is one of four values and *Language* is the full English name of the detected language (e.g. `chat_kb:Italian`, `investigate:English`).

**Categories**:

| Category | Meaning | What happens next |
|---|---|---|
| `plan` | The user wants an action — file ops, code, install, run, configure, manage wrappers/connectors/plugins. The user is issuing a command or asking for a change. | Full planner runs (see [Planner](#planner)). |
| `investigate` | The user wants to understand the live system state, diagnose an error, or get evidence about how something currently behaves. The answer requires running read-only commands or reading files but NOT changing them. Examples: *"why is X failing"*, *"show me the current config"*, isolated error reports. | Planner runs in `investigate=True` mode — a modular section is injected that constrains the plan to read-only tasks and a final diagnose `msg`. No mutations are proposed. |
| `chat_kb` | The user is asking about something stored in memory (entities, facts, previously discussed topics). | Pre-flight facts check (see below). If facts exist → fast path through messenger with the briefer-selected facts in context. If facts are empty → transparent fallback to `investigate`. |
| `chat` | Small talk: greetings, thanks, opinions, follow-up comments. No wrappers, no commands. | Fast path through messenger with the standard briefer pipeline. |

**Boundary rules** (built into the prompt):

- Imperative verb (*fix*, *install*, *restart*, *create*, *delete*, *run*) → `plan`
- Question or report without a fix verb (*why*, *what's wrong*, *show me*, *X is broken*) → `investigate`
- Mixed message (*"X is broken, fix it"*) → `plan` — the imperative wins
- *"What do you know about X"* → `chat_kb` (memory) vs *"What's the current X"* → `investigate` (live state)
- Self-referential knowledge (*"what do you know"*, *"your capabilities"*) → `chat_kb`
- General knowledge questions not about stored entities → `chat`
- When in doubt between `plan` and `investigate` → prefer `investigate` (preserves user autonomy)

**chat_kb empty-retrieval pre-flight**: when the classifier returns `chat_kb`, the worker dispatcher in `kiso/worker/loop.py:handle_message` runs a fast keyword-based facts query (`search_facts_scored` with `content.lower().split()[:10]`) BEFORE entering the chat_kb fast path. If the query returns zero facts, the worker:

1. Persists a deterministic transition message to the user (*"I don't have this in my knowledge base — let me check the live system."* / Italian variant) as a real msg task on the existing plan, with the standard webhook delivery.
2. Reassigns `msg_class = "investigate"` and falls through to the planner branch with `investigate=True`.

This is the **transparent fallback** designed in M1291. The classifier itself cannot know in advance whether a fact exists; the dispatcher checks before committing to the chat_kb path. If the facts query raises (DB error), the fallback is NOT triggered — the worker proceeds with the original chat_kb path. Fallback is reserved for *empty result*, not *failed query*.

**Trade-off**: the pre-flight uses raw keywords, while the full chat_kb path adds `entity_id` + `relevant_tags` derived from the briefer. The pre-flight is strictly less precise, so it can produce false negatives (briefer would have found something the keyword search missed). In that case the user gets an investigate plan instead of a chat response — verbose but never wrong.

**Model**: use a fast, cheap model — the task is a single-token classification with a small fixed vocabulary. Using a reasoning model here wastes time and tokens. See [config.md](config.md) for the default.

**Fallback on LLM error**: returns `("plan", "")`. The planner handles everything and the messenger detects the language from the user message.

**Prompt file**: `kiso/roles/classifier.md`

---

## Inflight Classifier

**When**: a message arrives on a session that already has a job running. Used by the API layer in `kiso/api/sessions.py` before deciding whether to queue the new message, cancel the running job, or merge the request.

**Input**: the running plan's goal, the new user message, and a short recent-conversation snippet — assembled by `build_inflight_classifier_messages` via string template substitution into `kiso/roles/inflight-classifier.md`.

**Output**: one bare category word.

**Categories**:

| Category | Meaning | Effect on the running job |
|---|---|---|
| `stop` | The user wants to cancel or abort the current job | Sets the cancel event; the worker tears down its task and processes the new message as a fresh request. |
| `update` | The user is modifying parameters of the current job (e.g. *"use port 8080 instead"*) | Queued; merged into the in-flight context for the next replan. |
| `independent` | Unrelated request that can wait until the current job finishes | Queued normally (drained after the current plan completes). |
| `conflict` | Contradicts or replaces the current job entirely (e.g. *"no, do X instead"*) | Cancels the running job and starts fresh on the new message. |

**Why it is a separate role from the initial classifier**: see the M1294 review in [devplan/v0.9-wip.md](../devplan/v0.9-wip.md). Short version: the two prompts share less than 5% of their text, the categories are completely disjoint, and merging them would force a single LLM call to choose between 8 categories — strictly worse for accuracy. They share only the model assignment via the `classifier` model key in `_MODEL_METADATA`.

**Stop pattern fast-path**: pure stop words (*"stop"*, *"ferma"*, *"basta"*, *"cancel"*, ALL-CAPS urgent messages ≥4 chars) are matched by `is_stop_message()` in `kiso/brain/common.py` BEFORE the LLM is called — the inflight classifier is skipped entirely for these. This keeps the urgent path latency-free.

**Model**: same as the initial classifier. See [config.md](config.md).

**Fallback on LLM error**: returns `"independent"` (safe — message gets queued for later).

**Prompt file**: `kiso/roles/inflight-classifier.md`

---

## Planner

**When**: a new message arrives on a session.

**Input**: see [Context per Role](#context-per-role) table. The planner sees three layers of history: session summary (compressed past), recent msg outputs (what the bot communicated), and last N raw messages (immediate conversation).

**Output**: JSON with a `goal`, `secrets` (nullable), and a `tasks` list.

### Structured Output (required)

Uses `response_format` with a strict JSON schema. Strict mode: all properties in `required` (optional = nullable types), `additionalProperties: false` everywhere:

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "plan",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "secrets": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {"type": "string"}
                        },
                        "required": ["key", "value"],
                        "additionalProperties": False
                    }
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["exec", "msg", "wrapper", "search", "replan"]},
                            "detail": {"type": "string"},
                            "wrapper": {"type": ["string", "null"]},
                            "args": {"type": ["string", "null"]},
                            "expect": {"type": ["string", "null"]}
                        },
                        "required": ["type", "detail", "wrapper", "args", "expect"],
                        "additionalProperties": False
                    }
                },
                "extend_replan": {"type": ["integer", "null"]}
            },
            "required": ["goal", "secrets", "tasks", "extend_replan"],
            "additionalProperties": False
        }
    }
}
```

Schema notes:
- **`secrets`**: array of `{key, value}` pairs — ephemeral credentials extracted from user messages. Stored in worker memory only, never in DB. `null` when no secrets. Example: `[{"key": "api_token", "value": "tok_abc123"}]`
- **`args`**: JSON string (strict mode doesn't allow dynamic-key objects). `null` for `exec`, `msg`, and `replan` tasks. Required for `wrapper` tasks (validated against `kiso.toml` schema). Optional for `search` tasks (`null` or JSON with `max_results`, `lang`, `country`).
- **Optional task fields** (`wrapper`, `args`, `expect`): nullable — `null` when not applicable.
- **`review` field removed**: `exec`, `wrapper`, and `search` tasks are always reviewed. `msg` tasks are never reviewed. The task type determines behavior.

Provider guarantees valid JSON at decoding level — no parse retries needed. If the provider doesn't support structured output, the call fails with a clear error:

```
Provider "ollama" does not support structured output.
Planner, Reviewer, and Curator require it. Route these roles to a compatible provider
(e.g. models.planner = "openrouter:z-ai/glm-4.7").
```

Structured output is a hard requirement for Planner, Reviewer, and Curator. Worker, Searcher, Summarizer, and Paraphraser produce free-form text.

### Validation After Parsing

JSON structure is guaranteed by the provider, but kiso validates **semantics** before execution:

1. `exec`, `wrapper`, and `search` tasks must have a non-null `expect`
2. `msg` and `replan` tasks must have `expect = null`
3. Last task must be `type: "msg"` or `type: "replan"` (user gets a response, or investigation triggers a new plan)
4. Every `wrapper` reference must exist in installed wrappers
5. Every `wrapper` task's `args` must be valid JSON matching the wrapper's schema from `kiso.toml`
6. `tasks` list must not be empty
7. `replan` tasks must have `wrapper = null` and `args = null`, and can only be the last task
8. A plan can have at most one `replan` task
9. `search` tasks must have `wrapper = null`, `expect` non-null, and `args` (if present) must be valid JSON with optional keys: `max_results` (int), `lang` (string), `country` (string)

On failure, kiso sends the plan back with specific errors, up to `max_validation_retries` (default 3):

```
Your plan has errors:
- Task 2: wrapper "aider" requires arg "message" (string, required) but it's missing
- Task 3: exec task missing expect field
Fix these and return the corrected plan.
```

If exhausted: fail the message, notify user. No silent fallback.

### Identity and Environment Awareness

The planner knows it is the planning component of Kiso, not a generic task planner. Kiso operates on two layers: the **OS layer** (direct shell commands) and the **Kiso layer** (native primitives: wrappers, connectors, env vars, memory). The planner checks for a Kiso-native solution before reaching for OS-level commands, and only proceeds when both intent and target are unambiguous — otherwise it asks the user first.

### Prompt Design

**System prompt** (`roles/planner.md`) is modular — a fixed core plus 11 conditional modules selected by the briefer (see [Briefer](#briefer)). When the briefer is disabled, keyword-based fallback selects modules.

**1. Few-shot examples.** Complete plan examples in `roles/planner.md`. Cover: coding task (msg → wrapper → exec → msg), research task (wrapper → msg). All task fields always present (strict mode); nullable fields are `null`.

**2. Task templates** as reference patterns (not forced, just suggested):

```
Common patterns:
- Code change: msg → wrapper(aider) → exec(test) → msg
- Web lookup: search → msg
- Bulk research: wrapper(search) → msg (if search wrapper installed, cheaper for >10 results)
- Investigation: search → exec → replan (gather info, then replan with results)
- Simple question: msg
- Clarification needed: msg (ask the user)
- Multi-step build: msg → exec(setup) → wrapper(aider) → exec(test) → msg
```

**3. Rules** — the expected JSON format, available task types, available wrappers with args schemas, caller role, and these constraints:
- Task `detail` must be self-contained — the worker does not see the conversation
- **CRITICAL**: The last task must be `type: "msg"` or `type: "replan"` — the user always gets a final response, or investigation triggers a new plan. This rule is marked `CRITICAL:` in the prompt because some models skip it under token pressure, wasting a validation retry.
- `exec`, `wrapper`, and `search` tasks must have an `expect` field (they are always reviewed)
- `msg` tasks are the only way to communicate with the user
- **Asking the user**: if the planner needs information it doesn't have, it ends the plan with a `msg` task asking the question. The next message cycle will have the user's answer in context (recent messages + msg outputs). Two cases:
  - Request is ambiguous or missing critical info **upfront** → produce a single `msg` task asking for clarification, do not guess
  - Planner realizes **mid-planning** that a later step depends on unknown user input → stop planning at that point, end with a `msg` asking the question. Do not plan tasks that depend on answers you don't have yet
- **Task output chaining**: outputs from earlier tasks are available to later tasks in the same plan. For `exec`: read `.kiso/plan_outputs.json` in the workspace. For `wrapper` and `msg`: provided automatically. Plan commands that use previous results accordingly
- If a user (non-admin) shares credentials, extract them into `secrets` (ephemeral, not persisted) and inform the user they are temporary
- If a user asks to permanently configure a credential, respond with a `msg` task telling them to ask an admin to set it as a deploy secret via `kiso env set`
- If an admin asks to configure a credential, generate exec tasks: `kiso env set ... && kiso env reload`
- To make files publicly accessible, write them to `pub/` in the exec CWD. Files there are auto-served at `/pub/` URLs (no auth). URLs appear in exec task output. **Important**: `/pub/<token>/filename` is the HTTP download URL — not a filesystem path. For exec tasks that read or write public files, always use the relative path `pub/filename` (relative to exec CWD).

### Task Fields

All fields are always present in the JSON output (strict mode requires it). The "Non-null when" column indicates when the field must have a meaningful value; otherwise it is `null`.

| Field | Non-null when | Description |
|---|---|---|
| `type` | always | `exec`, `msg`, `wrapper`, `search`, `replan` |
| `detail` | always | What to do (natural language). For `msg` tasks, must include all context the worker needs. For `exec` tasks, describes the operation — the exec translator will convert it to a shell command. For `search` tasks, the search query. |
| `expect` | `type` is `exec`, `wrapper`, or `search` | Success criteria for THIS task's output only — not the overall plan goal. Must be verifiable from the task's direct output. For maintenance commands, "0 changes" is a valid success state and should be stated explicitly. Required — all exec/wrapper/search tasks are reviewed. |
| `wrapper` | `type` is `wrapper` | Wrapper name. Must be `null` for search tasks. |
| `args` | `type` is `wrapper` (required); `type` is `search` (optional) | For wrappers: arguments as a JSON string validated against `kiso.toml` schema. For search: nullable — `null` or JSON `{"max_results": N, "lang": "xx", "country": "XX"}`. |

### Output Fields

- `goal`: high-level objective for the entire process. Persisted in `store.plans` (not on individual tasks). Passed to the reviewer as **Plan Context** (background only — not used as success criterion).
- `secrets`: always present. `null` when no credentials; array of `{key, value}` pairs when the user mentioned them. Ephemeral — stored in worker memory only, never in DB. See [security.md — Ephemeral Secrets](security.md#ephemeral-secrets).
- `extend_replan`: always present. `null` normally; a positive integer (max 3) when the planner needs additional replan attempts beyond the default limit. The worker grants at most +3 extra attempts.

After validation, the planner output becomes a **plan** entity — see [database.md — plans](database.md#plans).

---

## Reviewer

**When**: after execution of every `exec`, `wrapper`, and `search` task (always — no opt-out).

**Input**: see [Context per Role](#context-per-role) table. Task output is fenced (see [security.md](security.md#layer-2-random-boundary-fencing)).

**Output**: JSON (via structured output, same as planner) with `status`, optional `reason`, and optional `learn`.

### Structured Output (required)

Same mechanism as the planner (`response_format` with strict JSON schema):

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "review",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok", "replan"]},
                "reason": {"type": ["string", "null"]},
                "learn": {"type": ["string", "null"]}
            },
            "required": ["status", "reason", "learn"],
            "additionalProperties": False
        }
    }
}
```

### Status Values

| Status | Meaning | Effect |
|---|---|---|
| `"ok"` | Task output meets expectations | Proceed to next task |
| `"replan"` | Output is wrong, strategy needs revision | Notify user, mark remaining tasks as `failed` in DB, call planner with full context |

No "local fix" status — the planner replans with full context and decides whether to make a small correction or complete rework. One recovery mechanism, one depth counter.

### Fields

- `learn`: optional free-form string. Stored as a new entry in `store.learnings` (pending evaluation by the curator). NOT stored directly in facts.
- `reason`: expected when `status: "replan"`. Explains why the task failed. Included in user notification and replanner context. Schema makes it nullable — kiso validates non-null on `replan` and retries reviewer if missing (up to `max_validation_retries`).
- **Max replan depth**: after `max_replan_depth` cycles for the same message, worker stops replanning, notifies user, moves on.

### Examples

```json
{"status": "ok", "reason": null, "learn": null}

{"status": "replan",
 "reason": "The project uses Flask, not FastAPI. The entire approach to adding middleware needs to change.",
 "learn": "Project framework is Flask, not FastAPI"}

{"status": "ok", "reason": null, "learn": "Project uses pytest for testing"}
```

### Rules in the Default Prompt

- **Plan Context**: provided as background only. The sole success criterion is the task's `expect` — not the overall plan goal.
- **Exit code**: a non-zero exit code is a strong failure signal even if the output appears partially correct. A zero exit code is necessary but not sufficient — the output must also satisfy `expect`.
- **Maintenance commands**: a command that exits 0 with "nothing to do" or "0 changes" satisfies an expect about resolving issues — there were none to resolve.
- **Warnings**: warnings (missing env vars, deprecations, non-fatal notices) are informational. They do not override a successful exit code + satisfied `expect`. The reviewer marks replan for a warning only if the `expect` explicitly requires absence of warnings (e.g. "no warnings", "clean output").

### Replan Flow

See [flow.md — Replan Flow](flow.md#g-replan-flow-if-reviewer-returns-replan) for the full replan sequence (notify user → call planner with completed/remaining/failure/replan_history → new plan → continue execution).

---

## Worker

**When**: before executing every `exec` task. Acts as the "editor" in the architect/editor pattern (planner = architect, worker = editor).

> **Naming note**: this role used to be called *Exec Translator*; the Python function was `run_exec_translator`. After M1293, the role file is `kiso/roles/worker.md` and the brain entry point is `kiso.brain.run_worker`. The unrelated session-loop function `kiso.worker.loop.run_worker` is the long-running worker process for a session — disambiguation is by package path. The brain `run_worker` is imported into `kiso.worker.loop` as `run_worker_role` to avoid local shadowing.

**Input**: see [Context per Role](#context-per-role) table. Receives the task `detail` (natural language), the system environment (available binaries, shell, CWD), and preceding plan outputs.

**Output**: free-form text — the exact shell command(s) to run. The translated command is stored in the task's `command` column in the database, so the CLI can display it alongside the task header (e.g. `$ ls -la`).

### How It Works

The planner writes `exec` task details as natural-language descriptions (e.g., "List all Python files in the project directory"). The worker receives this description along with the system environment context (available binaries, OS, shell, working directory) and preceding task outputs, then produces the exact shell command (e.g., `find . -name "*.py" -type f`).

Uses the `worker` model (same LLM as `msg` tasks). Custom prompt at `~/.kiso/roles/worker.md`.

### Rules in the Default Prompt

- Output ONLY the shell command(s), no explanation, no markdown fences
- If multiple commands are needed, join with `&&` or `;`
- Use only binaries listed as available in the system environment
- If a Retry Context with a hint is provided, the hint takes priority over a literal re-translation of the task detail
- Do NOT add `sudo` unless it is explicitly mentioned in the task detail or in the system environment
- If the task cannot be accomplished, output `CANNOT_TRANSLATE` (triggers a failure, not a silent empty command)

### Error Handling

If translation fails (LLM error or `CANNOT_TRANSLATE`), the task is marked `failed` and the plan stops. The reviewer does not run — there is no output to review. The planner may replan with the failure context.

---

## Messenger

**When**: executing `msg` type tasks (text generation). This is the role that produces the actual reply the user sees.

**Input**: see [Context per Role](#context-per-role) table. Includes preceding plan outputs (fenced) — outputs from earlier tasks in the same plan, so the messenger can reference results when writing responses. Also includes a `briefing_context` from the briefer (filtered facts and entities relevant to the current message), and a `response_lang` hint propagated from the classifier so the reply uses the user's detected language.

**Output**: free-form text. Sanitized through `_sanitize_messenger_output` to strip any hallucinated `<tool_call>` / `<function_call>` markup before delivery.

### Why the messenger doesn't see the raw conversation

Deliberate design choice:

1. **Focus + separation.** The planner already interpreted the conversation into a self-contained `detail`. The messenger executes — no re-interpretation, no second-guessing the plan.
2. **Cost.** The planner pays the conversation-tokens cost once. The messenger (called multiple times per plan) stays cheap.
3. **Predictability.** Behavior depends only on (facts + summary + detail). No hidden context, easier to debug.

If `detail` lacks context, the reviewer catches it and triggers a replan.

**Prompt file**: `kiso/roles/messenger.md`

---

## Searcher

**When**: executing `search` type tasks (web search queries).

**Model**: a search-capable model (see [config.md](config.md) for the default). Users can override in `config.toml [models]`.

**Input**: see [Context per Role](#context-per-role) table. Receives the task `detail` (search query) and preceding plan outputs (fenced). Optional search parameters from `args`: `max_results`, `lang`, `country`.

**Output**: free-form text. NOT structured output — no `response_format`. The system prompt suggests a JSON structure (`results`, `summary`, `sources`) for consistency, but the output is treated as opaque text: it flows as-is into `plan_outputs` and is reviewed by the reviewer. No JSON parsing is performed — the messenger and reviewer work with the raw text regardless of format.

### How It Works

The planner creates a `search` task with `detail` containing the search query. The searcher LLM (with online/grounded search capability via OpenRouter's `:online` suffix) executes the query and returns results. Optional `args` can specify `{"max_results": N, "lang": "xx", "country": "XX"}` to constrain the search. The `expect` field provides semantic success criteria for the reviewer.

Search tasks are always reviewed (same as exec/wrapper). Results flow into `plan_outputs` for subsequent tasks.

### Coexistence with Search Wrapper

The built-in searcher and the `search` wrapper (if installed) coexist:

| | Built-in searcher | Search wrapper |
|---|---|---|
| **Best for** | Simple lookups (1-10 results) | Bulk queries (>10 results), pagination, advanced filtering |
| **Cost** | ~$0.014/query (LLM + Exa) | ~$0.001-0.003/query (Brave/Serper API) |
| **Task type** | `search` | `wrapper` |
| **Requires install** | No (built-in) | Yes |

The planner prompt instructs it to prefer the search wrapper for bulk queries when installed, and use the built-in `search` task type for simple lookups.

### System Prompt

Custom prompt: `~/.kiso/instances/{name}/roles/searcher.md` (user override) or `kiso/roles/searcher.md` (package default). Same override mechanism as all other roles.

---

## Summarizer

**When**: after queue completion, if raw messages >= `summarize_threshold`. Compresses old conversation history into a structured summary so the planner / briefer don't keep re-paying the full-history token cost on every message.

**Input**: current summary + oldest messages + their msg task outputs.

**Output**: an updated structured summary stored in `sessions.summary` with four sections:

```markdown
## Session Summary
Brief narrative of what happened and current state.

## Key Decisions
- Chose Flask over FastAPI for this project.

## Open Questions
- Still need to clarify deployment target.

## Working Knowledge
- File structure: src/app.py, tests/
- Current branch: main
```

**Prompt file**: `kiso/roles/summarizer.md` (renamed from `summarizer-session.md` in M1293; an idempotent in-place migration in `kiso/main.py:_migrate_summarizer_session_role` rewrites any existing user override the first time the new code boots).

**Model**: a fast cheap model — see [config.md](config.md). Same model as the consolidator and paraphraser.

---

## Consolidator

**When**: periodically, governed by `consolidation_enabled`, `consolidation_interval_hours` (default 24h), and `consolidation_min_facts` (default 20). Triggered from the post-plan knowledge phase in `kiso/worker/message_flow.py`.

**Input**: every fact in the session's knowledge base (or global, depending on scope) — content, tags, entity, age.

**Output**: structured JSON proposing dedupes, merges, demotions, and archives. The result is applied via `apply_consolidation_result` so the changes are observable in the DB and reversible from logs.

**Purpose**: the curator promotes individual facts immediately after each plan; the consolidator does the periodic *holistic* pass — finding duplicates that emerged over many plans, demoting facts that are no longer reinforced, archiving stale entries below a confidence floor. Without it the knowledge base grows monotonically and gets noisy.

**Decay + archive**: after consolidation, the worker runs `decay_facts` and `archive_low_confidence_facts` (pure SQL, no LLM dependency) — see [flow.md — Post-Execution](flow.md#4-post-execution).

**Model**: same fast cheap model family as the summarizer / paraphraser.

**Prompt file**: `kiso/roles/consolidator.md`

---

## Curator

**When**: after any execution cycle that produced learnings (reviewer `learn` fields). Runs after the worker finishes processing a message, if there are pending learnings.

**Input**: see [Context per Role](#context-per-role) table.

**Output**: JSON (via structured output) evaluating each learning.

### Structured Output (required)

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "curation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "evaluations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "learning_id": {"type": "integer"},
                            "verdict": {"type": "string", "enum": ["promote", "ask", "discard"]},
                            "fact": {"type": ["string", "null"]},
                            "category": {"anyOf": [{"type": "string", "enum": ["project", "user", "wrapper", "general"]}, {"type": "null"}]},
                            "question": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                            "tags": {"anyOf": [{"type": "array", "items": {"type": "string"}, "maxItems": 5}, {"type": "null"}]},
                            "entity_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "entity_kind": {"anyOf": [{"type": "string", "enum": ["website", "company", "wrapper", "person", "project", "concept"]}, {"type": "null"}]}
                        },
                        "required": ["learning_id", "verdict", "fact", "category", "question", "reason", "tags", "entity_name", "entity_kind"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["evaluations"],
            "additionalProperties": False
        }
    }
}
```

### Verdicts

| Verdict | Meaning | Effect |
|---|---|---|
| `promote` | Learning is a confirmed, important fact | `fact` + `category` become a new entry in `store.facts`. `tags` (1-5) enable semantic retrieval. `entity_name` + `entity_kind` link the fact to an entity record (created if new). `category` determines session scoping: `"user"` facts are session-scoped; `"project"`, `"wrapper"`, `"general"` facts are global. Learning marked `promoted`. |
| `ask` | Uncertain but potentially important | `question` field becomes a new entry in `store.pending` (scope = session). The planner will ask the user for confirmation. |
| `discard` | Trivial, transient, or already covered | Learning marked `discarded`. `reason` explains why. |

### What the Curator Promotes

The curator's prompt instructs it to promote only:
- Verified technical facts (framework, language, architecture, conventions)
- Explicit decisions confirmed by users or observed in code
- Stable project context (team structure, deployment targets)

And to discard:
- Casual remarks, opinions, jokes
- Transient information (current task status, in-progress work)
- Information already covered by existing facts

### Confirmation Flow

When the curator returns `verdict: "ask"`, the question is stored as a pending item. The next planner call in that session sees it and generates a `msg` task to ask the user. The user's response may produce a new learning, which the curator then evaluates with stronger evidence (user confirmation) and promotes.

---

## Paraphraser

**When**: before the planner call, if there are untrusted messages (from non-whitelisted users) in the context window.

**Input**: see [Context per Role](#context-per-role) table. **Output**: free-form text — third-person factual summaries.

Reuses `models.summarizer`. See [security.md — Prompt Injection Defense](security.md#6-prompt-injection-defense) for the full defense layers.

---

## Token Usage Tracking

Every `call_llm` invocation accumulates token usage (input and output tokens, model name) in a `contextvars`-based per-message accumulator. The worker calls `reset_usage_tracking()` at the start of each message and `get_usage_summary()` at the end, storing the totals in `plans.total_input_tokens`, `plans.total_output_tokens`, and `plans.model`. The CLI displays this summary at the end of plan execution (e.g. `⟨ 1,234 in → 567 out │ provider/model-name ⟩`).

---

## Scalability Note

Each session gets its own asyncio worker. Workers are lightweight (just a loop draining a queue), and the real bottleneck is LLM API latency and subprocess execution — not the workers themselves. For deployments with hundreds of concurrent sessions, consider a worker pool with a shared queue instead of per-session workers.
