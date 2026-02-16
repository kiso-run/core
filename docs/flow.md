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

Uses structured output (`response_format` with strict JSON schema — see [llm-roles.md — Planner](llm-roles.md#planner) for the full schema). The provider guarantees valid JSON at decoding level.

Returns JSON with:
- `goal`: high-level objective for the entire process. Stored for the reviewer and potential replan cycles.
- `secrets`: `{key, value}` pairs or `null`. If present, stored in `store.secrets` before task execution.
- `tasks`: each task with `review: true` must include an `expect` field with semantic success criteria.

### c) Validates the Plan

Before execution, kiso validates the plan semantically (see [llm-roles.md — Validation After Parsing](llm-roles.md#validation-after-parsing) for the full rule list and error example). On failure, retries up to `max_validation_retries` (default 3) with specific error feedback. If exhausted: fail the message, notify user. No silent fallback.

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

Every `msg` task output is delivered: POSTed to webhook (if set) and available via `GET /status/{session}`. `final: true` on the last `msg` task in the plan. Only `msg` tasks are delivered — `exec` and `skill` outputs are internal.

If the webhook POST fails, kiso logs it and continues. Outputs remain available via `/status`. No retries. See [api.md — Webhook Callback](api.md#webhook-callback) for the payload format.

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

1. **Summarize messages**: if `len(raw_messages) >= summarize_threshold`, calls Summarizer (current summary + oldest messages → new summary → `store.sessions.summary`).
2. **Consolidate facts**: if facts exceed `knowledge_max_facts`, calls Summarizer to merge/deduplicate in `store.facts`. See [Facts Lifecycle](#facts-lifecycle).
3. **Wait or shutdown**: worker waits on session queue. After `worker_idle_timeout` seconds idle, shuts down (respawned on next message).

## 4. New Message on the Same Session

If a message arrives while the worker is executing tasks, it gets queued. When the worker finishes the current queue, it picks up the next message and restarts from step 2.

---

## Facts Lifecycle

### How Facts Are Created

| Source | When | Example |
|---|---|---|
| Reviewer (`learn` field) | After reviewing a task | `"Project framework is Flask, not FastAPI"` |
| Summarizer (consolidation) | When facts exceed `knowledge_max_facts` | Merges `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"` |
| Manual | Admin adds via DB or future API | `"Conventions: snake_case, type hints"` |

### Where Facts Are Used

Planner and Worker receive facts in their context. Reviewer and Summarizer do **not** (see [llm-roles.md — Context per Role](llm-roles.md#context-per-role) for the full matrix).

### Facts Are Global

All facts are visible to all sessions. The `session` column is **provenance only** (which session generated it, not where it's visible).

### Consolidation

When facts exceed `knowledge_max_facts` (default 50), the Summarizer reads all facts, merges duplicates (e.g. `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"`), removes outdated ones, and replaces old rows with fewer consolidated entries.

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
