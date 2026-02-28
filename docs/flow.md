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

### Alternative: `kiso msg`

For non-interactive (single-shot) use:

```
kiso msg "what is 2+2?"
```

This sends a single message, polls for the response, prints it, and exits. Equivalent to the chat REPL but without the interactive loop. Implicitly quiet when stdout is not a TTY (e.g. piped).

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

When the worker picks up a message, it marks it `processed=1` and proceeds:

### a) Paraphrases Untrusted Messages

If there are untrusted messages (`trusted=0`) in the context window, the worker calls the paraphraser (batch LLM call using the summarizer model) to rewrite them in third person. See [security.md — Prompt Injection Defense](security.md#6-prompt-injection-defense).

### b) Builds Planner Context

Only what the planner needs (see [llm-roles.md](llm-roles.md)):
- Facts (session-scoped, from `store.facts` — see [Knowledge / Fact scoping](#facts-are-session-scoped))
- Pending items (global + session, from `store.pending`)
- Session summary (from `store.sessions.summary`)
- Last `context_messages` raw messages (default 7, from `store.messages`, trusted only)
- Paraphrased untrusted messages (from step a, with random boundary fencing)
- Recent msg outputs (all `msg` task outputs since last summarization, from `store.tasks`)
- Workspace file listing (files in the session directory, max 30, with sizes)
- Skill summaries and args schemas (only skills allowed for this user, from `kiso.toml`, rescanned on each planner call — skips directories with `.installing` marker)
- Caller role (admin | user)
- New message

### c) Calls the Planner

Uses structured output (`response_format` with strict JSON schema — see [llm-roles.md — Planner](llm-roles.md#planner) for the full schema). The provider guarantees valid JSON at decoding level.

Returns JSON with:
- `goal`: high-level objective for the entire process. Stored for the reviewer and potential replan cycles.
- `secrets`: `{key, value}` pairs or `null`. If present, stored in **worker memory** (ephemeral, never in DB). See [security.md — Ephemeral Secrets](security.md#ephemeral-secrets).
- `tasks`: `exec`, `skill`, and `search` tasks must include an `expect` field with semantic success criteria (they are always reviewed).

### d) Validates and Persists the Plan

Before execution, kiso validates the plan semantically (see [llm-roles.md — Validation After Parsing](llm-roles.md#validation-after-parsing) for the full rule list and error example). On failure, retries up to `max_validation_retries` (default 3) with specific error feedback. If exhausted: fail the message, notify user. No silent fallback.

After validation, kiso creates a **plan** entity in `store.plans` (with `goal`, `message_id`, and `status=running`) and persists all tasks to `store.tasks` linked to that plan via `plan_id`. See [database.md — plans](database.md#plans).

### e) Executes Tasks One by One

For each task, kiso first checks the **cancel flag** — if set, remaining tasks are marked `cancelled`, a cancel summary is delivered to the user, and execution stops (see [Cancel](#cancel)). Then kiso **re-validates the user's role and permissions** from `config.toml` (see [security.md — Runtime Permission Re-validation](security.md#runtime-permission-re-validation)). For `exec` tasks, the command is checked against the destructive command deny list (see [security.md — Exec Command Validation](security.md#exec-command-validation)). Then (status updated to `running` in DB):

| Type | Execution |
|---|---|
| `exec` | **Two-step (architect/editor pattern):** 1) The **exec translator** LLM converts the natural-language `detail` into a shell command, using the system environment context (available binaries, OS, CWD) and preceding plan outputs. The translated command is persisted in the task's `command` column so the CLI can display it. 2) The translated command is executed via `asyncio.create_subprocess_shell(...)` with `cwd=/root/.kiso/sessions/{session}` (container-internal path), timeout from config. Admin: full access. User: restricted to session workspace. Clean env (only PATH). Plan outputs from preceding tasks available in `{workspace}/.kiso/plan_outputs.json`. Captures stdout+stderr. |
| `msg` | Calls LLM with `messenger` role. Context: facts + session summary + task detail + preceding plan outputs (fenced). The worker does **not** see conversation messages — the planner provides all necessary context in the task `detail` field (see [llm-roles.md — Why the Worker Doesn't See the Conversation](llm-roles.md#why-the-worker-doesnt-see-the-conversation)). |
| `search` | Calls LLM with `searcher` role (`google/gemini-2.5-flash-lite:online`). `detail` = search query. `args` = optional JSON `{"max_results": N, "lang": "xx", "country": "XX"}`. Preceding plan outputs provided as context. Returns web search results. Always reviewed. |
| `skill` | Validates args against `kiso.toml` schema. Pipes input JSON to stdin: `.venv/bin/python /root/.kiso/skills/{name}/run.py` (container-internal path). Input: args + session + workspace + scoped ephemeral secrets + `plan_outputs` (preceding task outputs). Output: stdout. |

**Task output chaining**: the worker accumulates outputs from completed tasks in the current plan and passes them to each subsequent task. This allows later tasks to reference results from earlier ones without replanning. See [Task Output Chaining](#task-output-chaining).

Output is sanitized (known secret values stripped — plaintext, base64, URL-encoded) before any further use. Task output is fenced with random boundary tokens before inclusion in any LLM prompt (reviewer, replan planner, worker) — see [security.md — Random Boundary Fencing](security.md#layer-2-random-boundary-fencing). Task status and output are persisted to `store.tasks` (`done` or `failed`).

All LLM calls, task executions, and webhook deliveries are logged to the audit trail. See [audit.md](audit.md).

### f) Reviews and Delivers

**For `exec`, `skill`, and `search` tasks** (always reviewed):

1. **Review**: Reviewer receives process goal + task detail + task expect + task output + original user message. Uses structured output. Two outcomes:
   - `status: "ok"` → proceed to next task
   - `status: "replan"` → triggers the replan flow (see below)
2. **Learn**: if the reviewer's `learn` field is present, stored as a new entry in `store.learnings` (pending evaluation by the curator).

**For `msg` tasks** (never reviewed):

1. **Deliver**: POSTed to webhook (if set) and available via `GET /status/{session}`. `final: true` only on the last `msg` task in the plan, and only after all preceding tasks (including reviews) have completed successfully.
2. If the webhook POST fails, kiso retries (3 attempts, backoff 1s/3s/9s). If all fail, logs and continues. Outputs remain available via `/status`. See [api.md — Webhook Callback](api.md#webhook-callback).

### g) Replan Flow

Replans can be triggered two ways:

1. **Reviewer-triggered**: the reviewer returns status="replan" after a task fails
2. **Planner-initiated** (discovery plan): the planner ends a plan with a `replan` task after investigation steps

In both cases the flow is:

When the reviewer determines that the task failed and the plan needs revision, or when the planner requests a self-directed replan after investigation:

1. **Notify the user**: the worker creates a `msg` task on the current plan with the replan notification (so the CLI can display it), saves a system message, and delivers a webhook (with `final: false`) explaining that a replan is happening and why (using the reviewer's `reason`).

2. **Call the planner** with enriched context:
   - Everything the planner normally receives (facts, pending, summary, messages, skills, role, original message)
   - **completed**: tasks already executed, with their outputs (from `store.tasks`)
   - **remaining**: tasks that were planned but not yet executed
   - **failure**: the failed task, its output, and the reviewer's `reason`
   - **replan_history**: previous replan attempts for this message (goal, failure, what was tried) — so the planner doesn't repeat the same mistakes

3. The planner produces a new `goal` and `tasks` list. The current plan is marked `failed`. A new plan is created with `parent_id` pointing to the previous plan. Remaining tasks from the old plan are marked `failed`. New tasks go through validation (step d) again.

4. Execution continues with the new task list. Even on replan, the planner must produce at least one task (typically a `msg` task summarizing the situation).

**Self-directed replans**: when the planner creates a discovery plan (exec tasks + final `replan` task), the investigation tasks run normally, then the replan task triggers a new planning cycle with the investigation results in context. The completed plan is marked "done" (not "failed") since the investigation succeeded. Self-directed replans count toward the depth limit.

**Max replan depth**: after `max_replan_depth` (default 5) replan cycles for the same original message, the worker stops replanning, notifies the user of the failure, and moves on. The planner can request up to +3 additional replan attempts via the `extend_replan` field on the plan.

### Task Output Chaining

The worker accumulates outputs from completed tasks in the current plan and passes them to each subsequent task. The structure is an array of entries:

```json
[
  {
    "index": 1,
    "type": "skill",
    "detail": "Search for fly.io deployment guides",
    "output": "1. fly.io/docs/python - Deploy Python apps...",
    "status": "done"
  },
  {
    "index": 2,
    "type": "exec",
    "detail": "cat requirements.txt",
    "output": "flask==3.1\ngunicorn==21.2",
    "status": "done"
  }
]
```

Fields: `index` (1-based position in the plan), `type`, `detail` (what was requested), `output` (stdout for exec/skill, generated text for msg), `status` (`done` or `failed` — so the consumer knows if the output is a result or an error).

How each task type receives preceding outputs:

| Task type | Mechanism | Details |
|---|---|---|
| `exec` | File `{workspace}/.kiso/plan_outputs.json` | Written before each execution. Empty array (`[]`) if first task. The planner writes commands that reference it, e.g. `jq -r '.[-1].output' .kiso/plan_outputs.json`. |
| `skill` | `plan_outputs` field in input JSON | Same structure, added alongside `args`, `session`, `workspace`, `session_secrets` in the stdin JSON. |
| `search` | Fenced section in searcher LLM prompt | Same structure as `msg`, preceding outputs provided in the searcher's context. |
| `msg` | Fenced section in messenger LLM prompt | Formatted as readable text inside boundary fencing (external content). The worker uses it naturally when writing responses. |

The worker always provides preceding outputs — no conditional logic. The planner writes task details that reference them when needed. The file is cleaned up after plan completion.

### Public File Serving

Files written to `pub/` inside the session workspace (`~/.kiso/instances/{name}/sessions/{session}/pub/`) are served directly via an HMAC-based URL:

```
GET /pub/{token}/{filename}
```

- `token` = `hmac_sha256(session_id, cli_token)[:16]` (hex, 16 chars)
- The secret key is the `cli` token from config (per-install unique)
- The endpoint reverse-maps token→session by iterating existing sessions and comparing HMAC
- Path traversal protection: resolved path must stay inside `pub/`
- No authentication required — anyone with the URL can download

After exec task execution, the worker scans `pub/` and appends published file URLs to the task output. No DB registration needed — presence on disk is sufficient.

### Cancel

A plan in execution can be cancelled via `POST /sessions/{session}/cancel` (see [api.md](api.md#post-sessionssessioncancel)).

When the worker detects a cancel flag (checked between tasks, not mid-task):
1. Current task (if running) completes normally
2. Remaining `pending` tasks are marked `cancelled`
3. The plan is marked `cancelled`
4. The worker delivers a `msg` to the user with: completed tasks and their outcomes, tasks that were not executed, and suggestions for next steps (e.g. cleanup actions, how to retry)

This message is generated by the worker (not planned by the planner) — it's an automatic cancel summary.

Queued messages on the same session are processed normally after cancellation.

## 4. Post-Execution

After draining the task list:

1. **Update fact usage**: increments `use_count` and updates `last_used` for all facts that were included in the planner context this cycle.
2. **Curator**: if there are pending learnings from this cycle, calls the Curator to evaluate them (promote to facts, ask the user, or discard). See [llm-roles.md — Curator](llm-roles.md#curator).
3. **Summarize messages**: if `len(raw_messages) >= summarize_threshold`, calls Summarizer (current summary + oldest messages + their msg task outputs → new structured summary → `store.sessions.summary`). The summary has four sections: Session Summary, Key Decisions, Open Questions, Working Knowledge.
4. **Consolidate facts**: if facts exceed `knowledge_max_facts`, calls Summarizer to merge/deduplicate facts and assign categories and confidence scores. Structured output: `[{content, category, confidence}]`. See [Facts Lifecycle](#facts-lifecycle).
5. **Decay facts**: reduces `confidence` by `fact_decay_rate` for facts not used in the last `fact_decay_days` days (floor at 0.0).
6. **Archive low-confidence facts**: moves facts with `confidence < fact_archive_threshold` to `facts_archive` and removes them from active context.
7. **Wait or shutdown**: worker waits on session queue. After `worker_idle_timeout` seconds idle, shuts down (respawned on next message). Ephemeral secrets in worker memory are lost on shutdown.

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

### Facts Are Session-Scoped

See [database.md — facts](database.md#facts) for the full schema.

Facts have a `category` (`project`, `user`, `tool`, `general`) and an optional `session` column (provenance).

**Visibility rules (M43)**:
- `project`, `tool`, `general` facts: always global — visible in every session.
- `user` facts: scoped to the session where they were created. Other sessions do not see them.
- `user` facts: not visible outside their originating session.

**Admin visibility (M44f)**: admin callers receive all facts from all sessions, but the planner context splits them into two priority tiers:
- `## Known Facts` — current session + global facts (primary context).
- `## Context from Other Sessions` — facts from other sessions, annotated with `[session:<name>]` (background memory).

### Consolidation

When facts exceed `knowledge_max_facts` (default 50), the Summarizer reads all facts and returns a structured JSON array: `[{content, category, confidence}]`. It merges duplicates (e.g. `"uses Flask"` + `"Flask 2.3"` → `"Project uses Flask 2.3"`), resolves contradictions (keeps the most recent), and assigns a category (`project`, `user`, `tool`, `general`) and confidence (1.0 for well-established facts, lower for uncertain ones). The old rows are replaced with the consolidated entries.

After consolidation, the worker runs a **decay pass** (reduces confidence for stale facts) and an **archive pass** (moves low-confidence facts to `facts_archive`). See [database.md — facts](database.md#facts) for the full schema.

### Planner Context

Facts are grouped by category. For regular users:

```
## Known Facts
### Project
- Project uses Flask 2.3

### User
- Team: marco (backend), anna (frontend)

### Tool
- Tests run with: pytest tests/ -q
```

For admin callers, cross-session facts appear in a second block:

```
## Known Facts
### User
- Alice prefers verbose output

## Context from Other Sessions
### User
- Bob prefers brief output [session:discord-bot]
```

Unknown or uncategorized facts fall into `### General`.

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
  create plan (running) + persist tasks (pending)
  │
  ┌─── FOR EACH TASK ────────┐
  │                           │
  │  cancel? ──▶ cancel plan  │
  │  │                        │
  │  pass plan_outputs        │
  │  │                        │
  │  exec → translate → run   │
  │  skill → validate → run   │
  │  search → searcher LLM     │
  │  msg → generate → deliver │
  │  replan → trigger replan  │
  │         │                 │
  │  sanitize + accumulate    │
  │         │                 │
  │  exec/skill/search ──▶ review │
  │                 │    │    │
  │              ok │  replan ──▶ new plan (parent_id)
  │                 │         │
  │          learn? ──▶ store │
  │                 │         │
  │  msg ──▶ deliver          │
  │         │                 │
  │  next task ◀──────────────┘
  │
  plan → done
  │
POST-EXECUTION
  ├─ update fact usage (use_count, last_used)
  ├─ curator (if learnings)
  ├─ summarize messages (if threshold) → structured summary
  ├─ consolidate facts (if limit) → {content, category, confidence}
  ├─ decay stale facts (confidence -= fact_decay_rate)
  ├─ archive low-confidence facts → facts_archive
  ├─ store token usage on plan
  └─ wait / shutdown
```

## CLI Rendering

When displaying a plan execution, the CLI shows:

1. **Plan header** with goal and task count
2. **Plan detail** — numbered list of all tasks with types and descriptions
3. **Per-task display**:
   - Task header with index, type, and detail
   - For `exec` tasks: the translated shell command (e.g. `$ ls -la`)
   - Task output (truncated on TTY)
   - Review verdict (for exec/skill/search tasks)
4. **Token usage summary** at the end (input/output tokens and model used)
