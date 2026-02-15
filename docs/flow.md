# Full Message Flow

## 1. Message Reception

```
POST /msg
Authorization: Bearer <api_token>
{
  "session": "dev-backend",
  "user": "marco",
  "content": "add JWT authentication",
  "webhook": "https://example.com/hook"
}
```

`main.py` does:
1. Validates the bearer token (401 if invalid)
2. Resolves user role: admin if `user` is in `config.admins`, otherwise user
3. Creates or updates the session in `store.sessions` (stores webhook URL)
4. Saves the message to `store.messages`
5. Enqueues `{message, role}` in the session's in-memory queue
6. Responds `202 Accepted`

If no worker exists for that session, it spawns one.

## 2. Worker Picks Up the Message

The worker is an asyncio loop per session. When it finds a message:

### a) Builds Planner Context

Only what the planner needs (see [llm-roles.md](llm-roles.md)):
- Facts (from `store.meta["facts"]`)
- Session summary (from `store.sessions.summary`)
- Last N raw messages (from `store.messages`)
- Skill one-liners (from `kiso.toml` of each skill in `~/.kiso/skills/`, rescanned on each planner call)
- Caller role (admin | user)
- New message

### b) Calls the Planner

The LLM returns JSON with a `goal`, optional `secrets`, and a `tasks` list.

- `goal`: the high-level objective for the entire process (e.g. "Add JWT authentication with login endpoint, middleware, and tests"). Stored for the reviewer and potential replan cycles.
- `secrets`: if present, the worker stores them in `store.secrets` before executing tasks.
- `tasks`: each task may include an `expect` field with semantic success criteria (e.g. "all tests pass, exit code 0").

If JSON parsing fails, retries once. If it fails again, falls back to `{"tasks": [{"type": "msg", "detail": "reply to the user", "notify": true}]}`.

The planner only generates task types allowed for the caller's role.

### c) Executes Tasks One by One

For each task:

| Type | Execution |
|---|---|
| `exec` | `asyncio.create_subprocess_shell(...)` with `cwd=~/.kiso/sessions/{session}`, timeout from config, clean env (only PATH). Captures stdout+stderr. |
| `msg` | Calls LLM with `worker` role. Context: facts + summary + recent messages + task detail. |
| `skill` | Runs `.venv/bin/python run.py < input.json` as subprocess. Input: args + session + workspace + scoped secrets (only those declared in `kiso.toml`). Output: stdout. |

Output is sanitized (known secret values stripped) before any further use. Each task's status and output are persisted to `store.tasks` (see [database.md](database.md)).

### d) Reviewer Evaluates (if review: true)

Only for tasks with `"review": true`. Reviewer context: process goal + task detail + task expect + task output + original user message.

Three possible outcomes:

- `status: "ok"` → proceed to next task
- `status: "needs_fix"` → tasks in `inject` are inserted right after in the list (local correction)
- `status: "replan"` → triggers the replan flow (see below)

`learn` → if present in any outcome, appended to `store.meta["facts"]`.

**Max depth**: after `max_review_depth` inject rounds in the same chain, the worker stops calling the reviewer and moves on.

### e) Replan Flow (if reviewer returns "replan")

When the reviewer determines that local fixes are insufficient and the strategy itself is wrong:

1. **Notify the user**: the worker automatically sends a notification via webhook explaining that a replan is happening and why (using the reviewer's `reason`).

2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, summary, messages, skills, role, original message)
   - **completed**: tasks already executed, with their outputs
   - **remaining**: tasks that were planned but not yet executed
   - **failure**: the failed task, its output, and the reviewer's `reason`
   - **replan_history**: previous replan attempts for this message (goal, failure, what was tried) — so the planner doesn't repeat the same mistakes

3. The planner produces a new `goal` and `tasks` list. The old remaining tasks are replaced.

4. Execution continues with the new task list.

**Max replan depth**: after `max_replan_depth` replan cycles for the same original message, the worker stops replanning, notifies the user of the failure, and moves on.

### f) Notify if Requested

If the task has `notify: true`, POSTs to the session's webhook:

```json
{
  "session": "dev-backend",
  "task_id": 42,
  "type": "msg",
  "content": "Added JWT auth. Tests passing.",
  "final": false
}
```

`final: true` on the last task with `notify: true` in the queue.

## 3. Post-Execution

After draining the task list:

1. If `len(raw_messages) > summarize_threshold`, calls the **Summarizer**: current summary + oldest messages → new summary → writes to `store.sessions.summary`.
2. If facts text exceeds `knowledge_max_lines`, calls the **Summarizer** to consolidate → overwrites `store.meta["facts"]`.
3. The worker goes back to waiting on the session queue.
4. If no messages arrive within `worker_idle_timeout` seconds, the worker shuts down (respawned on next message).

## 4. New Message on the Same Session

If a message arrives while the worker is executing tasks, it gets queued. When the worker finishes the current queue, it picks up the next message and restarts from step 2.

## Diagram

```
POST /msg ──> auth ──> resolve role ──> save msg ──> queue(session)
                                                          │
                                                          ▼
                                                   worker(session)
                                                          │
                                            ┌─────────────┤
                                            ▼             │
                                     build planner ctx    │
                                     (facts, summary,     │
                                      messages, skills,   │
                                      role, new msg)      │
                                            │             │
                                            ▼             │
                                        planner ──────┐   │
                                       │    │     secrets? │
                                    goal  tasks   store    │
                                       │    │         │   │
                                       ▼    ▼         │   │
                                      task list ◄─────┘   │
                                  (each with expect)      │
                                            │             │
                                   ┌────────┼────────┐    │
                                   ▼        ▼        ▼    │
                                 exec     msg      skill  │
                                (shell)  (llm)   (subproc │
                                   │        │     json→txt)│
                                   └───┬────┘────────┘    │
                                       │ sanitize output  │
                                       ▼                  │
                               review: true? ──no──┐      │
                                       │           │      │
                                      yes          │      │
                                       │           │      │
                                       ▼           │      │
                                   reviewer        │      │
                                  (goal + task     │      │
                                   + expect        │      │
                                   + output        │      │
                                   + user msg)     │      │
                                   │    │    │     │      │
                                   │    │    │     │      │
                              ok ──┘    │  replan  │      │
                              │   needs_fix  │     │      │
                              │    │  │      │     │      │
                              │    │  learn? │     │      │
                              │    │    │    │     │      │
                              │    │    ▼    │     │      │
                              │    │ append  │     │      │
                              │    │  facts  │     │      │
                              │    │         │     │      │
                              │    ▼         ▼     │      │
                              │  inject   notify   │      │
                              │  tasks    user +   │      │
                              │    │      replan ──┘      │
                              │    │      (back to        │
                              │    │       planner with   │
                              │    │       completed +    │
                              │    │       remaining +    │
                              │    │       failure)       │
                              │    │                      │
                              ◄────┴──────────────────────┤
                              │                           │
                              ▼                           │
                       notify? ──yes──> webhook           │
                              │                           │
                              ▼                           │
                       queue empty?                       │
                          │     │                         │
                         no    yes                        │
                          │     │                         │
                          ▼     ├──> summarize msgs?      │
                     next task  ├──> consolidate facts?   │
                                └──> wait/shutdown ───────┘
```
