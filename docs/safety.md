# Safety Rules, Job Cancellation & In-Flight Message Handling

## Safety Rules

Admin-defined constraints that persist across sessions and restarts.

### CLI

```bash
kiso rules list           # show all safety rules
kiso rules add "..."      # add a safety rule
kiso rules remove <id>    # remove a safety rule by ID
```

### REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/safety-rules` | List all safety rules |
| `POST` | `/safety-rules` | Add a rule (`{"content": "..."}`) |
| `DELETE` | `/safety-rules/{id}` | Remove a rule by ID |

### Behavior

- Safety facts are stored with `category = "safety"` in the facts table.
- **Always injected** into planner messages — not gated by the briefer, not filtered by relevance scoring.
- **Never decay** — excluded from confidence decay and archival.
- The **reviewer** checks task output against safety rules. Violations trigger `status: "stuck"`, which blocks further execution and notifies the user.

### Code

- Store: `kiso/store.py` — `save_fact(..., category="safety")`, `get_safety_facts()`
- Planner injection: `kiso/brain.py` — `build_planner_messages()` (M411)
- Reviewer check: `kiso/brain.py` — `build_reviewer_messages(safety_rules=...)` (M412)
- CLI: `cli/rules.py`
- API: `kiso/main.py` — `/safety-rules` endpoints (M413)

## Install Confirmation (P71 / M418–M421)

Kiso never installs tools, connectors, or OS packages without explicit user approval.

### How it works

1. **First plan**: if a tool/connector is needed but not installed, the planner produces a single `msg` task asking the user whether to install it, offers alternatives (e.g. `search` instead of `browser` for read-only content), and ends the plan there.
2. **User replies**: the user's response triggers a new planning cycle (replan).
3. **Replan**: only in a replan (`is_replan=True`) is the planner allowed to include `exec "kiso tool install ..."` tasks.

### Enforcement layers

| Layer | What it does |
|-------|-------------|
| **Planner prompt** | `kiso_native`, `tools_rules`, `web`, `plugin_install` modules all instruct: ask first, end plan with msg |
| **Capability gap injection** | When a needed tool is missing, injects text telling the planner to ask the user |
| **validate_plan** | Rejects any `exec` task containing `kiso tool install` or `kiso connector install` when `is_replan=False` |
| **Tool-not-installed error** | When a `tool` task references an uninstalled tool, the error message guides the LLM to plan a single msg task |

### Code

- Validation: `kiso/brain.py` — `validate_plan()`, `_INSTALL_CMD_RE`
- Capability gap: `kiso/brain.py` — `_detect_capability_gap()`
- Prompt rules: `kiso/roles/planner.md` — modules `kiso_native`, `tools_rules`, `web`, `plugin_install`
- Tests: `tests/test_install_confirm.py`, `tests/test_brain.py` (M420 section)

## Job Cancellation

### CLI

```bash
kiso cancel               # cancel current session's active job
kiso cancel <session>     # cancel a specific session's job
```

### REST API

```
POST /sessions/{sid}/cancel
```

Returns `{"cancelled": true, "plan_id": N, "drained": M}` on success, or `{"cancelled": false}` if no active job.

### Behavior

- Sets the worker's `cancel_event`, which is checked at each task boundary in the execution loop.
- Drains any queued messages and marks them as processed.
- The worker generates a messenger summary of what was completed and what was cancelled.
- No session destruction — the session remains usable for new messages.

### Code

- API: `kiso/main.py` — `post_cancel()` (M403)
- CLI: `cli/__init__.py` — `_cancel_cmd()` (M404)
- Worker: `kiso/worker/loop.py` — cancel event checks (M405)

## In-Flight Message Handling

When a new message arrives while a job is already running:

### Fast-Path Stop Detection (M407)

Single stop words (`STOP`, `ferma`, `cancel`, `abort`, `basta`, `quit`) and ALL-CAPS urgent messages are detected with a regex — no LLM call needed. The cancel event is set immediately.

Messages with content after the stop word (e.g., "stop using port 80") are **not** treated as stop commands.

### LLM Classification (M406/M408)

Non-stop messages are classified by an LLM into four categories:

| Category | Action |
|----------|--------|
| `stop` | Cancel event set |
| `update` | Content added to `update_hints` — reviewer sees it at next step |
| `independent` | Queued to `pending_messages` — processed after current job |
| `conflict` | Cancel event set + new message queued first |

All categories except `stop` return an immediate `ack` message to the user.

### Code

- Stop detection: `kiso/brain/common.py` — `is_stop_message()`
- Classifier: `kiso/brain/common.py` — `run_inflight_classifier()`
- Routing: `kiso/main.py` — `post_msg()` inflight handling (M408/M409)
- Worker drain: `kiso/worker/loop.py` — `pending_messages` drain after plan completes (M408)
