# LLM Roles

Kiso makes 4 distinct types of LLM calls. Each type has its own model (from `config.toml`), its own system prompt (from `~/.kiso/roles/{role}.md`), and receives **only the context it needs**.

## Context per Role

| Context piece | Planner | Reviewer | Worker | Summarizer |
|---|---|---|---|---|
| Session summary | yes | - | yes | yes (old) |
| Last N raw messages | yes | - | yes | - |
| New message | yes | - | - | - |
| Facts (from store.facts) | yes | - | yes | - |
| Skill summaries + args schemas | yes | - | - | - |
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
- The **planner** gets the big picture to decide what to do. It generates a `goal` (process-level objective) and an `expect` (success criteria) for each reviewed task. It sees skill summaries and args schemas to generate correct invocations.
- The **reviewer** gets the task, its output, the expected outcome, and the process goal — enough to evaluate whether the task succeeded in context, not just in isolation.
- The **worker** gets conversation context to generate relevant text, not skills or role info.
- The **summarizer** gets only what it needs to compress.

---

## Planner

**When**: a new message arrives on a session.

**Input**: facts + session summary + last N raw messages + new message + skill summaries and args schemas + caller role.

**Output**: JSON with a `goal`, optional `secrets`, and a `tasks` list.

**Prompt** (`roles/planner.md`): tells it who it is, how to reason, the expected JSON format, available task types, and skills with their args schemas. Tells it the caller's role so it plans appropriate tasks. Must always end the task list with a `msg` task with `notify: true`.

**Example output:**

```json
{
  "goal": "Add JWT authentication with login endpoint, middleware, and tests",
  "secrets": {"github_token": "ghp_abc123"},
  "tasks": [
    {"type": "msg",   "detail": "Starting work on JWT auth", "notify": true},
    {"type": "skill", "skill": "aider", "args": {"message": "create /health endpoint"},
     "expect": "auth module created with login endpoint and JWT middleware",
     "review": true},
    {"type": "exec",  "detail": "python -m pytest tests/test_auth.py",
     "expect": "all tests pass, exit code 0",
     "review": true},
    {"type": "msg",   "detail": "Summarize what was done and reply to the user", "notify": true}
  ]
}
```

`goal` is the high-level objective for the entire process. The reviewer uses it to evaluate individual tasks in the context of the bigger picture.

`secrets` is optional. Only present when the user mentioned credentials in the message.

**Task fields:**

| Field | Required | Description |
|---|---|---|
| `type` | yes | `exec`, `msg`, `skill` |
| `detail` | yes | What to do |
| `expect` | **yes** if `review: true` | Success criteria for this task. Semantic, not literal (e.g. "tests pass" not exact output). Required for all reviewed tasks — a reviewer without criteria is useless. |
| `model` | no | Role name to use that role's model (e.g. `"reviewer"` to use the reviewer model for a cheap msg task) |
| `skill` | if type=skill | Skill name |
| `args` | if type=skill | Arguments for the skill (validated against kiso.toml schema) |
| `notify` | no | If `true`, output is sent to the webhook |
| `review` | no | If `true`, the reviewer evaluates this task's output. Default `false` |

**On parse failure**: kiso sends the broken response back to the planner asking it to fix the JSON. This retry loop runs up to `max_parse_retries` times (default 3, configurable in `config.toml`). Each retry includes the previous broken output and the parse error. If all retries are exhausted, kiso marks the message as failed and notifies the user: "Planning failed: could not parse planner response after {n} attempts." No silent fallback, no fake response.

---

## Reviewer

**When**: after execution of a task that has `"review": true`.

**Input**: process goal + task detail + task expect + task output + original user message.

**Output**: JSON with `status`, optional `inject`, optional `reason`, and optional `learn`.

**Prompt** (`roles/reviewer.md`): evaluates whether the task output meets the expected criteria (`expect`) in the context of the process goal. Three possible outcomes: approve, fix locally, or escalate to replanning.

**Status values:**

| Status | Meaning | Effect |
|---|---|---|
| `"ok"` | Task output meets expectations | Proceed to next task |
| `"needs_fix"` | Output is wrong but recoverable locally | Inject corrective tasks, continue |
| `"replan"` | Strategy is broken, local fixes won't help | Notify user, discard remaining tasks, call planner with full context |

**Example — local fix:**

```json
{
  "status": "needs_fix",
  "inject": [
    {"type": "exec", "detail": "uv pip install pytest"},
    {"type": "exec", "detail": "python -m pytest",
     "expect": "all tests pass, exit code 0",
     "review": true}
  ],
  "learn": "Project uses pytest for testing"
}
```

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

- `learn`: free-form string. Stored as a new entry in `store.facts`. See [database.md](database.md).
- `reason`: required when status is `"replan"`. Explains why local fixes are insufficient. Included in the notification to the user and in the replanner context.
- **Max depth**: after `max_review_depth` inject rounds in the same chain, the worker stops calling the reviewer and moves on.
- **Max replan depth**: after `max_replan_depth` replan cycles for the same original message, the worker stops replanning, notifies the user of the failure, and moves on.

### Replan Flow

When the reviewer returns `"replan"`:

1. **Notify the user** — a `msg` task with `notify: true` is sent automatically, informing the user that a replan is happening and why (using `reason`).
2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, summary, messages, skills, role, original message)
   - `completed`: list of tasks already executed with their outputs
   - `remaining`: list of tasks that were planned but not yet executed
   - `failure`: the failed task, its output, and the reviewer's `reason`
   - `replan_history`: list of previous replan attempts for this message (each with its goal, failure reason, and what was tried). This prevents the planner from repeating the same mistakes.
3. The planner produces a new `goal` and `tasks` list, replacing the old remaining tasks.
4. Execution continues with the new task list.

---

## Worker

**When**: executing `msg` type tasks (text generation).

**Input**: facts + session summary + last N raw messages + task detail.

**Output**: free-form text.

**Prompt** (`roles/worker.md`): base prompt for text generation and conversation. Skills handle specialized work (coding, search, etc.) — the worker focuses on communication.

---

## Summarizer

**When**: after queue completion, if raw messages > `summarize_threshold`. Also when facts exceed `knowledge_max_facts`.

**For messages**: takes current session summary + oldest messages → updated summary (overwrites `sessions.summary`).

**For facts**: takes all fact entries → consolidates into fewer entries (merges duplicates, removes outdated). Replaces old rows in `store.facts`.

**Prompt** (`roles/summarizer.md`): preserve facts, decisions, and technical context. Discard noise and redundancy.
