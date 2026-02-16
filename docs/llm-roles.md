# LLM Roles

Kiso makes 4 distinct types of LLM calls. Each type has its own model (from `config.toml`), its own system prompt (from `~/.kiso/roles/{role}.md`), and receives **only the context it needs**.

## Context per Role

| Context piece | Planner | Reviewer | Worker | Summarizer |
|---|---|---|---|---|
| Session summary | yes | - | yes | yes (existing, to be updated) |
| Last N raw messages | yes | - | - | - |
| Recent msg outputs | yes | - | - | - |
| New message | yes | - | - | - |
| Facts (global, from store.facts) | yes | - | yes | - |
| Allowed skill summaries + args schemas | yes | - | - | - |
| Caller role (admin/user) | yes | - | - | - |
| Process goal | generates | yes | - | - |
| Current task detail | - | yes | yes | - |
| Current task expect | - | yes | - | - |
| Current task output | - | yes | - | - |
| Original user request | - | yes | - | - |
| Messages to compress | - | - | - | yes |
| Completed tasks + outputs | replan only | - | - | - |
| Remaining tasks | replan only | - | - | - |
| Failure reason | replan only | - | - | - |
| Replan history | replan only | - | - | - |

Key principles:
- The **planner** is the architect. Gets the big picture: recent conversation, session summary, facts, allowed skills. Decides what to do. Must put everything the worker needs into the task `detail` — the worker won't see the conversation.
- The **reviewer** gets the task, its output, the expected outcome, and the process goal — enough to evaluate whether the task succeeded in context.
- The **worker** is the executor. Gets facts + session summary + task detail. Does not see conversation messages — by design (see below).
- The **summarizer** gets only what it needs to compress.

---

## Planner

**When**: a new message arrives on a session.

**Input**: facts + session summary + last `context_messages` raw messages (default 5) + recent msg outputs (all `msg` task outputs since last summary) + new message + allowed skill summaries and args schemas (filtered by user's `skills` config) + caller role.

The planner sees three layers of history:
1. **Session summary** — compressed history of everything before the recent window
2. **Recent msg outputs** — what the bot communicated to the user (msg task outputs since last summarization)
3. **Last N raw messages** — the most recent user messages (default 5, configurable)

This keeps the prompt lean while giving the planner both the immediate conversation and a record of recent actions.

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
                            "expect": {"type": ["string", "null"]},
                            "review": {"type": ["boolean", "null"]},
                            "model": {"type": ["string", "null"]}
                        },
                        "required": ["type", "detail", "skill", "args", "expect", "review", "model"],
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
- **`secrets`**: array of `{key, value}` pairs (strict mode prohibits `additionalProperties` as a schema). `null` when no secrets.
- **`args`**: JSON string (strict mode doesn't allow dynamic-key objects). `null` for non-skill tasks.
- **Optional task fields** (`skill`, `args`, `expect`, `review`, `model`): nullable — `null` when not applicable.

Provider guarantees valid JSON at decoding level — no parse retries needed. If the provider doesn't support structured output, the call fails with a clear error:

```
Provider "ollama" does not support structured output.
Planner and Reviewer require it. Route these roles to a compatible provider
(e.g. models.planner = "openrouter:moonshotai/kimi-k2.5").
```

Structured output is a hard requirement for Planner and Reviewer. Worker and Summarizer produce free-form text.

### Validation After Parsing

JSON structure is guaranteed by the provider, but kiso validates **semantics** before execution:

1. Every task with `review: true` must have a non-null `expect`
2. Last task must be `type: "msg"` (user always gets a final response)
3. Every `skill` reference must exist in installed skills
4. Every `skill` task's `args` must be valid JSON matching the skill's schema from `kiso.toml`
5. `tasks` list must not be empty

On failure, kiso sends the plan back with specific errors, up to `max_validation_retries` (default 3):

```
Your plan has errors:
- Task 2: skill "aider" requires arg "message" (string, required) but it's missing
- Task 4: has review=true but no expect field
Fix these and return the corrected plan.
```

If exhausted: fail the message, notify user. No silent fallback.

### Prompt Design

**System prompt** (`roles/planner.md`) includes:

**1. Few-shot examples.** Two complete plan examples covering different scenarios. Note: all task fields are always present (strict mode); nullable fields are `null` when not applicable.

```
Example 1 — coding task:
User: "add JWT authentication"
{
  "goal": "Add JWT auth with login endpoint, middleware, and tests",
  "secrets": null,
  "tasks": [
    {"type": "msg", "detail": "Tell the user: starting work on JWT authentication.",
     "skill": null, "args": null, "expect": null, "review": null, "model": null},
    {"type": "skill", "detail": "Add JWT auth module",
     "skill": "aider", "args": "{\"message\": \"create JWT auth module with /login and /logout endpoints\"}",
     "expect": "auth module created with login endpoint and JWT middleware", "review": true, "model": null},
    {"type": "exec", "detail": "python -m pytest tests/",
     "skill": null, "args": null,
     "expect": "all tests pass", "review": true, "model": null},
    {"type": "msg", "detail": "We added JWT auth with /login, /logout, and jwt_required middleware. Tests pass. Summarize for the user.",
     "skill": null, "args": null, "expect": null, "review": null, "model": null}
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
     "expect": "relevant search results about fly.io deployment", "review": true, "model": null},
    {"type": "msg", "detail": "The user wants to deploy on fly.io. Based on the search results: [search output will be here]. Write a clear summary of the deployment steps.",
     "skill": null, "args": null, "expect": null, "review": null, "model": null}
  ]
}
```

**2. Task templates** as reference patterns (not forced, just suggested):

```
Common patterns:
- Code change: msg → skill(aider, review) → exec(test, review) → msg
- Research: skill(search, review) → msg
- Simple question: msg
- Multi-step build: msg → exec(setup) → skill(aider, review) → exec(test, review) → msg
```

**3. Rules** — the expected JSON format, available task types, available skills with args schemas, caller role, and these constraints:
- Task `detail` must be self-contained — the worker does not see the conversation
- The last task must be `type: "msg"` — the user always gets a final response
- Every task with `review: true` must have an `expect` field
- `msg` tasks are the only way to communicate with the user
- If the user mentions credentials (API keys, tokens, passwords), extract them into the `secrets` array

### Task Fields

All fields are always present in the JSON output (strict mode requires it). The "Non-null when" column indicates when the field must have a meaningful value; otherwise it is `null`.

| Field | Non-null when | Description |
|---|---|---|
| `type` | always | `exec`, `msg`, `skill` |
| `detail` | always | What to do. For `msg` tasks, must include all context the worker needs. For `exec` tasks, the shell command. |
| `expect` | `review` is `true` | Semantic success criteria (e.g. "tests pass", not exact output). |
| `model` | optional | Role name to use that role's configured model for `msg` tasks (e.g. `"reviewer"` for a stronger model). Ignored on `exec` and `skill` tasks. Valid values: `planner`, `reviewer`, `worker`, `summarizer`. |
| `skill` | `type` is `skill` | Skill name. |
| `args` | `type` is `skill` | Skill arguments as a JSON string. Kiso parses and validates against `kiso.toml` schema. |
| `review` | optional | `true` to have the reviewer evaluate this task's output. `null` or `false` means no review. |

### Output Fields

- `goal`: high-level objective for the entire process. The reviewer uses it to evaluate individual tasks in context.
- `secrets`: always present. `null` when no credentials; array of `{key, value}` pairs when the user mentioned them. Example with non-null secrets:

```json
"secrets": [{"key": "github_token", "value": "ghp_abc123"}]
```

---

## Reviewer

**When**: after execution of a task that has `"review": true`.

**Input**: process goal + task detail + task expect + task output + original user message.

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
| `"replan"` | Output is wrong, strategy needs revision | Notify user, discard remaining tasks, call planner with full context |

No "local fix" status — the planner replans with full context and decides whether to make a small correction or complete rework. One recovery mechanism, one depth counter.

### Fields

- `learn`: optional free-form string. Stored as new entry in `store.facts` (global, all sessions).
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

**Input**: facts (global) + session summary + task detail.

**Output**: free-form text. No structured output required.

### Why the Worker Doesn't See the Conversation

Deliberate design choice:

1. **Focus.** The planner already interpreted the conversation. Each task has a self-contained `detail` — the worker doesn't need to re-interpret raw messages.
2. **Cost.** The planner pays the conversation-tokens cost once. The worker (called multiple times per plan) stays cheap.
3. **Separation.** Planner reasons about *what* to do; worker does it. Mixing both would degrade each — the worker would second-guess the plan, the planner's instructions would compete with raw messages.
4. **Predictability.** Behavior depends only on (facts + summary + detail). No hidden context, easier to debug.

If `detail` lacks context, the reviewer catches it and triggers a replan.

---

## Summarizer

**When**: after queue completion, if raw messages > `summarize_threshold`. Also when facts exceed `knowledge_max_facts`.

**For messages**: takes current session summary + oldest messages → updated summary (overwrites `sessions.summary`).

**For facts**: takes all fact entries → consolidates into fewer entries (merges duplicates, removes outdated). Replaces old rows in `store.facts`.

**Output**: free-form text. No structured output required.

**Prompt** (`roles/summarizer.md`): preserve facts, decisions, and technical context. Discard noise and redundancy.

---

## Scalability Note

Each session gets its own asyncio worker. Workers are lightweight (just a loop draining a queue), and the real bottleneck is LLM API latency and subprocess execution — not the workers themselves. For deployments with hundreds of concurrent sessions, consider a worker pool with a shared queue instead of per-session workers.
