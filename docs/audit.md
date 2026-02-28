# Audit Trail

All LLM calls, task executions, and webhook deliveries are logged to `~/.kiso/instances/{name}/audit/`. JSONL format, one file per day.

## Structure

```
~/.kiso/instances/{name}/audit/
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

Logged for all roles: planner, reviewer, worker (exec translator), messenger, searcher, summarizer, curator, paraphraser.

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

Logged for all task types: exec, msg, skill, search, replan. Output content is **not** stored in the audit log (it's in `store.tasks`). Only the length.

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

## Querying the Audit Log

The `kiso stats` command aggregates the LLM audit entries and prints a formatted summary:

```bash
kiso stats                     # last 30 days, grouped by model
kiso stats --since 7           # last 7 days
kiso stats --by session        # group by session instead of model
kiso stats --session alice     # filter to a single session
kiso stats --all               # iterate all instances
```

Internally, `kiso stats` calls `GET /admin/stats` which reads the JSONL files via `kiso.stats.read_audit_entries()`. Only entries with `type == "llm"` contribute to the aggregation. The mapping of JSONL fields to aggregation dimensions:

| `--by` | JSONL field used as key |
|--------|------------------------|
| `model` | `model` |
| `session` | `session` |
| `role` | `role` |

See [cli.md — Token Usage Statistics](cli.md#token-usage-statistics) for usage examples and output format.

## Retention

No automatic cleanup. Files accumulate in `~/.kiso/instances/{name}/audit/`. Admins manage retention externally (logrotate, cron, etc.).
