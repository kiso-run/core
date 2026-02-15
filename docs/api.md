# API Endpoints

All endpoints except `/pub` require authentication via bearer token (see [security.md](security.md)).

```
Authorization: Bearer <api_token>
```

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
| `user` | yes | User alias (Linux username). Role resolved from `config.admins` |
| `content` | yes | Message content |
| `webhook` | no | URL to receive results. If empty, results available only via `/status` |

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
  "active_task": null
}
```

## GET /pub/{id}

Serves a published file. **No authentication required** — anyone with the link can download.

The `id` is a random string that maps to a file in `~/.kiso/sessions/{session}/pub/`. The session ID is never exposed in the URL.

**Response**: the file with appropriate `Content-Type` and `Content-Disposition` headers.

**404** if the id doesn't exist.

Files are published by exec or skill tasks that write to the session's `pub/` directory and register the file via the store.

## Webhook Callback

When a task has `notify: true`, the worker POSTs to the session's webhook:

```json
{
  "session": "dev-backend",
  "task_id": 42,
  "type": "msg",
  "content": "Added JWT auth. Tests passing.",
  "final": false
}
```

`final: true` on the last notifying task in the queue.

## GET /health

Health check. **No authentication required.** Used by Docker `HEALTHCHECK` and monitoring tools.

**Response** `200 OK`:

```json
{
  "status": "ok"
}
```
