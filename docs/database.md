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

`user` is an opaque alias (e.g. "marco", "anna"). In multi-user sessions (Discord channel), tracks who said what.

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
    expect     TEXT,                -- success criteria for reviewer
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    output     TEXT,                -- stdout / generated text
    stderr     TEXT,                -- stderr (exec/skill only)
    notify     BOOLEAN DEFAULT 0,
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

### secrets

Per-session credentials provided by the user for the bot to use.

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

See [security.md](security.md).

### meta

Global key-value store for persistent data that lives outside any session.

```sql
CREATE TABLE meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

Primary use: the `facts` key stores a text blob of shared knowledge across all sessions. Managed by the reviewer (appends via `learn`) and consolidated by the summarizer when it exceeds `knowledge_max_lines`.

Example:
```
key: "facts"
value: "Project uses FastAPI + SQLite. Team: marco (backend), anna (frontend). Conventions: snake_case, type hints."
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
