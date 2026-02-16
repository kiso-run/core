# Database

Single SQLite file: `~/.kiso/store.db`. Six tables.

## Tables

### sessions

Active sessions with webhook URL and rolling conversation summary.

```sql
CREATE TABLE sessions (
    session    TEXT PRIMARY KEY,
    webhook    TEXT,
    summary    TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

- Created or updated on every `POST /msg`. Webhook URL can change between calls (latest wins).
- `summary` is a rolling text blob maintained by the summarizer. Overwritten each time.

### messages

All messages across all sessions.

```sql
CREATE TABLE messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session   TEXT NOT NULL,
    user      TEXT,
    role      TEXT NOT NULL,       -- user | assistant | system
    content   TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_messages_session ON messages(session, id);
```

`user` is the resolved Linux username (not the platform alias). In multi-user sessions (Discord channel), tracks who said what.

### tasks

All tasks across all sessions. Persisted to survive container restarts.

```sql
CREATE TABLE tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT NOT NULL,
    message_id INTEGER NOT NULL,   -- which message triggered this task
    goal       TEXT NOT NULL,       -- process-level goal from the planner
    type       TEXT NOT NULL,       -- exec | msg | skill
    detail     TEXT NOT NULL,       -- what to do
    skill      TEXT,                -- skill name (if type=skill)
    args       TEXT,                -- JSON args (if type=skill)
    expect     TEXT,                -- success criteria (required if review=1)
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    output     TEXT,                -- stdout / generated text
    stderr     TEXT,                -- stderr (exec/skill only)
    review     BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_tasks_session ON tasks(session, id);
CREATE INDEX idx_tasks_status ON tasks(session, status);
```

Status lifecycle: `pending` → `running` → `done` | `failed`.

On startup, any tasks left in `running` status are marked as `failed` (container crashed mid-execution).

The `/status/{session}` endpoint reads from this table.

**Delivery rule**: all `msg` task outputs are delivered to the user (via webhook and/or polling). `exec` and `skill` outputs are internal — the planner adds `msg` tasks wherever it wants to communicate. See [flow.md](flow.md).

### facts

Persistent knowledge learned across all sessions. Individual entries, not a blob.

```sql
CREATE TABLE facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL,       -- "reviewer" | "summarizer" | "manual"
    session    TEXT,                -- provenance: which session generated this (null for manual)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Facts are global.** All facts are visible to all sessions regardless of the `session` column. The `session` column is **provenance only** — it records where the fact came from, not where it's visible.

- **Reviewer** adds entries via the `learn` field in review output.
- **Summarizer** consolidates when entries exceed `knowledge_max_facts`: merges duplicates and removes outdated entries, replacing old rows with fewer consolidated ones.
- The planner and worker see all facts as a flat list.

Example entries:
```
id=1  content="Project uses Flask 2.3"                  source="reviewer"    session="dev-backend"
id=2  content="Team: marco (backend), anna (frontend)"  source="reviewer"    session="discord-general"
id=3  content="Conventions: snake_case, type hints"      source="manual"      session=NULL
```

All three are visible in every session. Fact #1, learned in `dev-backend`, helps the planner in `discord-general` too.

See [flow.md — Facts Lifecycle](flow.md#facts-lifecycle) for creation, usage, and consolidation flows.

### secrets

Per-session credentials provided by the user for the bot to use. These are **session secrets** — not to be confused with deploy secrets (env vars). See [security.md](security.md).

```sql
CREATE TABLE secrets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session, key)
);
CREATE INDEX idx_secrets_session ON secrets(session);
```

### published

Mapping for published file URLs. See [api.md](api.md) `GET /pub/{id}`.

```sql
CREATE TABLE published (
    id         TEXT PRIMARY KEY,
    session    TEXT NOT NULL,
    filename   TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

`id` is a random string. `path` is the file's location on disk (inside `~/.kiso/sessions/{session}/pub/`). The URL `GET /pub/{id}` resolves to this file without exposing the session ID.

## What's NOT in the database

- **Logs**: plain text files in `sessions/{id}/session.log` and `~/.kiso/server.log`.
