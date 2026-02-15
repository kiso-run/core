# Logging

Plain text log files. Append-only, human-readable, `tail -f` friendly.

## Server Log

```
~/.kiso/server.log
```

Server-level events: startup, shutdown, auth failures, errors, worker spawn/shutdown.

```
[2026-02-13 10:30:00] server started on 0.0.0.0:8333
[2026-02-13 10:30:05] worker spawned for session "dev-backend"
[2026-02-13 10:30:05] auth failed from 192.168.1.50 (invalid token)
[2026-02-13 10:35:00] worker idle timeout for session "dev-backend"
```

## Session Log

```
~/.kiso/sessions/{session}/session.log
```

Everything that happens in a session, including full task output inline.

```
[2026-02-13 10:30:05] msg from marco: "add JWT authentication"
[2026-02-13 10:30:06] planner: 4 tasks
[2026-02-13 10:30:06] [1/4] msg "Analyzing the request..."
[2026-02-13 10:30:07] [1/4] output:
  Analyzing the request. I'll set up JWT authentication with
  login/logout endpoints and middleware for token validation.
[2026-02-13 10:30:07] [1/4] done → notify
[2026-02-13 10:30:07] [2/4] skill:aider {"message": "add JWT auth to main.py"}
[2026-02-13 10:30:18] [2/4] output:
  Applied changes to main.py:
  + added /login and /logout endpoints
  + added jwt_required middleware
[2026-02-13 10:30:18] [2/4] done
[2026-02-13 10:30:18] [3/4] exec "python -m pytest"
[2026-02-13 10:30:21] [3/4] output:
  ===== 3 passed in 0.5s =====
[2026-02-13 10:30:21] [3/4] review: ok
[2026-02-13 10:30:21] [4/4] msg "Summarize what was done and reply"
[2026-02-13 10:30:23] [4/4] output:
  Added JWT authentication to the project. All tests pass.
[2026-02-13 10:30:23] [4/4] done → notify (final)
[2026-02-13 10:30:23] summarizer: 34 msgs → summary updated
```

One file per session, full output inline. `grep` and `tail` are all you need.
