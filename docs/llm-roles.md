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

**Output**: JSON with a `goal`, optional `secrets`, and a `tasks` list.

### Structured Output (required)

The planner call uses `response_format` with a strict JSON schema:

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
                    "type": "object",
                    "additionalProperties": {"type": "string"}
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["exec", "msg", "skill"]},
                            "detail": {"type": "string"},
                            "skill": {"type": "string"},
                            "args": {"type": "object"},
                            "expect": {"type": "string"},
                            "review": {"type": "boolean"},
                            "model": {"type": "string"}
                        },
                        "required": ["type", "detail"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["goal", "tasks"],
            "additionalProperties": False
        }
    }
}
```

This guarantees valid JSON from the provider at the decoding level. **No parse retries needed.** If the provider does not support `response_format` with `json_schema`, the call fails with a clear error:

```
Provider "ollama" does not support structured output.
Planner and Reviewer require it. Route these roles to a compatible provider
(e.g. models.planner = "openrouter:moonshotai/kimi-k2.5").
```

No silent fallback, no manual JSON parsing. Structured output is a hard requirement for Planner and Reviewer. Worker and Summarizer produce free-form text and have no such constraint.

### Validation After Parsing

The JSON is guaranteed valid by the provider, but kiso still validates the **semantics** before execution:

1. Every task with `review: true` must have an `expect` field
2. The last task must be `type: "msg"` (the user always gets a final response)
3. Every `skill` reference must exist in the installed skills list
4. Every `skill` task's `args` must match the skill's schema from `kiso.toml`
5. No empty `tasks` list

If validation fails, kiso sends the plan back to the planner with the specific error, up to `max_validation_retries` times (default 3). Example:

```
Your plan has errors:
- Task 2: skill "aider" requires arg "message" (string, required) but it's missing
- Task 4: has review=true but no expect field
Fix these and return the corrected plan.
```

If all retries are exhausted: fail the message, notify the user. No silent fallback.

### Prompt Design

**System prompt** (`roles/planner.md`) includes:

**1. Few-shot examples.** Two complete plan examples covering different scenarios:

```
Example 1 — coding task:
User: "add JWT authentication"
{
  "goal": "Add JWT auth with login endpoint, middleware, and tests",
  "tasks": [
    {"type": "msg", "detail": "Tell the user: starting work on JWT authentication."},
    {"type": "skill", "skill": "aider", "args": {"message": "create JWT auth module with /login and /logout endpoints and jwt_required middleware"},
     "expect": "auth module created with login endpoint and JWT middleware", "review": true},
    {"type": "exec", "detail": "python -m pytest tests/",
     "expect": "all tests pass", "review": true},
    {"type": "msg", "detail": "We added JWT auth with /login, /logout, and jwt_required middleware. Tests pass. Summarize for the user."}
  ]
}

Example 2 — research task:
User: "find out how to deploy on fly.io"
{
  "goal": "Research fly.io deployment and summarize for the user",
  "tasks": [
    {"type": "skill", "skill": "search", "args": {"query": "fly.io python deployment guide"},
     "expect": "relevant search results about fly.io deployment", "review": true},
    {"type": "msg", "detail": "The user wants to deploy on fly.io. Based on the search results: [search output will be here]. Write a clear summary of the deployment steps."}
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

### Task Fields

| Field | Required | Description |
|---|---|---|
| `type` | yes | `exec`, `msg`, `skill` |
| `detail` | yes | What to do. For `msg` tasks, must include all context the worker needs to generate a good response. For `exec` tasks, the shell command. |
| `expect` | **yes** if `review: true` | Semantic success criteria (e.g. "tests pass", not exact output). |
| `model` | no | Role name to override the model for `msg` tasks only (e.g. `"reviewer"` to use the reviewer's stronger model). Ignored on `exec` and `skill` tasks. Valid values: `planner`, `reviewer`, `worker`, `summarizer`. |
| `skill` | if type=skill | Skill name |
| `args` | if type=skill | Arguments for the skill (validated against kiso.toml schema) |
| `review` | no | If `true`, the reviewer evaluates this task's output. Default `false`. |

### Example Output

```json
{
  "goal": "Add JWT authentication with login endpoint, middleware, and tests",
  "secrets": {"github_token": "ghp_abc123"},
  "tasks": [
    {"type": "msg",   "detail": "Tell the user: starting work on JWT authentication with login/logout endpoints and middleware."},
    {"type": "skill", "skill": "aider", "args": {"message": "create JWT auth module with /login and /logout endpoints and jwt_required middleware"},
     "expect": "auth module created with login endpoint and JWT middleware",
     "review": true},
    {"type": "exec",  "detail": "python -m pytest tests/test_auth.py",
     "expect": "all tests pass, exit code 0",
     "review": true},
    {"type": "msg",   "detail": "The user asked for JWT auth. We created auth module with /login, /logout, and jwt_required middleware. All 3 tests pass. Summarize this for the user."}
  ]
}
```

`goal` is the high-level objective for the entire process. The reviewer uses it to evaluate individual tasks in context.

`secrets` is optional. Present only when the user mentioned credentials in the message.

---

## Reviewer

**When**: after execution of a task that has `"review": true`.

**Input**: process goal + task detail + task expect + task output + original user message.

**Output**: JSON (via structured output, same as planner) with `status`, optional `reason`, and optional `learn`.

### Structured Output (required)

Same as the planner — the reviewer call uses `response_format` with a strict JSON schema:

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
                "reason": {"type": "string"},
                "learn": {"type": "string"}
            },
            "required": ["status"],
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

There is no "local fix" status. When something fails, the planner replans with full context — it decides whether the new plan is a small correction or a complete rework. One recovery mechanism, one depth counter.

**Example — everything fine:**

```json
{
  "status": "ok"
}
```

**Example — replan needed:**

```json
{
  "status": "replan",
  "reason": "The project uses Flask, not FastAPI. The entire approach to adding middleware needs to change.",
  "learn": "Project framework is Flask, not FastAPI"
}
```

- `learn`: free-form string, optional. Stored as a new entry in `store.facts` (global). Persists across all sessions.
- `reason`: expected when status is `"replan"`. Explains why the task failed. Included in the notification to the user and in the replanner context. The JSON schema doesn't enforce this conditionally — semantic validation catches `replan` without `reason` and retries.
- **Max replan depth**: after `max_replan_depth` replan cycles for the same original message, the worker stops replanning, notifies the user of the failure, and moves on.

### Replan Flow

When the reviewer returns `"replan"`:

1. **Notify the user** — the worker sends an automatic `msg` to the webhook explaining that a replan is happening and why (using `reason`).

2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, summary, messages, skills, role, original message)
   - `completed`: list of tasks already executed with their outputs
   - `remaining`: list of tasks that were planned but not yet executed
   - `failure`: the failed task, its output, and the reviewer's `reason`
   - `replan_history`: list of previous replan attempts for this message (each with its goal, failure reason, and what was tried) — so the planner doesn't repeat the same mistakes

3. The planner produces a new `goal` and `tasks` list, replacing the old remaining tasks.

4. Execution continues with the new task list.

---

## Worker

**When**: executing `msg` type tasks (text generation).

**Input**: facts (global) + session summary + task detail.

**Output**: free-form text. No structured output required.

### Why the Worker Doesn't See the Conversation

This is a deliberate design choice, not a limitation:

1. **Focus.** The worker generates text based on clear instructions. It doesn't need to interpret raw conversation — the planner already did that. Each task has a self-contained `detail` with everything needed.

2. **Cost.** Conversation history is tokens. The planner pays that cost once and distills it into focused task details. The worker (potentially called multiple times per plan) stays cheap.

3. **Separation of concerns.** The planner reasons about *what* to do. The worker does it. Mixing the two would make both worse — the worker would second-guess the plan, the planner's instructions would compete with raw messages.

4. **Predictability.** The worker's behavior depends only on (facts + summary + detail). No hidden context, no surprises from earlier messages. Easier to debug, easier to review.

If the planner fails to include enough context in `detail`, the result will be poor — and the reviewer will catch it and trigger a replan. The validation step (see Planner section) also checks that `detail` is non-empty.

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
