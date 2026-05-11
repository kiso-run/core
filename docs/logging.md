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
[2026-02-13 10:30:07] [2/4] wrapper:aider {"message": "add JWT auth to main.py"}
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

## Prompt-First Debugging

When the planner emits a plan you don't understand — a wrong server name, a missing MCP, a strange path, an unexpected `replan` — the first thing to look at is **the prompt that reached the model**, not the model itself. Kiso uses modern top-tier models; reproducible weirdness almost always means the assembled prompt had a contradiction, a missing rule, or an unexpected context section. (Project rule: see `CLAUDE.md` → "Prompt-first debugging".)

### `KISO_DUMP_PLANNER_PROMPT`

Set this env var to capture the full system + user prompt for every planner LLM call to disk. Off by default — zero overhead when unset.

```bash
# capture to a specific file (recommended)
KISO_DUMP_PLANNER_PROMPT=/tmp/kiso-planner.log kiso ...

# or use the default path (/tmp/kiso-planner-prompt.log)
KISO_DUMP_PLANNER_PROMPT=1 kiso ...
```

Each planner turn appends a timestamped block:

```
===== PLANNER PROMPT DUMP @ 2026-05-10T23:49:45 =====
--- session=func-9cc041fbcb74 replan=False user_msg_preview='naviga a https://example.com ...' ---
--- SYSTEM PROMPT ---
<full system prompt, including all modules selected by the briefer>
--- USER CONTEXT ---
<assembled user context: facts, recent messages, workspace, briefing, MCP catalog, ...>
===== END PROMPT DUMP =====
```

### Inside Docker tests

The functional/extended tier runs pytest inside a container. To pipe the env var through and extract the dump, override docker-compose:

```bash
docker compose -f docker-compose.test.yml run --build --rm \
  -e OPENROUTER_API_KEY \
  -e KISO_DUMP_PLANNER_PROMPT=/dumps/dump.log \
  -v /tmp/kiso-audit:/dumps \
  test-functional \
  uv run pytest tests/functional/test_<failing>.py --functional --extended
```

`docker-compose.test.yml` already forwards `KISO_DUMP_PLANNER_PROMPT` to the container — only the volume mount is needed.

### What to look for

The dump is the ground truth. Read it end-to-end and compare what the planner saw against the plan it emitted:

- **Path issues** (`ENOENT`, wrong base dir): check `Exec CWD` line + `## Session Workspace` listing. If those are correct but the file ended up elsewhere, the MCP's own `cwd` is the culprit, not the planner.
- **Wrong MCP routing**: check the `## MCP Methods` section of the briefing — was the right method even visible? If not, the augmenter (`_augment_capability_matches` in `kiso/brain/common.py`) dropped it.
- **Server-name hallucination** (e.g. `playwright` instead of `browser`): the resolver at `validate_plan` normalizes by method when method is unique. Confirm by grepping the dump for both names.
- **Contradictory rules**: scan the system prompt for pairs of rules that produce opposite outputs on the same input. Real prompt contradictions hide in 20k+ char prompts; the dump makes them readable.

### When NOT to use

For unit tests, mock LLM calls — there's no real prompt to dump. For deterministic bugs (validator errors, schema mismatches, code paths that never reach the LLM), traditional logging beats prompt-dumping. Reach for `KISO_DUMP_PLANNER_PROMPT` when the model is involved and you'd otherwise be tempted to blame "LLM variance".
