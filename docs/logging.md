# Logging

Plain text log files, human-readable, `tail -f` friendly.

## Server Log

```
~/.kiso/instances/{name}/server.log
```

Server-level events: startup, shutdown, auth failures, errors, worker spawn/shutdown.

```
[2026-02-13 10:30:00] server started on 0.0.0.0:8333
[2026-02-13 10:30:05] auth ok (token: "cli") → session "dev-backend"
[2026-02-13 10:30:05] worker spawned for session "dev-backend"
[2026-02-13 10:30:05] auth failed from 192.168.1.50 (no matching token)
[2026-02-13 10:35:00] worker idle timeout for session "dev-backend"
```

## Session Log

```
~/.kiso/instances/{name}/sessions/{session}/session.log
```

Everything that happens in a session, including full task output inline.

```
[2026-02-13 10:30:05] msg from marco: "add JWT authentication"
[2026-02-13 10:30:06] planner: 4 tasks (goal: "Add JWT auth with login endpoint and middleware")
[2026-02-13 10:30:06] [1/4] msg "Analyzing the request..."
[2026-02-13 10:30:07] [1/4] output:
  Analyzing the request. I'll set up JWT authentication with
  login/logout endpoints and middleware for token validation.
[2026-02-13 10:30:07] [1/4] done → delivered
[2026-02-13 10:30:07] [2/4] skill:aider {"message": "add JWT auth to main.py"}
[2026-02-13 10:30:18] [2/4] output:
  Applied changes to main.py:
  + added /login and /logout endpoints
  + added jwt_required middleware
[2026-02-13 10:30:18] [2/4] done → review
[2026-02-13 10:30:19] [2/4] review: ok
[2026-02-13 10:30:19] [3/4] exec "python -m pytest"
[2026-02-13 10:30:22] [3/4] output:
  ===== 3 passed in 0.5s =====
[2026-02-13 10:30:22] [3/4] done → review
[2026-02-13 10:30:23] [3/4] review: ok
[2026-02-13 10:30:23] [3/4] review learn: "Project uses pytest for testing"
[2026-02-13 10:30:23] [4/4] msg "Summarize what was done and reply"
[2026-02-13 10:30:25] [4/4] output:
  Added JWT authentication to the project. All tests pass.
[2026-02-13 10:30:25] [4/4] done → delivered (final)
[2026-02-13 10:30:25] summarizer: 34 msgs → summary updated
[2026-02-13 10:30:26] facts: 51 entries → consolidated to 38
```

### Validation Failure Example

```
[2026-02-13 10:30:06] planner: validation failed (task 2: review=true but no expect field)
[2026-02-13 10:30:06] planner: validation retry 1/3
[2026-02-13 10:30:08] planner: validation ok on retry 1
```

### Replan Example

```
[2026-02-13 10:30:22] [3/4] review: replan — "Project uses Flask, not FastAPI"
[2026-02-13 10:30:22] replan: notifying user
[2026-02-13 10:30:22] replan: calling planner (attempt 1/3, completed: 2 tasks, remaining: 1)
[2026-02-13 10:30:24] planner: 3 new tasks (goal: "Add JWT auth using Flask patterns")
```

Session log is rotated at 2 MB (up to 2 backups: `session.log.1`, `session.log.2`). Server log is rotated at 5 MB (up to 3 backups). Full output inline — `grep` and `tail` are all you need.
