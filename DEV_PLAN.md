# Development Plan

Working document. Tracks what to build, in what order, and how to verify each step.

## How to use this file

- Work top to bottom — each milestone builds on the previous
- Check boxes as you go: `- [x]` when done
- Each milestone ends with a verification step — don't move on until it passes
- If a task turns out harder than expected, break it into sub-tasks inline
- Add notes under tasks as needed during development

## Principles

- **Agile**: smallest testable increment first, then layer on
- **No dead code**: every line written is immediately reachable and testable
- **Fail loud**: missing config, broken provider, invalid input → clear error, never silent fallback
- **Tested**: every milestone adds tests for its code. `uv run pytest` must pass before moving on.

---

## Milestone 1: Project skeleton + health check

Get a running server that responds to `/health`. Proves the project structure, config loading, and FastAPI setup work.

- [x] Create `pyproject.toml` with dependencies: `fastapi`, `uvicorn`, `tomli` (or `tomllib` on 3.11+), `aiosqlite`
- [x] Create `kiso/config.py`
  - [x] Load `~/.kiso/config.toml` with TOML parser
  - [x] Validate required sections: `[tokens]`, `[providers]`, `[users]`
  - [x] Validate each user has `role` (admin/user), users with role=user have `skills`
  - [x] Validate token names and usernames match `^[a-z_][a-z0-9_-]{0,31}$`
  - [x] Detect duplicate aliases across users → error
  - [x] Load `[settings]` with defaults
  - [x] Exit with clear error if anything is missing/invalid
- [x] Create `kiso/main.py`
  - [x] FastAPI app
  - [x] Load config at startup
  - [x] `GET /health` → `{"status": "ok"}`
- [x] Create test config file for development
- [x] Set up dev container
  - [x] Create `Dockerfile` (python:3.12-slim + git + curl + uv, workdir `/opt/kiso`)
  - [x] Create `docker-compose.yml` with source bind-mount and `sleep infinity`
  - [x] Create `.dockerignore`
- [x] Set up test infrastructure
  - [x] Add test dependencies to `pyproject.toml`: `pytest`, `pytest-asyncio`, `httpx`, `pytest-cov`
  - [x] Create `tests/conftest.py` with shared fixtures (test config, async client)
  - [x] Write `tests/test_config.py`: valid load, missing sections, invalid names, duplicate aliases, user role validation
  - [x] Write `tests/test_health.py`: GET /health → 200 + {"status": "ok"}

**Verify:**
```bash
docker compose up -d
docker compose exec dev uv sync --group dev
docker compose exec dev uv run pytest --cov=kiso -q   # all tests pass, coverage reported
docker compose exec dev uv run kiso serve &
curl http://localhost:8333/health                      # → {"status": "ok"}
# Remove [tokens] from config → server refuses to start with clear error
```

---

## Milestone 2: Database + message storage

Messages go in, get stored, can be retrieved via `/status`.

- [x] Create `kiso/store.py`
  - [x] Initialize SQLite at `~/.kiso/store.db`
  - [x] Create all 8 tables (sessions, messages, plans, tasks, facts, learnings, pending, published) with indexes
  - [x] Parameterized queries only — never string concatenation
  - [x] Messages table: include `role` column (`user` | `assistant` | `system`)
  - [x] Tasks table: include `stderr` column (exec/skill only)
  - [x] Core functions: `save_message`, `get_session`, `create_session`, `mark_message_processed`, `get_unprocessed_messages`
- [x] Add auth as FastAPI `Depends()` dependency in `kiso/auth.py`
  - [x] Extract `Authorization: Bearer <token>` header
  - [x] Match against `config.tokens` → token name or 401
  - [x] Apply to all endpoints except `/health` and `/pub/{id}`
- [x] Implement `POST /msg`
  - [x] Validate session ID: `^[a-zA-Z0-9_@.-]{1,255}$`
  - [x] Resolve user: direct username match → alias match via token name → untrusted
  - [x] If not whitelisted: save with `trusted=0`, respond 202, stop
  - [x] If whitelisted: save with `processed=0`, enqueue `{message, role, allowed_skills}`, respond `202 {"queued": true, "session": "..."}`
  - [x] If session doesn't exist: create implicitly
- [x] Implement `GET /status/{session}`
  - [x] Return: tasks, queue_length, plan, worker_running, active_task (currently running or null)
  - [x] Support `?after={id}` parameter: return only tasks with id > after (for polling)
- [x] Implement `GET /sessions?user=...`
  - [x] Resolve user from `user` query param + token name (same logic as POST /msg)
  - [x] Return objects: `{session, connector, description, updated_at}`
  - [x] Filter: only sessions where user has messages; admin + `?all=true` → all

**Verify:**
```bash
# Valid token, known user → 202, message in DB
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"hello"}'
# Invalid token → 401
# Unknown user → 202 (but trusted=0 in DB)
# GET /status/test → empty tasks
# GET /sessions → ["test"]
# Check store.db directly: message rows, session created
```

---

## Milestone 3: LLM client + basic planner

Send a message, get a plan back. No execution yet — just prove the LLM integration works.

- [x] Create `kiso/llm.py`
  - [x] `get_provider(model_string)` → resolve provider from config (split on `:`)
  - [x] Resolve API key from env var
  - [x] `call_llm(role, messages, response_format=None)` → generic OpenAI-compatible call
  - [x] Structured output support: pass `response_format` with JSON schema
  - [x] Error handling: provider not found, API key missing, HTTP errors, timeouts
  - [x] Clear error if provider doesn't support structured output
- [x] Create `kiso/brain.py` (planner only for now)
  - [x] Build planner context: facts (empty), pending (empty), summary (empty), last N messages, recent msg outputs (all msg task outputs since last summarization), skills (empty), role, new message
  - [x] Read system prompt from `~/.kiso/roles/planner.md`
  - [x] Call planner with structured output schema
  - [x] Semantic validation — all 6 rules:
    1. `exec` and `skill` tasks must have non-null `expect`
    2. `msg` tasks must have `expect = null`
    3. Last task must be `type: "msg"`
    4. Every `skill` reference must exist in installed skills (deferred to M7)
    5. Every `skill` task's `args` must validate against skill's `kiso.toml` schema (deferred to M7)
    6. `tasks` list must not be empty
  - [x] Retry on validation failure with specific error feedback (up to `max_validation_retries`)
- [x] Create `~/.kiso/roles/planner.md` (initial system prompt with few-shot examples, rules, task templates) — default embedded in brain.py, overridable from file
- [x] Wire into `POST /msg`: after saving message, call planner, log the plan (don't execute)

> **Deferred**: paraphraser (rewrites untrusted messages before planner context) — implemented in M10. Until then, untrusted messages are excluded from planner context entirely.

**Verify:**
```bash
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"what is 2+2?"}'
# Check logs: planner called, plan JSON logged
# Plan should have goal + tasks (at least one msg task)
# Send ambiguous message → plan should be a single msg asking for clarification
```

---

## Milestone 4: Worker + exec + msg task execution

The worker loop runs, executes tasks, stores output. First time we see actual results.

- [x] Create `kiso/worker.py`
  - [x] Per-session asyncio worker: loop draining an in-memory queue
  - [x] Atomic check-and-spawn in `main.py` (no await between checking workers dict and creating task)
  - [x] On message: mark processed, call planner (via brain.py)
  - [x] Create plan in DB, persist tasks
  - [x] Execute tasks one by one
- [x] Implement exec task execution
  - [x] `asyncio.create_subprocess_shell` with `cwd=~/.kiso/sessions/{session}/`
  - [x] Clean env (only PATH)
  - [x] Capture stdout + stderr
  - [x] Timeout from config (`exec_timeout`)
  - [x] Update task status + output in DB
- [x] Implement msg task execution
  - [x] Create `~/.kiso/roles/worker.md` (system prompt) — default embedded, overridable from file
  - [x] Call worker LLM with: facts + summary + task detail (worker does NOT see the conversation — all context must be in the planner's `detail` field)
  - [x] Store generated text as task output
- [x] Create session workspace directory on first use (`~/.kiso/sessions/{session}/`)
- [x] Update `GET /status` to return real tasks and plan info (queue_length, worker_running now live)
- [x] Implement plan status lifecycle: running → done | failed
- [x] Implement worker idle timeout (`worker_idle_timeout`, default 300s)
  - [x] After draining queue: wait on queue with timeout
  - [x] On timeout: shut down worker (ephemeral secrets lost)

> **Deferred**: task output sanitization (strip secrets from output) — implemented in M10. Until then, output is stored raw. Also deferred: plan_outputs chaining (M6), review (M5).

**Verify:**
```bash
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"list files in current directory"}'
# Poll /status/test → see exec task with ls output + msg task with summary
# Plan status should be "done"

curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"what is the capital of France?"}'
# Poll /status/test → see msg task with answer
```

---

## Milestone 5: Reviewer + replan

Failed tasks get caught, plans get revised. The agent becomes self-correcting.

- [ ] Implement reviewer in `brain.py`
  - [ ] Structured output schema: `{status, reason, learn}`
  - [ ] Reviewer receives: process goal + task detail + task expect + task output (fenced) + original user message
  - [ ] Create `~/.kiso/roles/reviewer.md`
  - [ ] Call after every exec and skill task (never for msg)
  - [ ] Validate: if replan, reason must be non-null — retry reviewer up to `max_validation_retries` if missing
- [ ] Implement replan flow in `worker.py`
  - [ ] On `status: "replan"`: notify user FIRST (automatic webhook/status msg with reviewer's `reason`)
  - [ ] Collect completed tasks (with outputs), remaining tasks, failure info
  - [ ] Build `replan_history`: list of previous replan attempts `{goal, failure, what was tried}` — prevents repeating same mistakes
  - [ ] Call planner with enriched context (all normal context + completed, remaining, failure, replan_history)
  - [ ] Mark current plan as failed, create new plan with `parent_id`
  - [ ] Mark old remaining tasks as failed
  - [ ] Persist new tasks, continue execution
  - [ ] Track replan depth, stop at `max_replan_depth` (default 3) — notify user of failure, move on
- [ ] Store learnings from reviewer `learn` field in `store.learnings`

**Verify:**
```bash
# Send a message that will cause a predictable exec failure
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"run the tests in /nonexistent/path"}'
# Poll /status → see: exec fails → reviewer says replan → new plan → eventual msg to user
# Check DB: two plans linked by parent_id, first plan failed, second done
```

---

## Milestone 6: Task output chaining

Later tasks can use outputs from earlier tasks in the same plan.

- [ ] Implement plan_outputs accumulation in worker
  - [ ] After each task completes: append `{index, type, detail, output, status}` to list
  - [ ] Before exec: write `{workspace}/.kiso/plan_outputs.json`
  - [ ] Before skill: add `plan_outputs` to input JSON
  - [ ] Before msg: include fenced outputs in worker prompt
- [ ] Clean up `plan_outputs.json` after plan completion

**Verify:**
```bash
# Send a message requiring chaining: "search for X, then summarize the results"
# (requires a skill — can test with exec chaining first)
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"create a file hello.txt with content hello world, then show me its contents"}'
# Plan: exec(echo hello world > hello.txt) → exec(cat hello.txt) → msg
# Second exec should succeed because file was created by first
# The msg task should reference the cat output
```

---

## Milestone 7: Skills system

Third-party capabilities via subprocess.

- [ ] Create `kiso/skills.py`
  - [ ] Discover skills: scan `~/.kiso/skills/`, skip `.installing` markers
  - [ ] Parse `kiso.toml`: validate type, name, summary, args schema, env declarations, session_secrets, `[kiso.deps]` (python version, bin list)
  - [ ] Check `[kiso.deps].bin` entries with `which` (warn if missing)
  - [ ] Build planner skill list (one-liner + args schema per skill)
  - [ ] Validate skill task args against schema (type checking, required/optional, max 64KB, max depth 5)
- [ ] Implement skill execution in worker
  - [ ] Build input JSON: args + session + workspace + scoped session_secrets + plan_outputs
  - [ ] Run: `.venv/bin/python ~/.kiso/skills/{name}/run.py` via subprocess, pipe stdin, capture stdout/stderr, `cwd=~/.kiso/sessions/{session}`
  - [ ] Timeout from config
- [ ] Create a test skill for development (e.g. echo skill that returns its input)
- [ ] Wire skill discovery into planner context (rescan on each planner call)

**Verify:**
```bash
# Install test skill manually (create directory + kiso.toml + run.py)
# Send message that should trigger the skill
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"search for python async patterns"}'
# Poll /status → see skill task with output from the skill subprocess
# Verify skill received correct input JSON (args, session, workspace, plan_outputs)
```

---

## Milestone 8: Webhook delivery + POST /sessions

Connectors can register sessions and receive responses via webhook.

- [ ] Implement `POST /sessions`
  - [ ] Create/update session with connector name (from token), webhook URL, description
  - [ ] Webhook URL validation: reject private IPs, DNS rebinding check, non-HTTP schemes
  - [ ] `webhook_allow_list` exception from config
- [ ] Implement webhook delivery in worker
  - [ ] After each msg task: POST to session webhook (if set)
  - [ ] Payload: `{session, task_id, type, content, final}`
  - [ ] `final: true` only on last msg task after all reviews pass
  - [ ] Retry: 3 attempts, backoff 1s/3s/9s
  - [ ] On all failures: log, continue (output stays in /status)

**Verify:**
```bash
# Start a simple HTTP server to receive webhooks
python -m http.server 9999 &
# Register session with webhook
curl -X POST localhost:8333/sessions -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"webhook-test","webhook":"http://localhost:9999/callback"}'
# Send message
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"webhook-test","user":"marco","content":"hello"}'
# Check HTTP server logs for webhook POST
# Verify final=true on last msg
```

---

## Milestone 9: Knowledge system (facts, learnings, curator, summarizer)

The bot learns and remembers across sessions.

- [ ] Implement curator in `brain.py`
  - [ ] Create `~/.kiso/roles/curator.md`
  - [ ] Structured output schema: `{evaluations: [{learning_id, verdict, fact, question, reason}]}`
  - [ ] Run after worker finishes processing a message, only if pending learnings exist
  - [ ] Must run before summarizer (learnings evaluated first)
  - [ ] For each evaluation:
    - `promote`: save `fact` to `store.facts` (source="curator"), mark learning "promoted"
    - `ask`: save `question` to `store.pending` (scope=session, source="curator"), mark learning "promoted"
    - `discard`: mark learning "discarded" with reason
- [ ] Implement summarizer in `brain.py`
  - [ ] Create `~/.kiso/roles/summarizer.md`
  - [ ] Message summarization: current summary + oldest messages + their msg task outputs → new summary
  - [ ] Trigger when raw messages >= `summarize_threshold`
  - [ ] Update `store.sessions.summary`
- [ ] Implement fact consolidation
  - [ ] Trigger when facts > `knowledge_max_facts`
  - [ ] Call summarizer to merge/deduplicate
  - [ ] Replace old fact entries with consolidated ones
- [ ] Wire facts + pending + summary into planner context
  - [ ] Facts are global (visible to all sessions)
  - [ ] Pending items: global + session-scoped (planner sees both)
- [ ] Wire facts + summary into worker context

**Verify:**
```bash
# Have a multi-turn conversation where the reviewer learns something
# Check DB: learnings table has entries
# Check DB: after curator runs, some learnings promoted to facts
# Start a new session → planner context includes facts from previous session (facts are global)
# Send many messages → summarizer runs → session summary updated
# Verify planner sees the summary in context
```

---

## Milestone 10: Security hardening

Lock down permissions, sandboxing, prompt injection defense. Paraphraser and secret sanitization (deferred from M3/M4) land here.

- [ ] Implement exec command deny list
  - [ ] Check command against destructive patterns before execution
  - [ ] Block: `rm -rf /`, `dd if=`, `mkfs`, `chmod -R 777 /`, `chown -R`, `shutdown`, `reboot`, fork bomb
  - [ ] Only bare `/`, `~`, `$HOME` targets are blocked — `rm -rf ./build/` is allowed
- [ ] Implement runtime permission re-validation
  - [ ] Before each task: re-read user role and skills from config
  - [ ] If user removed → fail task, cancel remaining
  - [ ] If role downgraded → enforce sandbox
  - [ ] If skill removed → fail skill task
- [ ] Implement exec sandbox for user role
  - [ ] Create dedicated Linux user per session
  - [ ] Set workspace ownership + `chmod 700`
  - [ ] Run exec as restricted user via subprocess `user=` parameter
- [ ] Implement paraphraser
  - [ ] Reuse summarizer model
  - [ ] Batch rewrite untrusted messages in third person
  - [ ] Strip literal commands and instructions
- [ ] Implement random boundary fencing
  - [ ] `secrets.token_hex(16)` per LLM call (128-bit)
  - [ ] Escape `<<<.*>>>` → `«««...»»»` before fencing
  - [ ] Fence: untrusted messages in planner, task output in reviewer/worker/replan
- [ ] Implement secret sanitization
  - [ ] Known values: deploy + ephemeral secrets
  - [ ] Strip: plaintext, base64, URL-encoded variants
  - [ ] Apply to all task output before storage and LLM inclusion

**Verify:**
```bash
# Exec deny list
curl -X POST localhost:8333/msg -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"test","user":"marco","content":"run rm -rf /"}'
# → task fails immediately with explanation

# Runtime re-validation: remove user from config mid-execution
# → next task fails, remaining cancelled

# Sandbox: send exec as user role → verify it can't read outside workspace

# Fencing: check LLM prompts in audit log → untrusted content wrapped with random tokens
```

---

## Milestone 11: Cancel mechanism

Users can abort running plans.

- [ ] Implement `POST /sessions/{session}/cancel` in main.py
  - [ ] Set cancel flag on worker (in-memory)
  - [ ] Return `{cancelled: true, plan_id}` or `{cancelled: false}`
- [ ] Implement cancel check in worker loop
  - [ ] Check flag between tasks (not mid-task)
  - [ ] Mark remaining tasks as `cancelled`
  - [ ] Mark plan as `cancelled`
  - [ ] Generate cancel summary msg (automatic, not from planner)
  - [ ] Include: completed tasks, skipped tasks, suggestions for next steps
  - [ ] Deliver via webhook + /status with `final: true`

**Verify:**
```bash
# Send a message that will take multiple tasks
# Call cancel while tasks are executing
curl -X POST localhost:8333/sessions/test/cancel -H "Authorization: Bearer $TOKEN"
# → remaining tasks cancelled, cancel summary delivered
# → next message on same session processes normally
```

---

## Milestone 12: Ephemeral secrets

User-provided credentials during conversation.

- [ ] Implement secret extraction from planner output
  - [ ] Planner returns `secrets: [{key, value}]`
  - [ ] Store in worker memory (dict), never in DB
  - [ ] Log: "N secrets extracted" (no values)
- [ ] Pass scoped secrets to skills
  - [ ] Read `session_secrets` declaration from `kiso.toml`
  - [ ] Include only declared keys in skill input JSON `session_secrets` field
- [ ] Implement deploy secret management
  - [ ] `POST /admin/reload-env`: read `~/.kiso/.env`, update process env
  - [ ] Enforce admin-only: resolve user from token → check role → `403 Forbidden` if not admin
  - [ ] Response: `{"reloaded": true, "keys_loaded": N}`

**Verify:**
```bash
# Send message with credentials: "use this API key: sk-test123"
# Planner should extract into secrets field
# Skill should receive it in session_secrets (only if declared)
# Check: secret value never appears in DB, audit logs, or task output
```

---

## Milestone 13: Audit trail

Structured logging for all LLM calls, task executions, reviews, webhooks.

- [ ] Create `kiso/audit.py`
  - [ ] Write JSONL to `~/.kiso/audit/{YYYY-MM-DD}.jsonl`
  - [ ] Entry types: `llm`, `task`, `review`, `webhook`
  - [ ] Secret masking: strip known values (plaintext, base64, URL-encoded) from all entries
- [ ] Wire audit logging into:
  - [ ] `llm.py`: log every LLM call (role, model, tokens, duration, status)
  - [ ] `worker.py`: log every task execution (type, status, duration, output_length)
  - [ ] `worker.py`: log every review (verdict, has_learning)
  - [ ] `worker.py`: log every webhook delivery (url, status, attempts)

**Verify:**
```bash
# Send a message, let it process
# Check ~/.kiso/audit/$(date +%F).jsonl
# Verify: llm entries for planner + worker, task entries, no secret values in logs
```

---

## Milestone 14: CLI

Interactive chat client and management commands. Full spec: [docs/cli.md](docs/cli.md).

### 14a. Core CLI + argument parsing

- [ ] Create `kiso/cli.py` with argument parser (argparse)
  - [ ] Subcommands: `serve`, `skill`, `connector`, `sessions`, `env`
  - [ ] No subcommand → chat mode (default)
- [ ] `kiso serve`: start HTTP server (wraps uvicorn)

### 14b. Chat mode REPL

- [ ] Chat mode: `kiso [--session SESSION] [--api URL] [--quiet]`
  - [ ] Always uses the token named `cli` from config
  - [ ] `--api`: connect to remote kiso instance (default: `http://localhost:8333`)
  - [ ] `--quiet` / `-q`: only show `msg` task content (hide decision flow)
  - [ ] Default session: `{hostname}@{whoami}`
  - [ ] REPL loop: prompt → POST /msg → poll /status → render → repeat
  - [ ] Exit on `Ctrl+C` at prompt or `exit` command
  - [ ] `Ctrl+C` during execution → `POST /sessions/{session}/cancel`

### 14c. Display renderer (`kiso/render.py`)

The renderer shows the full decision flow by default — every planning step, task execution, review verdict, and replan is visible. See [docs/cli.md — Display Rendering](docs/cli.md#display-rendering).

- [ ] Create `kiso/render.py` — stateless renderer that maps `/status` events to terminal output
- [ ] Terminal capability detection at startup
  - [ ] Color: `TERM` contains `256color` or `COLORTERM` set → 256-color; else no color
  - [ ] Unicode: `LC_ALL` / `LANG` contains `UTF-8` → Unicode icons; else ASCII fallback
  - [ ] Width: `os.get_terminal_size()`, fallback 80
  - [ ] TTY: `sys.stdout.isatty()` → if not TTY: no spinner, no truncation, no color (pipe-friendly)
- [ ] Plan rendering
  - [ ] `◆ Plan: {goal} ({N} tasks)` — bold cyan
  - [ ] On replan: `↻ Replan: {new goal} ({N} tasks)` with reviewer reason in red
  - [ ] On max replan depth: `⊘ Max replans reached ({N}). Giving up.` in bold red
- [ ] Task rendering (per task, real-time as `/status` polling delivers updates)
  - [ ] Header: icon + `[{i}/{total}] {type}: {detail}` — yellow for exec/skill, green for msg
  - [ ] Icons: `▶` exec, `⚡` skill, `💬` msg (ASCII fallback: `>`, `!`, `"`)
  - [ ] Spinner on active task: braille animation (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`), 80ms cycle, replaces with final icon
  - [ ] Output lines: indented with `┊`, dim color
  - [ ] Output truncation: first 20 lines shown (10 if terminal < 40 rows), rest collapsed behind `... (N more lines, press Enter to expand)`. Expansion is inline, no scrollback modification.
  - [ ] `msg` task output never truncated — it's the bot's response
- [ ] Review rendering
  - [ ] `✓ review: ok` — green (ASCII: `ok`)
  - [ ] `✗ review: replan — "{reason}"` — bold red (ASCII: `FAIL`)
  - [ ] `📝 learning: "{content}"` — magenta (ASCII: `+ learning: ...`)
- [ ] Cancel rendering
  - [ ] `⊘ Cancelling...` on Ctrl+C
  - [ ] `⊘ Cancelled. {N} of {M} tasks completed.` with done/skipped summary
- [ ] Non-TTY output: plain text, no ANSI codes, no spinner, no truncation (pipe-friendly)

### 14d. Skill management

- [ ] `kiso skill install {name|url}` / `update` / `remove` / `list` / `search`
  - [ ] Install flow: git clone → validate kiso.toml → deps.sh → uv sync → check env vars
  - [ ] Official repos: `git@github.com:kiso-run/skill-{name}.git`
  - [ ] `.installing` marker during install (prevents discovery)
  - [ ] Naming convention: official → name, unofficial URL → `{domain}_{ns}_{repo}`, `--name` override
  - [ ] URL-to-name: strip `.git`, normalize SSH/HTTPS, lowercase, `.`→`-` in domain, `/`→`_`
  - [ ] Unofficial repo warning + deps.sh display before confirmation
  - [ ] `--no-deps` flag: skip deps.sh execution
  - [ ] `--show-deps` flag: display deps.sh without installing
  - [ ] `skill search`: query GitHub API (`org:kiso-run+topic:kiso-skill`)
  - [ ] `skill update all`: update all installed skills

### 14e. Connector management

- [ ] `kiso connector install` / `update` / `remove` / `list` / `search`
  - [ ] Same flow as skills but validate `type = "connector"` and `[kiso.connector]` section
  - [ ] Official repos: `git@github.com:kiso-run/connector-{name}.git`
  - [ ] If `config.example.toml` exists and `config.toml` doesn't → copy it
  - [ ] `connector search`: query GitHub API (`org:kiso-run+topic:kiso-connector`)
- [ ] `kiso connector {name} run` / `stop` / `status`
  - [ ] Daemon subprocess management with PID tracking
  - [ ] Logs: `~/.kiso/connectors/{name}/connector.log`
  - [ ] Exponential backoff restart on crash, stop after repeated failures

### 14f. Session + env management

- [ ] `kiso sessions [--all]`
- [ ] `kiso env set` / `get` / `list` / `delete` / `reload`
  - [ ] Manage `~/.kiso/.env`
  - [ ] `reload` calls `POST /admin/reload-env`

**Verify:**
```bash
# --- Chat mode (default, verbose) ---
kiso --session test
# You: list files in the current directory
# ◆ Plan: List files and report (2 tasks)
# ▶ [1/2] exec: ls -la ⠋
#   ┊ total 24
#   ┊ drwxr-xr-x 3 user user 4096 ...
#   ✓ review: ok
# 💬 [2/2] msg
# Bot: Here are the files in the current directory: ...
# Ctrl+C to exit

# --- Quiet mode ---
kiso --session test --quiet
# You: list files
# Bot: Here are the files: ...

# --- Replan visible in verbose mode ---
kiso --session test
# You: run the tests in /nonexistent
# ◆ Plan: Run tests (2 tasks)
# ▶ [1/2] exec: cd /nonexistent && pytest
#   ┊ bash: cd: /nonexistent: No such file or directory
#   ✗ review: replan — "Directory does not exist"
# ↻ Replan: Inform user about missing directory (1 task)
# 💬 [1/1] msg
# Bot: The directory /nonexistent doesn't exist. ...

# --- Pipe-friendly output (no colors, no spinner) ---
echo "hello" | kiso --session test > output.txt

# --- Management ---
kiso skill list   # → lists installed skills
kiso sessions     # → lists sessions
kiso env list     # → lists KISO_* keys
```

---

## Milestone 15: Docker

Package everything for production.

- [ ] Create `Dockerfile`
  - [ ] Base: `python:3.12-slim`
  - [ ] Install: git, curl, uv
  - [ ] Copy project, `uv sync`
  - [ ] Volume: `/root/.kiso`
  - [ ] Expose 8333
  - [ ] Healthcheck: `curl -f http://localhost:8333/health`
  - [ ] CMD: `uv run kiso serve`
- [ ] Create `docker-compose.yml`
  - [ ] Service: kiso, port 8333, volume kiso-data
  - [ ] Environment variables for deploy secrets
  - [ ] `restart: unless-stopped`
- [ ] Test pre-installing skills in Dockerfile

**Verify:**
```bash
docker compose build
docker compose up -d
curl http://localhost:8333/health  # → ok
docker exec -it kiso kiso env set KISO_OPENROUTER_API_KEY sk-...
docker exec -it kiso kiso env reload
# Send a message via curl → get response
# docker compose down && docker compose up → message recovery works
```

---

## Milestone 16: Startup recovery + production hardening

Crash-proof the system.

- [ ] Message recovery on startup
  - [ ] Query `processed=0 AND trusted=1` messages
  - [ ] Re-enqueue to session queues, spawn workers
- [ ] Plan/task recovery on startup
  - [ ] Mark `running` plans as `failed`
  - [ ] Mark `running` tasks as `failed`
- [ ] Input validation (all endpoints)
  - [ ] Session IDs: `^[a-zA-Z0-9_@.-]{1,255}$`
  - [ ] Usernames: `^[a-z_][a-z0-9_-]{0,31}$`
  - [ ] Skill args: max 64KB, max depth 5
- [ ] Output size limits
  - [ ] Exec/skill output: max 1MB, truncate with warning
- [ ] Rate limiting
  - [ ] Per-token: max requests/minute on `/msg` and `/sessions`
  - [ ] Per-user: max concurrent messages in processing
  - [ ] Per-session: max queued messages before rejecting
- [ ] Graceful shutdown
  - [ ] SIGTERM: finish current task, cancel remaining, close DB

**Verify:**
```bash
# Send message, kill server mid-execution
# Restart → unprocessed messages re-enqueued, running tasks marked failed
# Send message with invalid session ID → 400
# Send exec that produces huge output → truncated with warning
```

---

## Milestone 17: Session and server logging

Human-readable logs for debugging and monitoring.

- [ ] Server log: `~/.kiso/server.log`
  - [ ] Startup, auth events, worker lifecycle, errors
- [ ] Session log: `~/.kiso/sessions/{session}/session.log`
  - [ ] Messages received, plans created, task execution + output, reviews, replans

**Verify:**
```bash
# Run a full conversation
# Check server.log: startup, auth ok entries
# Check session.log: message → planner → tasks → reviews → plan done
```

---

## Milestone 18: Published files

Exec/skill tasks can publish downloadable files.

- [ ] Implement `GET /pub/{id}` in main.py
  - [ ] Look up UUID in `store.published`
  - [ ] Serve file with Content-Type and Content-Disposition
  - [ ] No authentication required
- [ ] Implement file publishing in store.py
  - [ ] `publish_file(session, filename, path)` → UUID4
  - [ ] Files in `~/.kiso/sessions/{session}/pub/`

**Verify:**
```bash
# Manually insert a published file entry
# curl localhost:8333/pub/{uuid} → downloads the file
# Random UUID → 404
```

---

## Done

When all milestones are checked off, kiso is production-ready per the documentation spec.
