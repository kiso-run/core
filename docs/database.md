# Database

Single SQLite file per instance: `~/.kiso/instances/{name}/store.db`. **All queries use parameterized statements** — never string concatenation. Input values (session IDs, user names, content) are always passed as query parameters.

## Tables

### sessions

Active sessions with metadata and rolling conversation summary.

```sql
CREATE TABLE sessions (
    session     TEXT PRIMARY KEY,
    connector   TEXT,                -- token name of the connector that created it (null for CLI)
    webhook     TEXT,                -- connector callback URL (null for CLI)
    description TEXT,                -- human-readable label (e.g. "Discord #dev channel")
    summary     TEXT DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

- Created explicitly via `POST /sessions` (connectors) or implicitly on first `POST /msg` (CLI).
- `webhook` is set at session creation and used for all msg task deliveries. Not updated per-message.
- `summary` is a rolling text blob maintained by the summarizer. Overwritten each time.

### messages

All messages across all sessions, including from non-whitelisted users.

```sql
CREATE TABLE messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session   TEXT NOT NULL,
    user      TEXT,
    role      TEXT NOT NULL,       -- user | assistant | system
    content   TEXT NOT NULL,
    trusted   BOOLEAN DEFAULT 1,   -- 0 for non-whitelisted users
    processed BOOLEAN DEFAULT 0,   -- 1 after worker picks it up
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_messages_session ON messages(session, id);
CREATE INDEX idx_messages_unprocessed ON messages(processed) WHERE processed = 0;
```

- `user` is the resolved Linux username (not the platform alias). In multi-user sessions, tracks who said what.
- `trusted=0` messages are from non-whitelisted users: saved for context and audit, never trigger planning. Paraphrased before inclusion in planner context (see [security.md — Prompt Injection Defense](security.md#6-prompt-injection-defense)).
- `processed=0` messages are recovered on startup — re-enqueued for processing. Prevents silent message loss on crash.

### plans

A plan is the planner's output for a single message: a goal and a list of tasks. First-class entity that groups tasks and tracks plan-level state.

```sql
CREATE TABLE plans (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session             TEXT NOT NULL,
    message_id          INTEGER NOT NULL,    -- which message triggered this plan
    parent_id           INTEGER,             -- previous plan if this is a replan (null for first plan)
    goal                TEXT NOT NULL,       -- from planner output
    status              TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | cancelled
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,       -- cumulative LLM input tokens for this plan
    total_output_tokens INTEGER NOT NULL DEFAULT 0,       -- cumulative LLM output tokens for this plan
    model               TEXT,                             -- model used for the planner call
    llm_calls           TEXT,                             -- JSON array of per-call LLM stats
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_plans_session ON plans(session, id);
```

- Created after the planner returns and validation passes.
- `parent_id` links replan chains: replan creates a new plan pointing to the previous one.
- Status lifecycle: `running` → `done` | `failed` | `cancelled`.
- On startup, any plans left in `running` status are marked as `failed`.
- `total_input_tokens` / `total_output_tokens`: accumulated across all tasks in the plan. Updated as tasks complete.
- `llm_calls`: JSON array of `{role, model, input_tokens, output_tokens}` objects, one per LLM call in the plan lifecycle.

### tasks

All tasks across all sessions. Each task belongs to a plan.

```sql
CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         INTEGER NOT NULL,   -- which plan this task belongs to
    session         TEXT NOT NULL,
    type            TEXT NOT NULL,      -- exec | msg | skill
    detail          TEXT NOT NULL,      -- what to do (natural-language for exec, message for msg)
    skill           TEXT,               -- skill name (if type=skill)
    args            TEXT,               -- JSON string of skill args (parsed before execution)
    expect          TEXT,               -- success criteria (required for exec and skill tasks)
    command         TEXT,               -- translated shell command (exec only, set after LLM translation)
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    substatus       TEXT,               -- free-text detail on current status (e.g. "reviewing")
    output          TEXT,               -- stdout / generated text
    stderr          TEXT,               -- stderr (exec/skill only)
    retry_count     INTEGER NOT NULL DEFAULT 0,
    review_verdict  TEXT,               -- "pass" | "fail" | "replan" (set after reviewer runs)
    review_reason   TEXT,               -- reviewer rationale
    review_learning TEXT,               -- learning extracted by reviewer (promoted to facts by curator)
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    llm_calls       TEXT,               -- JSON array of per-call LLM stats (atomic append via json_insert)
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_tasks_plan ON tasks(plan_id, id);
CREATE INDEX idx_tasks_session ON tasks(session, id);
CREATE INDEX idx_tasks_status ON tasks(session, status);
```

- `plan_id` replaces the old `message_id` + `goal` — the plan owns the goal, tasks reference the plan.
- `exec` and `skill` tasks are always reviewed. `expect` is required for them. `msg` tasks are never reviewed.
- `command`: for `exec` tasks, the planner writes a natural-language `detail`; the exec translator LLM converts it to a shell command stored here before execution.
- `llm_calls`: appended atomically via SQLite `json_insert` — no read-modify-write race condition between concurrent coroutines.
- Status lifecycle: `pending` → `running` → `done` | `failed` | `cancelled`.
- On startup, any tasks left in `running` status are marked as `failed` (container crashed mid-execution).
- On cancel, remaining `pending` tasks are marked `cancelled`.
- The `/status/{session}` endpoint reads from this table.
- Only `msg` tasks are delivered to the user. See [flow.md — Delivers msg Tasks](flow.md#f-reviews-and-delivers).

### facts

Global persistent knowledge — confirmed truths promoted by the curator. Individual entries, not a blob.

```sql
CREATE TABLE facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL,       -- "curator" | "summarizer" | "manual"
    session    TEXT,                -- provenance: which session originated this (null for manual)
    category   TEXT DEFAULT 'general',  -- "project" | "user" | "tool" | "general"
    confidence REAL DEFAULT 1.0,    -- 0.0–1.0; decays with disuse, raises with use
    last_used  TEXT,                -- ISO timestamp of last inclusion in planner context
    use_count  INTEGER DEFAULT 0,   -- how many times included in a plan context
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Facts visibility depends on category** (M43):
- `project`, `tool`, `general`: global — visible to all sessions.
- `user`: session-scoped — visible only in the originating session (and to admin sessions).

The `session` column serves dual purpose: provenance (who created it) and, for `user`-category facts, access scope.

Facts are **certain truths** that have passed evaluation by the curator. They are not created directly by the reviewer — the reviewer produces learnings (see below), and the curator promotes confirmed learnings to facts. See [flow.md — Facts Lifecycle](flow.md#facts-lifecycle).

- **`category`**: one of `project`, `user`, `tool`, `general`. The planner receives facts grouped by category so it can find relevant context faster.
- **`confidence`**: starts at 1.0. Decays by `fact_decay_rate` for facts not used in `fact_decay_days` days. Facts below `fact_archive_threshold` (default 0.3) are moved to `facts_archive`.
- **`last_used` / `use_count`**: updated after each successful plan that included the fact in the planner context. Facts used frequently maintain their confidence.

Example entries:
```
id=1  content="Project uses Flask 2.3"  category="project"  confidence=0.9  source="curator"
id=2  content="Team: marco (backend)"   category="user"     confidence=1.0  source="curator"
id=3  content="snake_case conventions"  category="project"  confidence=0.6  source="manual"
```

### facts_archive

Soft-deleted facts moved from `facts` when their confidence drops below `fact_archive_threshold`. Kept for audit and potential recovery.

```sql
CREATE TABLE facts_archive (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    original_id INTEGER,            -- id from facts table at time of archiving
    content     TEXT NOT NULL,
    source      TEXT NOT NULL,
    session     TEXT,
    category    TEXT DEFAULT 'general',
    confidence  REAL DEFAULT 0.0,
    last_used   TEXT,
    use_count   INTEGER DEFAULT 0,
    archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at  TEXT
);
```

### learnings

Candidate facts produced by the reviewer. Pending evaluation by the curator before potential promotion to facts.

```sql
CREATE TABLE learnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    session    TEXT NOT NULL,       -- where it was learned
    user       TEXT,                -- who was interacting
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | promoted | discarded
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_learnings_status ON learnings(status) WHERE status = 'pending';
```

- Created by the reviewer's `learn` field after task review.
- The curator evaluates pending learnings and either promotes them to facts, asks the user for confirmation, or discards them.

### pending

Open questions and unresolved issues. Visible to the planner, which can act on them.

```sql
CREATE TABLE pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    scope      TEXT NOT NULL,       -- "global" or a session ID
    source     TEXT NOT NULL,       -- "curator" | "planner" | "reviewer"
    status     TEXT NOT NULL DEFAULT 'open',  -- open | resolved
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_pending_scope ON pending(scope, status);
```

- Global pending items are visible to all sessions. Session-scoped ones only to that session.
- Resolved pending items may become facts (via curator) or get absorbed into the session summary.

## What's NOT in the database

- **Logs & audit**: plain text files (`session.log`, `server.log`) and JSONL (`audit/`). See [audit.md](audit.md).
- **Secrets**: ephemeral (worker memory only) and deploy (env vars via `kiso env`) — never in DB. See [security.md](security.md#5-secrets).
- **Published files**: served directly from the filesystem (`pub/` directory in the session workspace). The URL token is HMAC-SHA256 derived from the session ID — no DB table involved. See [api.md — GET /pub](api.md#get-pubtokenfilename).
