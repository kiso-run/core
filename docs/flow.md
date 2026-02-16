# Full Message Flow

## 1. Session Creation

Connectors create sessions explicitly before sending messages:

```
POST /sessions
Authorization: Bearer <token>
{
  "session": "discord_dev",
  "webhook": "http://localhost:9001/callback",
  "description": "Discord #dev channel"
}
```

The CLI does not call this — sessions are created implicitly on first `POST /msg`. See [api.md — POST /sessions](api.md#post-sessions).

## 2. Message Reception

```
POST /msg
Authorization: Bearer <token>
{
  "session": "dev-backend",
  "user": "marco",
  "content": "add JWT authentication"
}
```

`main.py` does:
1. Validates the bearer token against `config.toml` tokens (401 if no match)
2. Logs which named token was used
3. Checks if `user` is in `config.users` whitelist (direct name or alias match — see [security.md](security.md)).
   - **Whitelisted**: resolves Linux username, role, and allowed skills. Message saved with `trusted=1`.
   - **Not whitelisted**: saves message with `trusted=0` (context and audit). Responds `202 Accepted`. Does **not** enqueue or process. Stops here.
4. If session doesn't exist, creates it implicitly (no webhook, no connector metadata)
5. Saves the message to `store.messages` with `processed=0`
6. Enqueues `{message, role, allowed_skills}` in the session's in-memory queue
7. Responds `202 Accepted`

If no worker exists for that session, it spawns one. **The check-and-spawn must be atomic** (no `await` between checking the workers dict and creating the task) to prevent duplicate workers on the same session.

### Message Recovery on Startup

On startup, kiso queries `SELECT * FROM messages WHERE processed=0 AND trusted=1 ORDER BY id` and re-enqueues all unprocessed messages. This prevents silent message loss after a crash.

## 3. Worker Picks Up the Message

The worker is an asyncio loop per session. When it finds a message, it marks it `processed=1` in the DB, then:

### a) Paraphrases Untrusted Messages

If there are untrusted messages (`trusted=0`) in the context window, the worker calls the paraphraser (batch LLM call using the summarizer model) to rewrite them in third person. See [security.md — Prompt Injection Defense](security.md#6-prompt-injection-defense).

### b) Builds Planner Context

Only what the planner needs (see [llm-roles.md](llm-roles.md)):
- Facts (global, from `store.facts`)
- Pending items (global + session, from `store.pending`)
- Session summary (from `store.sessions.summary`)
- Last `context_messages` raw messages (default 5, from `store.messages`, trusted only)
- Paraphrased untrusted messages (from step a, with random boundary fencing)
- Recent msg outputs (all `msg` task outputs since last summarization, from `store.tasks`)
- Skill summaries and args schemas (only skills allowed for this user, from `kiso.toml`, rescanned on each planner call — skips directories with `.installing` marker)
- Caller role (admin | user)
- New message

### c) Calls the Planner

Uses structured output (`response_format` with strict JSON schema — see [llm-roles.md — Planner](llm-roles.md#planner) for the full schema). The provider guarantees valid JSON at decoding level.

Returns JSON with:
- `goal`: high-level objective for the entire process. Stored for the reviewer and potential replan cycles.
- `secrets`: `{key, value}` pairs or `null`. If present, stored in **worker memory** (ephemeral, never in DB). See [security.md — Ephemeral Secrets](security.md#ephemeral-secrets).
- `tasks`: `exec` and `skill` tasks must include an `expect` field with semantic success criteria (they are always reviewed).

### d) Validates the Plan

Before execution, kiso validates the plan semantically (see [llm-roles.md — Validation After Parsing](llm-roles.md#validation-after-parsing) for the full rule list and error example). On failure, retries up to `max_validation_retries` (default 3) with specific error feedback. If exhausted: fail the message, notify user. No silent fallback.

All validated tasks are persisted to `store.tasks` with status `pending`.

### e) Executes Tasks One by One

For each task, kiso first **re-validates the user's role and permissions** from `config.toml` (see [security.md — Runtime Permission Re-validation](security.md#runtime-permission-re-validation)). For `exec` tasks, the command is checked against the destructive command deny list (see [security.md — Exec Command Validation](security.md#exec-command-validation)). Then (status updated to `running` in DB):

| Type | Execution |
|---|---|
| `exec` | `asyncio.create_subprocess_shell(...)` with `cwd=~/.kiso/sessions/{session}`, timeout from config. Admin: full access. User: restricted to session workspace. Clean env (only PATH). Captures stdout+stderr. |
| `msg` | Calls LLM with `worker` role. Context: facts + session summary + task detail. The worker does **not** see conversation messages — the planner provides all necessary context in the task `detail` field (see [llm-roles.md — Why the Worker Doesn't See the Conversation](llm-roles.md#why-the-worker-doesnt-see-the-conversation)). |
| `skill` | Validates args against `kiso.toml` schema. Pipes input JSON to stdin: `.venv/bin/python ~/.kiso/skills/{name}/run.py`. Input: args + session + workspace + scoped ephemeral secrets (only those declared in `kiso.toml`). Output: stdout. |

Output is sanitized (known secret values stripped — plaintext, base64, URL-encoded) before any further use. Task output is fenced with random boundary tokens before inclusion in any LLM prompt (reviewer, replan planner) — see [security.md — Random Boundary Fencing](security.md#layer-2-random-boundary-fencing). Task status and output are persisted to `store.tasks` (`done` or `failed`).

All LLM calls, task executions, and webhook deliveries are logged to the audit trail. See [audit.md](audit.md).

### f) Reviews and Delivers

**For `exec` and `skill` tasks** (always reviewed):

1. **Review**: Reviewer receives process goal + task detail + task expect + task output + original user message. Uses structured output. Two outcomes:
   - `status: "ok"` → proceed to next task
   - `status: "replan"` → triggers the replan flow (see below)
2. **Learn**: if the reviewer's `learn` field is present, stored as a new entry in `store.learnings` (pending evaluation by the curator).

**For `msg` tasks** (never reviewed):

1. **Deliver**: POSTed to webhook (if set) and available via `GET /status/{session}`. `final: true` only on the last `msg` task in the plan, and only after all preceding tasks (including reviews) have completed successfully.
2. If the webhook POST fails, kiso retries (3 attempts, backoff 1s/3s/9s). If all fail, logs and continues. Outputs remain available via `/status`. See [api.md — Webhook Callback](api.md#webhook-callback).

### g) Replan Flow (if reviewer returns "replan")

When the reviewer determines that the task failed and the plan needs revision:

1. **Notify the user**: the worker sends an automatic webhook message explaining that a replan is happening and why (using the reviewer's `reason`).

2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, pending, summary, messages, skills, role, original message)
   - **completed**: tasks already executed, with their outputs (from `store.tasks`)
   - **remaining**: tasks that were planned but not yet executed
   - **failure**: the failed task, its output, and the reviewer's `reason`
   - **replan_history**: previous replan attempts for this message (goal, failure, what was tried) — so the planner doesn't repeat the same mistakes

3. The planner produces a new `goal` and `tasks` list. The old remaining tasks are marked `failed` in DB. New tasks go through validation (step d) again.

4. Execution continues with the new task list. Even on replan, the planner must produce at least one task (typically a `msg` task summarizing the situation).

**Max replan depth**: after `max_replan_depth` replan cycles for the same original message, the worker stops replanning, notifies the user of the failure, and moves on.

## 4. Post-Execution

After draining the task list:

1. **Curator**: if there are pending learnings from this cycle, calls the Curator to evaluate them (promote to facts, ask the user, or discard). See [llm-roles.md — Curator](llm-roles.md#curator).
2. **Summarize messages**: if `len(raw_messages) >= summarize_threshold`, calls Summarizer (current summary + oldest messages + their msg task outputs → new summary → `store.sessions.summary`).
3. **Consolidate facts**: if facts exceed `knowledge_max_facts`, calls Summarizer to merge/deduplicate in `store.facts`. See [Facts Lifecycle](#facts-lifecycle).
4. **Wait or shutdown**: worker waits on session queue. After `worker_idle_timeout` seconds idle, shuts down (respawned on next message). Ephemeral secrets in worker memory are lost on shutdown.

## 5. New Message on the Same Session

If a message arrives while the worker is executing tasks, it gets queued. When the worker finishes the current queue, it picks up the next message and restarts from step 3.

---

## Facts Lifecycle

### How Facts Are Created

| Source | When | Example |
|---|---|---|
| Curator (promoted learning) | After evaluating a reviewer's `learn` field | `"Project uses Flask 2.3"` |
| Summarizer (consolidation) | When facts exceed `knowledge_max_facts` | Merges `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"` |
| Manual | Admin adds via DB or future API | `"Conventions: snake_case, type hints"` |

The reviewer does **not** create facts directly. It creates learnings, which the curator evaluates. See [llm-roles.md — Curator](llm-roles.md#curator) for the full evaluation flow (promote / ask user / discard).

### Where Facts Are Used

Planner, Worker, and Curator receive facts in their context. Reviewer, Summarizer, and Paraphraser do **not** (see [llm-roles.md — Context per Role](llm-roles.md#context-per-role) for the full matrix).

### Facts Are Global

See [database.md — facts](database.md#facts) for details. All facts are visible to all sessions — session column is provenance only.

### Consolidation

When facts exceed `knowledge_max_facts` (default 50), the Summarizer reads all facts, merges duplicates (e.g. `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"`), removes outdated ones, and replaces old rows with fewer consolidated entries.

---

## Diagram

```
POST /msg
  │
  auth (token) ───── 401 if invalid
  │
  whitelist? ──no──▶ save (trusted=0), stop
  │ yes
  resolve role, save (processed=0), queue
  │
  ▼
WORKER (per session)
  │
  paraphrase untrusted msgs (if any)
  │
  build context ──▶ planner (structured output)
  │                    ├─ goal
  │                    ├─ tasks
  │                    └─ secrets? → memory only
  │
  validate plan ──fail?──▶ retry (max 3) or fail msg
  │
  persist tasks (pending)
  │
  ┌─── FOR EACH TASK ───┐
  │                      │
  │  exec / msg / skill  │
  │         │            │
  │  sanitize + persist  │
  │         │            │
  │  exec/skill ──▶ review
  │                 │    │
  │              ok │  replan ──▶ notify + re-plan
  │                 │
  │          learn? ──▶ store learning
  │                 │
  │  msg ──▶ deliver (webhook + /status)
  │         │
  │  next task ◀────┘
  └──────────────────┘
  │
POST-EXECUTION
  ├─ curator (if learnings)
  ├─ summarize (if threshold)
  ├─ consolidate facts (if limit)
  └─ wait / shutdown
```
