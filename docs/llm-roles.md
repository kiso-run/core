# LLM Roles

Each LLM call has its own role. Each role has its own model (from `config.toml`), its own system prompt (from `~/.kiso/roles/{role}.md`), and receives **only the context it needs**.

## Context per Role

| Context piece | Planner | Reviewer | Worker | Summarizer | Curator | Paraphraser |
|---|---|---|---|---|---|---|
| Session summary | yes | - | yes | yes (existing) | yes | - |
| Last N raw messages | yes | - | - | - | - | - |
| Recent msg outputs | yes | - | - | - | - | - |
| Paraphrased untrusted messages | yes | - | - | - | - | generates |
| New message | yes | - | - | - | - | - |
| Facts (global) | yes | - | yes | - | yes | - |
| Pending items (global + session) | yes | - | - | - | yes | - |
| Allowed skill summaries + args schemas | yes | - | - | - | - | - |
| Caller role (admin/user) | yes | - | - | - | - | - |
| Process goal | generates | yes | - | - | - | - |
| Preceding plan outputs (fenced) | - | - | yes | - | - | - |
| Current task detail | - | yes | yes | - | - | - |
| Current task expect | - | yes | - | - | - | - |
| Current task output (fenced) | - | yes | - | - | - | - |
| Original user request | - | yes | - | - | - | - |
| Messages to compress + their msg outputs | - | - | - | yes | - | - |
| Pending learnings | - | - | - | - | yes | - |
| Completed tasks + outputs (fenced) | replan only | - | - | - | - | - |
| Remaining tasks | replan only | - | - | - | - | - |
| Failure reason | replan only | - | - | - | - | - |
| Replan history | replan only | - | - | - | - | - |
| Raw untrusted messages (batch) | - | - | - | - | - | yes |

Key principle: the planner must put everything the worker needs into the task `detail` — the worker won't see the conversation (see [Why the Worker Doesn't See the Conversation](#why-the-worker-doesnt-see-the-conversation)).

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
                            "type": {"type": "string", "enum": ["exec", "msg", "skill"]},
                            "detail": {"type": "string"},
                            "skill": {"type": ["string", "null"]},
                            "args": {"type": ["string", "null"]},
                            "expect": {"type": ["string", "null"]}
                        },
                        "required": ["type", "detail", "skill", "args", "expect"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["goal", "secrets", "tasks"],
            "additionalProperties": False
        }
    }
}
```

Schema notes:
- **`secrets`**: array of `{key, value}` pairs — ephemeral credentials extracted from user messages. Stored in worker memory only, never in DB. `null` when no secrets. Example: `[{"key": "api_token", "value": "tok_abc123"}]`
- **`args`**: JSON string (strict mode doesn't allow dynamic-key objects). `null` for non-skill tasks.
- **Optional task fields** (`skill`, `args`, `expect`): nullable — `null` when not applicable.
- **`review` field removed**: `exec` and `skill` tasks are always reviewed. `msg` tasks are never reviewed. The task type determines behavior.

Provider guarantees valid JSON at decoding level — no parse retries needed. If the provider doesn't support structured output, the call fails with a clear error:

```
Provider "ollama" does not support structured output.
Planner, Reviewer, and Curator require it. Route these roles to a compatible provider
(e.g. models.planner = "openrouter:moonshotai/kimi-k2.5").
```

Structured output is a hard requirement for Planner, Reviewer, and Curator. Worker, Summarizer, and Paraphraser produce free-form text.

### Validation After Parsing

JSON structure is guaranteed by the provider, but kiso validates **semantics** before execution:

1. `exec` and `skill` tasks must have a non-null `expect`
2. `msg` tasks must have `expect = null`
3. Last task must be `type: "msg"` (user always gets a final response)
4. Every `skill` reference must exist in installed skills
5. Every `skill` task's `args` must be valid JSON matching the skill's schema from `kiso.toml`
6. `tasks` list must not be empty

On failure, kiso sends the plan back with specific errors, up to `max_validation_retries` (default 3):

```
Your plan has errors:
- Task 2: skill "aider" requires arg "message" (string, required) but it's missing
- Task 3: exec task missing expect field
Fix these and return the corrected plan.
```

If exhausted: fail the message, notify user. No silent fallback.

### Prompt Design

**System prompt** (`roles/planner.md`) includes:

**1. Few-shot examples.** Two complete plan examples. All task fields are always present (strict mode); nullable fields are `null` when not applicable.

```
Example 1 — coding task:
User: "add JWT authentication"
{
  "goal": "Add JWT auth with login endpoint, middleware, and tests",
  "secrets": null,
  "tasks": [
    {"type": "msg", "detail": "Tell the user: starting work on JWT authentication.",
     "skill": null, "args": null, "expect": null},
    {"type": "skill", "detail": "Add JWT auth module",
     "skill": "aider", "args": "{\"message\": \"create JWT auth module with /login and /logout endpoints\"}",
     "expect": "auth module created with login endpoint and JWT middleware"},
    {"type": "exec", "detail": "python -m pytest tests/",
     "skill": null, "args": null,
     "expect": "all tests pass"},
    {"type": "msg", "detail": "We added JWT auth with /login, /logout, and jwt_required middleware. Tests pass. Summarize for the user.",
     "skill": null, "args": null, "expect": null}
  ]
}

Example 2 — research task:
User: "find out how to deploy on fly.io"
{
  "goal": "Research fly.io deployment and summarize for the user",
  "secrets": null,
  "tasks": [
    {"type": "skill", "detail": "Search for fly.io deployment guides",
     "skill": "search", "args": "{\"query\": \"fly.io python deployment guide\"}",
     "expect": "relevant search results about fly.io deployment"},
    {"type": "msg", "detail": "The user wants to deploy on fly.io. Based on the search results: [search output will be here]. Write a clear summary of the deployment steps.",
     "skill": null, "args": null, "expect": null}
  ]
}
```

**2. Task templates** as reference patterns (not forced, just suggested):

```
Common patterns:
- Code change: msg → skill(aider) → exec(test) → msg
- Research: skill(search) → msg
- Simple question: msg
- Clarification needed: msg (ask the user)
- Multi-step build: msg → exec(setup) → skill(aider) → exec(test) → msg
```

**3. Rules** — the expected JSON format, available task types, available skills with args schemas, caller role, and these constraints:
- Task `detail` must be self-contained — the worker does not see the conversation
- The last task must be `type: "msg"` — the user always gets a final response
- `exec` and `skill` tasks must have an `expect` field (they are always reviewed)
- `msg` tasks are the only way to communicate with the user
- **Asking the user**: if the planner needs information it doesn't have, it ends the plan with a `msg` task asking the question. The next message cycle will have the user's answer in context (recent messages + msg outputs). Two cases:
  - Request is ambiguous or missing critical info **upfront** → produce a single `msg` task asking for clarification, do not guess
  - Planner realizes **mid-planning** that a later step depends on unknown user input → stop planning at that point, end with a `msg` asking the question. Do not plan tasks that depend on answers you don't have yet
- **Task output chaining**: outputs from earlier tasks are available to later tasks in the same plan. For `exec`: read `.kiso/plan_outputs.json` in the workspace. For `skill` and `msg`: provided automatically. Plan commands that use previous results accordingly
- If a user (non-admin) shares credentials, extract them into `secrets` (ephemeral, not persisted) and inform the user they are temporary
- If a user asks to permanently configure a credential, respond with a `msg` task telling them to ask an admin to set it as a deploy secret via `kiso env set`
- If an admin asks to configure a credential, generate exec tasks: `kiso env set ... && kiso env reload`

### Task Fields

All fields are always present in the JSON output (strict mode requires it). The "Non-null when" column indicates when the field must have a meaningful value; otherwise it is `null`.

| Field | Non-null when | Description |
|---|---|---|
| `type` | always | `exec`, `msg`, `skill` |
| `detail` | always | What to do. For `msg` tasks, must include all context the worker needs. For `exec` tasks, the shell command. |
| `expect` | `type` is `exec` or `skill` | Semantic success criteria (e.g. "tests pass", not exact output). Required — all exec/skill tasks are reviewed. |
| `skill` | `type` is `skill` | Skill name. |
| `args` | `type` is `skill` | Skill arguments as a JSON string. Kiso parses and validates against `kiso.toml` schema. |

### Output Fields

- `goal`: high-level objective for the entire process. Persisted in `store.plans` (not on individual tasks). The reviewer uses it to evaluate individual tasks in context.
- `secrets`: always present. `null` when no credentials; array of `{key, value}` pairs when the user mentioned them. Ephemeral — stored in worker memory only, never in DB. See [security.md — Ephemeral Secrets](security.md#ephemeral-secrets).

After validation, the planner output becomes a **plan** entity — see [database.md — plans](database.md#plans).

---

## Reviewer

**When**: after execution of every `exec` and `skill` task (always — no opt-out).

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

### Replan Flow

See [flow.md — Replan Flow](flow.md#g-replan-flow-if-reviewer-returns-replan) for the full replan sequence (notify user → call planner with completed/remaining/failure/replan_history → new plan → continue execution).

---

## Worker

**When**: executing `msg` type tasks (text generation).

**Input**: see [Context per Role](#context-per-role) table. Includes preceding plan outputs (fenced) — outputs from earlier tasks in the same plan, so the worker can reference results when writing responses.

**Output**: free-form text.

### Why the Worker Doesn't See the Conversation

Deliberate design choice:

1. **Focus + separation.** The planner already interpreted the conversation into a self-contained `detail`. The worker executes — no re-interpretation, no second-guessing the plan.
2. **Cost.** The planner pays the conversation-tokens cost once. The worker (called multiple times per plan) stays cheap.
3. **Predictability.** Behavior depends only on (facts + summary + detail). No hidden context, easier to debug.

If `detail` lacks context, the reviewer catches it and triggers a replan.

---

## Summarizer

**When**: after queue completion, if raw messages >= `summarize_threshold`. Also when facts exceed `knowledge_max_facts`.

Two tasks (see [Context per Role](#context-per-role) table):
- **Messages**: current summary + oldest messages + their msg task outputs → updated summary. Includes bot responses so the summary captures what was communicated, not just what was asked.
- **Facts**: all fact entries → consolidated into fewer entries (merges duplicates, removes outdated).

**Output**: free-form text. **Prompt** (`roles/summarizer.md`): preserve facts, decisions, technical context. Discard noise and redundancy.

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
                            "question": {"type": ["string", "null"]},
                            "reason": {"type": ["string", "null"]}
                        },
                        "required": ["learning_id", "verdict", "fact", "question", "reason"],
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
| `promote` | Learning is a confirmed, important fact | `fact` field becomes a new entry in `store.facts`. Learning marked `promoted`. |
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

## Scalability Note

Each session gets its own asyncio worker. Workers are lightweight (just a loop draining a queue), and the real bottleneck is LLM API latency and subprocess execution — not the workers themselves. For deployments with hundreds of concurrent sessions, consider a worker pool with a shared queue instead of per-session workers.
