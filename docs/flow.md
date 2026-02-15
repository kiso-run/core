# Full Message Flow

## 1. Message Reception

```
POST /msg
Authorization: Bearer <token>
{
  "session": "dev-backend",
  "user": "marco",
  "content": "add JWT authentication",
  "webhook": "https://example.com/hook"
}
```

`main.py` does:
1. Validates the bearer token against `config.toml` tokens (401 if no match)
2. Logs which named token was used
3. Resolves user role: admin if `user` is in `config.admins`, otherwise user
4. Creates or updates the session in `store.sessions` (stores webhook URL)
5. Saves the message to `store.messages`
6. Enqueues `{message, role}` in the session's in-memory queue
7. Responds `202 Accepted`

If no worker exists for that session, it spawns one.

## 2. Worker Picks Up the Message

The worker is an asyncio loop per session. When it finds a message:

### a) Builds Planner Context

Only what the planner needs (see [llm-roles.md](llm-roles.md)):
- Facts (from `store.facts`)
- Session summary (from `store.sessions.summary`)
- Last N raw messages (from `store.messages`)
- Skill summaries and args schemas (from `kiso.toml` of each skill in `~/.kiso/skills/`, rescanned on each planner call)
- Caller role (admin | user)
- New message

### b) Calls the Planner

The LLM returns JSON with a `goal`, optional `secrets`, and a `tasks` list.

- `goal`: the high-level objective for the entire process (e.g. "Add JWT authentication with login endpoint, middleware, and tests"). Stored for the reviewer and potential replan cycles.
- `secrets`: if present, the worker stores them in `store.secrets` before executing tasks.
- `tasks`: each task with `review: true` must include an `expect` field with semantic success criteria.

If JSON parsing fails, kiso sends the broken response back to the planner asking it to fix the JSON, up to `max_parse_retries` times (default 3). If all retries are exhausted, kiso marks the message as failed and notifies the user. No silent fallback.

The planner generates task types appropriate for the caller's role (admin: unrestricted exec; user: sandboxed exec).

All tasks are persisted to `store.tasks` with status `pending`.

### c) Executes Tasks One by One

For each task (status updated to `running` in DB):

| Type | Execution |
|---|---|
| `exec` | `asyncio.create_subprocess_shell(...)` with `cwd=~/.kiso/sessions/{session}`, timeout from config. Admin: full access. User: restricted to session workspace. Clean env (only PATH). Captures stdout+stderr. |
| `msg` | Calls LLM with `worker` role. Context: facts + summary + recent messages + task detail. |
| `skill` | Validates args against `kiso.toml` schema. Runs `.venv/bin/python run.py < input.json` as subprocess. Input: args + session + workspace + scoped secrets (only those declared in `kiso.toml`). Output: stdout. |

Output is sanitized (known secret values stripped) before any further use. Task status and output are persisted to `store.tasks` (`done` or `failed`).

### d) Reviewer Evaluates (if review: true)

Only for tasks with `"review": true`. Reviewer context: process goal + task detail + task expect + task output + original user message.

Three possible outcomes:

- `status: "ok"` → proceed to next task
- `status: "needs_fix"` → tasks in `inject` are inserted right after in the list (local correction)
- `status: "replan"` → triggers the replan flow (see below)

`learn` → if present in any outcome, stored as a new entry in `store.facts`.

**Max depth**: after `max_review_depth` inject rounds in the same chain, the worker stops calling the reviewer and moves on.

### e) Replan Flow (if reviewer returns "replan")

When the reviewer determines that local fixes are insufficient and the strategy itself is wrong:

1. **Notify the user**: the worker automatically sends a notification via webhook explaining that a replan is happening and why (using the reviewer's `reason`).

2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, summary, messages, skills, role, original message)
   - **completed**: tasks already executed, with their outputs (from `store.tasks`)
   - **remaining**: tasks that were planned but not yet executed
   - **failure**: the failed task, its output, and the reviewer's `reason`
   - **replan_history**: previous replan attempts for this message (goal, failure, what was tried) — so the planner doesn't repeat the same mistakes

3. The planner produces a new `goal` and `tasks` list. The old remaining tasks are marked `failed` in DB.

4. New tasks are persisted and execution continues.

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
2. If facts count exceeds `knowledge_max_facts`, calls the **Summarizer** to consolidate → merges/deduplicates entries in `store.facts`.
3. The worker goes back to waiting on the session queue.
4. If no messages arrive within `worker_idle_timeout` seconds, the worker shuts down (respawned on next message).

## 4. New Message on the Same Session

If a message arrives while the worker is executing tasks, it gets queued. When the worker finishes the current queue, it picks up the next message and restarts from step 2.

## Diagram

```
POST /msg ──> auth (named token) ──> resolve role ──> save msg ──> queue(session)
                                                                        │
                                                                        ▼
                                                                 worker(session)
                                                                        │
                                                          ┌─────────────┤
                                                          ▼             │
                                                   build planner ctx    │
                                                   (facts, summary,     │
                                                    messages, skills     │
                                                    + args schemas,     │
                                                    role, new msg)      │
                                                          │             │
                                                          ▼             │
                                                      planner ──────┐   │
                                                     │    │     secrets? │
                                                  goal  tasks   store    │
                                                     │    │         │   │
                                                     ▼    ▼         │   │
                                                  persist tasks     │   │
                                                  in store.tasks    │   │
                                                     (pending)  ◄───┘   │
                                                          │             │
                                                 ┌────────┼────────┐    │
                                                 ▼        ▼        ▼    │
                                               exec     msg      skill  │
                                              (shell)  (llm)   (subproc)│
                                              admin:            validate │
                                              full     │     args from  │
                                              user:    │     kiso.toml  │
                                              sandbox  │     scoped     │
                                                 │     │     secrets    │
                                                 └──┬──┘────────┘      │
                                                    │ sanitize output   │
                                                    │ persist to DB     │
                                                    │ (done | failed)   │
                                                    ▼                   │
                                             review: true? ──no──┐      │
                                                    │            │      │
                                                   yes           │      │
                                                    │            │      │
                                                    ▼            │      │
                                                reviewer         │      │
                                               (goal + task      │      │
                                                + expect         │      │
                                                + output         │      │
                                                + user msg)      │      │
                                                │    │    │      │      │
                                           ok ──┘    │  replan   │      │
                                           │   needs_fix  │      │      │
                                           │    │  │      │      │      │
                                           │    │  learn? │      │      │
                                           │    │    │    │      │      │
                                           │    │    ▼    │      │      │
                                           │    │ store   │      │      │
                                           │    │  fact   │      │      │
                                           │    │         │      │      │
                                           │    ▼         ▼      │      │
                                           │  inject   notify    │      │
                                           │  tasks    user +    │      │
                                           │    │      replan ───┘      │
                                           │    │      (planner with    │
                                           │    │       completed +     │
                                           │    │       remaining +     │
                                           │    │       failure)        │
                                           │    │                       │
                                           ◄────┴───────────────────────┤
                                           │                            │
                                           ▼                            │
                                    notify? ──yes──> webhook            │
                                           │                            │
                                           ▼                            │
                                    queue empty?                        │
                                       │     │                          │
                                      no    yes                         │
                                       │     │                          │
                                       ▼     ├──> summarize msgs?       │
                                  next task  ├──> consolidate facts?    │
                                             └──> wait/shutdown ────────┘
```
