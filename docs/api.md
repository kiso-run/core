# API Endpoints

All endpoints except `/pub` and `/health` require authentication via bearer token (see [security.md](security.md)).

```
Authorization: Bearer <token>
```

Tokens are defined as named entries in `config.toml`. Kiso matches the token to its name, logs which client made the call, and uses the token name for alias resolution. See [config.md](config.md).

## POST /msg

Receives a message and queues it for processing.

**Request:**

```json
{
  "session": "dev-backend",
  "user": "marco",
  "content": "add /health endpoint",
  "webhook": "https://example.com/hook"
}
```

| Field | Required | Description |
|---|---|---|
| `session` | yes | Session identifier |
| `user` | yes | User identity: Linux username (direct API) or platform identity (connectors — resolved via aliases) |
| `content` | yes | Message content |
| `webhook` | no | URL to receive `msg` task outputs. If empty, results available only via `/status` |

**Response** `202 Accepted`:

```json
{
  "queued": true,
  "session": "dev-backend"
}
```

## GET /status/{session}

For polling. Used by the CLI and clients without a webhook.

**Query params:**

| Param | Required | Description |
|---|---|---|
| `after` | no | ID of last seen task, returns only subsequent ones |

**Response:**

```json
{
  "session": "dev-backend",
  "tasks": [
    {"id": 5, "type": "exec", "status": "done", "output": "OK"},
    {"id": 6, "type": "msg", "status": "done", "output": "Done!"}
  ],
  "queue_length": 0,
  "active_task": null,
  "worker_running": true
}
```

| Field | Description |
|---|---|
| `tasks` | All tasks since `after` (or all if `after` not specified). Each has `id`, `type`, `status`, `output`. |
| `queue_length` | Number of pending messages waiting in the session queue. |
| `active_task` | Currently running task (`{"id": 5, "type": "exec", "status": "running"}`) or `null` if idle. |
| `worker_running` | Whether the session's worker is alive. `false` if idle-timed-out or never started. |

Returns all tasks (for monitoring and debugging). Clients that only want user-facing messages filter by `type: "msg"`.

## Webhook Callback

Every `msg` task output is POSTed to the session's webhook:

```json
{
  "session": "dev-backend",
  "task_id": 42,
  "type": "msg",
  "content": "Added JWT auth. Tests passing.",
  "final": false
}
```

`final: true` on the last `msg` task in the current plan.

Only `msg` tasks trigger webhooks. `exec` and `skill` outputs are internal — the planner adds `msg` tasks wherever it wants to communicate with the user. See [flow.md](flow.md).

## GET /pub/{id}

Serves a published file. **No authentication required** — anyone with the link can download.

The `id` is a random string that maps to a file in `~/.kiso/sessions/{session}/pub/`. The session ID is never exposed in the URL.

**Response**: the file with appropriate `Content-Type` and `Content-Disposition` headers.

**404** if the id doesn't exist.

Files are published by exec or skill tasks that write to the session's `pub/` directory and register the file via the store.

## GET /health

Health check. **No authentication required.** Used by Docker `HEALTHCHECK` and monitoring tools.

**Response** `200 OK`:

```json
{
  "status": "ok"
}
```

## GET /stream/{session} (SSE) — not yet implemented

Server-Sent Events endpoint for real-time task progress. **Status: planned, not yet implemented.**

```
event: task_start
data: {"task_id": 2, "type": "skill", "skill": "aider"}

event: task_done
data: {"task_id": 2, "status": "done"}

event: msg
data: {"task_id": 5, "content": "JWT auth added. Tests pass."}
```

The current architecture (worker loop emitting events per task) supports adding this without structural changes. The webhook + polling model covers all current use cases.
