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
3. Checks if `user` is in `config.users` whitelist (direct name or alias match — see [security.md](security.md)). If not: saves message to `store.messages` (audit), responds `202 Accepted`, but does **not** enqueue or process. Stops here.
4. Resolves the Linux username, role, and allowed skills from `config.users.{user}`
5. Creates or updates the session in `store.sessions` (stores webhook URL)
6. Saves the message to `store.messages`
7. Enqueues `{message, role, allowed_skills}` in the session's in-memory queue
8. Responds `202 Accepted`

If no worker exists for that session, it spawns one.

## 2. Worker Picks Up the Message

The worker is an asyncio loop per session. When it finds a message:

### a) Builds Planner Context

Only what the planner needs (see [llm-roles.md](llm-roles.md)):
- Facts (global, from `store.facts`)
- Session summary (from `store.sessions.summary`)
- Last `context_messages` raw messages (default 5, from `store.messages`)
- Recent msg outputs (all `msg` task outputs since last summarization, from `store.tasks`)
- Skill summaries and args schemas (only skills allowed for this user, from `kiso.toml`, rescanned on each planner call)
- Caller role (admin | user)
- New message

### b) Calls the Planner

Uses structured output (`response_format` with strict JSON schema). The provider guarantees valid JSON at the decoding level — no parse retries needed.

The LLM returns JSON with a `goal`, `secrets` (nullable), and a `tasks` list.

- `goal`: the high-level objective for the entire process (e.g. "Add JWT authentication with login endpoint, middleware, and tests"). Stored for the reviewer and potential replan cycles.
- `secrets`: array of `{key, value}` pairs, or `null`. If present, the worker stores them in `store.secrets` before executing tasks.
- `tasks`: each task with `review: true` must include an `expect` field with semantic success criteria.

### c) Validates the Plan

Before execution, kiso validates the plan programmatically:

1. Every task with `review: true` has an `expect` field
2. The last task is `type: "msg"` (the user always gets a final response)
3. Every `skill` reference exists in the installed skills
4. Every `skill` task's `args` is valid JSON and matches the skill's schema from `kiso.toml`
5. The `tasks` list is not empty

If validation fails, kiso sends the plan back to the planner with specific errors, up to `max_validation_retries` times (default 3). If all retries are exhausted, kiso marks the message as failed and notifies the user. No silent fallback.

All validated tasks are persisted to `store.tasks` with status `pending`.

### d) Executes Tasks One by One

For each task (status updated to `running` in DB):

| Type | Execution |
|---|---|
| `exec` | `asyncio.create_subprocess_shell(...)` with `cwd=~/.kiso/sessions/{session}`, timeout from config. Admin: full access. User: restricted to session workspace. Clean env (only PATH). Captures stdout+stderr. |
| `msg` | Calls LLM with `worker` role (or override via the task's `model` field). Context: facts + session summary + task detail. The worker does **not** see conversation messages — the planner provides all necessary context in the task `detail` field (see [llm-roles.md — Why the Worker Doesn't See the Conversation](llm-roles.md#why-the-worker-doesnt-see-the-conversation)). |
| `skill` | Validates args against `kiso.toml` schema. Pipes input JSON to stdin: `.venv/bin/python ~/.kiso/skills/{name}/run.py`. Input: args + session + workspace + scoped secrets (only those declared in `kiso.toml`). Output: stdout. |

Output is sanitized (known secret values stripped) before any further use. Task status and output are persisted to `store.tasks` (`done` or `failed`).

### e) Delivers msg Tasks

Every `msg` task output is delivered to the user:

- If the session has a webhook: POST the output to the webhook URL
- Always available via `GET /status/{session}`

```json
{
  "session": "dev-backend",
  "task_id": 42,
  "type": "msg",
  "content": "Added JWT auth. Tests passing.",
  "final": false
}
```

`final: true` on the last `msg` task in the plan.

Only `msg` tasks are delivered. `exec` and `skill` outputs are internal — the planner adds `msg` tasks wherever it wants to communicate with the user.

If the webhook POST fails, kiso logs the failure and continues execution. Task outputs remain available via `/status`. No retries.

### f) Reviewer Evaluates (if review: true)

Only for tasks with `"review": true`. Reviewer context: process goal + task detail + task expect + task output + original user message.

Uses structured output (same as planner). Two outcomes:

- `status: "ok"` → proceed to next task
- `status: "replan"` → triggers the replan flow (see below)

`learn` → if present in any outcome, stored as a new entry in `store.facts` (global, visible across all sessions).

### g) Replan Flow (if reviewer returns "replan")

When the reviewer determines that the task failed and the plan needs revision:

1. **Notify the user**: the worker sends an automatic webhook message explaining that a replan is happening and why (using the reviewer's `reason`).

2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, summary, messages, skills, role, original message)
   - **completed**: tasks already executed, with their outputs (from `store.tasks`)
   - **remaining**: tasks that were planned but not yet executed
   - **failure**: the failed task, its output, and the reviewer's `reason`
   - **replan_history**: previous replan attempts for this message (goal, failure, what was tried) — so the planner doesn't repeat the same mistakes

3. The planner produces a new `goal` and `tasks` list. The old remaining tasks are marked `failed` in DB. New tasks go through validation (step c) again.

4. Execution continues with the new task list. Even on replan, the planner must produce at least one task (typically a `msg` task summarizing the situation).

**Max replan depth**: after `max_replan_depth` replan cycles for the same original message, the worker stops replanning, notifies the user of the failure, and moves on.

## 3. Post-Execution

After draining the task list:

### a) Summarize Messages

If `len(raw_messages) >= summarize_threshold`, calls the **Summarizer**: current summary + oldest messages → new summary → writes to `store.sessions.summary`.

### b) Consolidate Facts

If global facts count exceeds `knowledge_max_facts`, calls the **Summarizer** to consolidate → merges/deduplicates entries in `store.facts`. See [Facts Lifecycle](#facts-lifecycle).

### c) Wait or Shutdown

The worker goes back to waiting on the session queue. If no messages arrive within `worker_idle_timeout` seconds, the worker shuts down (respawned on next message).

## 4. New Message on the Same Session

If a message arrives while the worker is executing tasks, it gets queued. When the worker finishes the current queue, it picks up the next message and restarts from step 2.

---

## Facts Lifecycle

Facts are persistent knowledge that lives across all sessions. They help the planner and worker make better decisions over time.

### How Facts Are Created

| Source | When | Example |
|---|---|---|
| Reviewer (`learn` field) | After reviewing a task | `"Project framework is Flask, not FastAPI"` |
| Summarizer (consolidation) | When facts exceed `knowledge_max_facts` | Merges `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"` |
| Manual | Admin adds via DB or future API | `"Conventions: snake_case, type hints"` |

### Where Facts Are Used

| Consumer | How | Why |
|---|---|---|
| Planner | Included in context on every call | So it plans with knowledge of the project/environment |
| Worker | Included in context for `msg` tasks | So it responds to the user with accurate context |
| Reviewer | **Not included** | The reviewer evaluates a specific task output against specific criteria — facts would add noise |
| Summarizer | **Not included** | The summarizer compresses messages, doesn't need facts to do it |

### Facts Are Global

All facts are visible to all sessions. The `session` column in `store.facts` is **provenance only** — it records which session generated the fact, not which sessions can see it.

A fact like `"Project uses Flask 2.3"` learned in session `dev-backend` is equally useful in session `discord-general`.

### Consolidation

When facts exceed `knowledge_max_facts` (default 50), the Summarizer:
1. Reads all facts
2. Merges duplicates (e.g. `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"`)
3. Removes outdated facts (superseded by newer ones)
4. Replaces old rows with fewer consolidated entries

This keeps the facts list lean and relevant. The planner and worker always see the full list.

---

## Diagram

```
POST /msg ──> auth (token) ──> whitelist? ──no──> save msg (audit only)
                                    │
                                   yes
                                    │
                              resolve role ──> save msg ──> queue(session)
                                                                        │
                                                                        ▼
                                                                 worker(session)
                                                                        │
                                                          ┌─────────────┤
                                                          ▼             │
                                                   build planner ctx    │
                                                   (facts, summary,     │
                                                    last N msgs,        │
                                                    msg outputs,        │
                                                    skills + schemas,   │
                                                    role, new msg)      │
                                                          │             │
                                                          ▼             │
                                                      planner           │
                                                  (structured output)   │
                                                     │    │     secrets?│
                                                  goal  tasks   store   │
                                                     │    │         │   │
                                                     ▼    ▼         │   │
                                                  validate plan     │   │
                                                  (semantic checks) │   │
                                                     │  ╳ fail?     │   │
                                                     │  → retry     │   │
                                                     │  (max 3)  ◄──┘   │
                                                     │                  │
                                                     ▼                  │
                                                  persist tasks         │
                                                  (pending)             │
                                                     │                  │
                                            ┌────────┼────────┐        │
                                            ▼        ▼        ▼        │
                                          exec     msg      skill      │
                                         (shell)  (llm)   (subproc)    │
                                         admin:            validate    │
                                         full     │     args from     │
                                         user:    │     kiso.toml     │
                                         sandbox  │     scoped        │
                                            │     │     secrets       │
                                            └──┬──┘────────┘          │
                                               │ sanitize output       │
                                               │ persist to DB         │
                                               │ (done | failed)       │
                                               ▼                       │
                                        type = msg? ──yes──> deliver   │
                                               │             (webhook  │
                                               │              + store) │
                                               ▼                       │
                                        review: true? ──no──┐          │
                                               │            │          │
                                              yes           │          │
                                               │            │          │
                                               ▼            │          │
                                           reviewer         │          │
                                          (structured       │          │
                                           output)          │          │
                                          │         │       │          │
                                     ok ──┘       replan    │          │
                                     │         │    │       │          │
                                     │     learn?   │       │          │
                                     │       │      │       │          │
                                     │       ▼      │       │          │
                                     │    store     │       │          │
                                     │     fact     │       │          │
                                     │    (global)  │       │          │
                                     │              ▼       │          │
                                     │         notify       │          │
                                     │         user +       │          │
                                     │         replan ──────┘          │
                                     │         (planner with           │
                                     │          completed +            │
                                     │          remaining +            │
                                     │          failure)               │
                                     │                                 │
                                     ◄─────────────────────────────────┤
                                     │                                 │
                                     ▼                                 │
                                  more tasks?                          │
                                    │     │                            │
                                   yes    no                           │
                                    │     │                            │
                                    ▼     ├──> summarize msgs?         │
                               next task  ├──> consolidate facts?      │
                                          └──> wait/shutdown ──────────┘
```
