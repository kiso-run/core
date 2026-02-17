# API Endpoints

All endpoints except `/pub` and `/health` require authentication via bearer token (see [security.md](security.md)).

```
Authorization: Bearer <token>
```

Tokens are defined as named entries in `config.toml`. Kiso matches the token to its name, logs which client made the call, and uses the token name for alias resolution. See [config.md](config.md).

## POST /sessions

Creates or updates a session. Used by connectors to register sessions with metadata before sending messages. The CLI does not call this — sessions are created implicitly on first `POST /msg`.

**Request:**

```json
{
  "session": "discord_dev",
  "webhook": "http://localhost:9001/callback",
  "description": "Discord #dev channel"
}
```

| Field | Required | Description |
|---|---|---|
| `session` | yes | Session identifier (chosen by the connector, opaque string). Must match `^[a-zA-Z0-9_@.-]{1,255}$`. |
| `webhook` | no | Connector callback URL for `msg` task deliveries |
| `description` | no | Human-readable label for the session |

**Response** `201 Created` (new session) or `200 OK` (updated):

```json
{
  "session": "discord_dev",
  "created": true
}
```

The `connector` field is set automatically from the token name (e.g. token `discord` → `connector = "discord"`).

## POST /msg

Receives a message and queues it for processing.

**Request:**

```json
{
  "session": "dev-backend",
  "user": "marco",
  "content": "add /health endpoint"
}
```

| Field | Required | Description |
|---|---|---|
| `session` | yes | Session identifier. Must match `^[a-zA-Z0-9_@.-]{1,255}$`. |
| `user` | yes | User identity: Linux username (direct API) or platform identity (connectors — resolved via aliases) |
| `content` | yes | Message content |

If the session does not exist, it is created implicitly (with no webhook, no connector metadata). This is the normal path for CLI usage.

**Response** `202 Accepted`:

```json
{
  "queued": true,
  "session": "dev-backend"
}
```

**Error responses:**

| Status | When |
|---|---|
| `401 Unauthorized` | Bearer token does not match any entry in `config.toml` |
| `202 Accepted` | Unknown user — message saved for audit but not processed (same response as success, by design) |

## GET /sessions

Lists sessions the authenticated user participates in.

**Query params:**

| Param | Required | Description |
|---|---|---|
| `all` | no | If `true`, return all sessions (admin only). Non-admins: ignored. |

The user is resolved from the bearer token + `user` param (same as `POST /msg`). Returns sessions where the user has at least one message in `store.messages`.

**Response:**

```json
[
  {"session": "discord_dev", "connector": "discord", "description": "Discord #dev", "updated_at": "..."},
  {"session": "laptop@marco", "connector": null, "description": null, "updated_at": "..."}
]
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
  "plan": {                    // current or most recent plan (null if none)
    "id": 3,
    "goal": "Add JWT auth with tests",
    "status": "running"
  },
  "tasks": [                   // all tasks since `after` (each: id, type, status, output)
    {"id": 5, "type": "exec", "status": "done", "output": "OK"},
    {"id": 6, "type": "msg", "status": "done", "output": "Done!"}
  ],
  "queue_length": 0,           // pending messages in session queue
  "active_task": null,         // currently running task or null
  "worker_running": true       // whether session worker is alive
}
```

Returns all tasks (for monitoring and debugging). Clients that only want user-facing messages filter by `type: "msg"`.

## POST /sessions/{session}/cancel

Cancels the currently executing plan on a session. The worker finishes the current task, marks remaining tasks as `cancelled`, marks the plan as `cancelled`, and delivers a cancel summary to the user.

**Response** `200 OK`:

```json
{
  "cancelled": true,
  "plan_id": 3
}
```

If no plan is currently executing: `200 OK` with `"cancelled": false`. Idempotent — calling twice has no additional effect.

Queued messages on the session are not affected — they are processed normally after cancellation. See [flow.md — Cancel](flow.md#cancel).

## POST /admin/reload-env

Hot-reloads deploy secrets from `~/.kiso/.env` without restarting the server. Admin only.

**Response** `200 OK`:

```json
{
  "reloaded": true,
  "keys_loaded": 5
}
```

**`403 Forbidden`** if the token does not belong to an admin user.

## Webhook Callback

Every `msg` task output is POSTed to the session's webhook URL (set via `POST /sessions`):

```json
{
  "session": "dev-backend",
  "task_id": 42,
  "type": "msg",
  "content": "Added JWT auth. Tests passing.",
  "final": false
}
```

- `final: true` on the last `msg` task in the current plan, sent only after the entire plan completes successfully (no pending reviews). Also `true` on the cancel summary message.
- Only `msg` tasks trigger webhooks — `exec` and `skill` outputs are internal. See [flow.md — Delivers msg Tasks](flow.md#f-reviews-and-delivers).
- **Retry**: 3 attempts with backoff (1s, 3s, 9s). If all fail, kiso logs the failure and continues. Outputs remain available via `/status`.
- **Connector requirement**: connectors must implement a polling fallback — if no webhook callback arrives within a reasonable timeout, poll `GET /status/{session}?after={last_task_id}` to recover missed responses.

## GET /pub/{id}

Serves a published file. **No authentication required** — anyone with the link can download.

The `id` is a UUID4 (128-bit random, non-enumerable) that maps to a file in `~/.kiso/sessions/{session}/pub/`. The session ID is never exposed in the URL.

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

The webhook + polling model covers all current use cases. The worker loop architecture supports adding SSE without structural changes.
