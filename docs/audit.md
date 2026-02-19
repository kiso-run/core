# Audit Trail

All LLM calls, task executions, and webhook deliveries are logged to `~/.kiso/audit/`. JSONL format, one file per day.

## Structure

```
~/.kiso/audit/
├── 2024-01-15.jsonl
├── 2024-01-16.jsonl
└── ...
```

## What Gets Logged

Every entry has a `timestamp`, `type`, `session`, and type-specific fields.

### LLM Calls

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "type": "llm",
  "session": "dev-backend",
  "role": "planner",
  "model": "minimax/minimax-m2.5",
  "provider": "openrouter",
  "input_tokens": 1200,
  "output_tokens": 350,
  "duration_ms": 2400,
  "status": "ok"
}
```

Logged for all roles: planner, reviewer, worker, summarizer, curator, paraphraser.

### Task Executions

```json
{
  "timestamp": "2024-01-15T10:30:05Z",
  "type": "task",
  "session": "dev-backend",
  "task_id": 42,
  "task_type": "exec",
  "detail": "python -m pytest tests/",
  "status": "done",
  "duration_ms": 5200,
  "output_length": 1500
}
```

Logged for all task types: exec, msg, skill. Output content is **not** stored in the audit log (it's in `store.tasks`). Only the length.

### Webhook Deliveries

```json
{
  "timestamp": "2024-01-15T10:30:10Z",
  "type": "webhook",
  "session": "discord_dev",
  "task_id": 43,
  "url": "http://localhost:9001/callback",
  "status": 200,
  "attempts": 1
}
```

Includes failed attempts (status != 2xx) and retry count.

### Reviews

```json
{
  "timestamp": "2024-01-15T10:30:08Z",
  "type": "review",
  "session": "dev-backend",
  "task_id": 42,
  "verdict": "ok",
  "has_learning": true
}
```

## Secret Masking

Audit entries never contain raw secret values. Before logging, known secret values are replaced with `[REDACTED]`.

**Masking algorithm**: for each known secret (deploy secrets from `.env` + ephemeral secrets from worker memory), check for and replace:
1. Plaintext value
2. Base64-encoded value
3. URL-encoded value

This is the same sanitization applied to task output (see [security.md — Leak Prevention](security.md#leak-prevention)). Best-effort — encoded variants beyond these three are not guaranteed to be caught.

## Retention

No automatic cleanup. Files accumulate in `~/.kiso/audit/`. Admins manage retention externally (logrotate, cron, etc.).
