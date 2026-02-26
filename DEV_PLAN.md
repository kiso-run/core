# Development Plan

Working document. Tracks what was built and key decisions made along the way.

## Principles

- **Agile**: smallest testable increment first, then layer on
- **No dead code**: every line written is immediately reachable and testable
- **Fail loud**: missing config, broken provider, invalid input → clear error, never silent fallback
- **Tested**: every milestone adds tests. `uv run pytest` must pass before moving on.

---

## Completed Milestones

### M1: Project skeleton + health check
FastAPI app, config loading (`kiso/config.py` with full validation), SQLite init, `GET /health`, Docker dev container, pytest infrastructure.

### M2: Database + message storage
`kiso/store.py` (8 tables: sessions, messages, plans, tasks, facts, learnings, pending, published), bearer-token auth, `POST /msg`, `GET /status/{session}`, `GET /sessions`.

### M3: LLM client + basic planner
`kiso/llm.py` (OpenAI-compatible, structured output), `kiso/brain.py` (planner, semantic validation with 6 rules, retry on validation failure).

### M4: Worker + exec + msg task execution
Per-session asyncio worker loop, exec task (subprocess, timeout, sandbox uid), msg task (LLM call → text), plan lifecycle (running/done/failed), worker idle timeout.

### M5: Reviewer + replan
Reviewer (`ok`/`replan`/`learn` structured output), replan flow with `replan_history` (prevents repeating mistakes), `max_replan_depth`, learning storage in `learnings` table.

### M6: Task output chaining
`plan_outputs` accumulated per plan; written to `{workspace}/.kiso/plan_outputs.json` before exec, passed to skills as input JSON, included in msg worker prompt.

### M7: Skills system
`kiso/skills.py`: discovery (`~/.kiso/skills/`), `kiso.toml` validation (type, name, args schema, env, deps), skill execution (subprocess stdin/stdout JSON), schema validation for args.

### M8: Webhook delivery + POST /sessions
Session registration with connector name + webhook URL. Webhook delivery (3 retries, 1s/3s/9s backoff). `final: true` on last msg task. Private IP/DNS rebinding protection.

### M9: Knowledge system
Curator (promote/ask/discard learnings → facts), summarizer (session summary + fact consolidation triggered by thresholds), facts and pending items injected into planner context.

### M10: Security hardening
Exec deny list, runtime permission re-validation, per-session Linux sandbox user, paraphraser (batch-rewrites untrusted messages in third person), random boundary fencing (128-bit tokens), secret sanitization (plain/base64/URL-encoded), webhook HMAC-SHA256 + HTTPS enforcement.

### M11: Cancel mechanism
`POST /sessions/{session}/cancel` → in-memory cancel flag → remaining tasks cancelled → cancel summary msg with `final: true`.

### M12: Ephemeral secrets
Planner returns `secrets: [{key, value}]` → stored in worker memory only (never DB). Passed to skills scoped to declared `session_secrets`. `POST /admin/reload-env` (admin only).

### M13: Audit trail
`kiso/audit.py` — JSONL per day (`~/.kiso/audit/YYYY-MM-DD.jsonl`), secret masking, event types: `llm`, `task`, `review`, `webhook`.

### M14: Startup recovery + production hardening
Unprocessed message re-enqueueing on startup, running plan/task recovery (→ failed), input validation (session IDs, usernames), output size cap (1MB), rate limiting, graceful SIGTERM shutdown.

### M15: CLI
`kiso/cli.py`: chat REPL (POST /msg → poll /status → render → repeat), `kiso serve/skill/connector/sessions/env` subcommands. Renderer (`kiso/render.py`): spinners (braille), rich markdown, color/unicode detection, non-TTY pipe mode. System env context (`kiso/sysenv.py`, 300s TTL cache).

### M16: Docker
Production `Dockerfile` + `docker-compose.yml`, healthcheck, skill pre-install support.

### M17: Session and server logging
`~/.kiso/server.log` + `~/.kiso/sessions/{session}/session.log` (structured per-session events: messages, plans, tasks, reviews, replans).

### M18: Published files
`GET /pub/{id}` (no auth), `publish_file()` → UUID → `~/.kiso/sessions/{session}/pub/`.

### M19: Review rendering in CLI
`review_verdict`, `review_reason`, `review_learning` columns on tasks; rendered: `✓ review: ok` / `✗ review: replan — "{reason}"` / `📝 learning: "..."`.

### M20a: Live LLM integration tests
`tests/live/`: L1 role isolation (8 tests), L2 partial flows (4), L3 e2e (4). Gated behind `--llm-live` flag + `KISO_LLM_API_KEY`.

### M20b: Practical live tests
L4 acceptance (7: exec chaining, full `_process_message`, multi-turn, replan recovery, knowledge pipeline, skill execution). L5 CLI lifecycle (5: search/connector install/remove, not-found error). `--live-network` flag.

### M21: Security & robustness hardening
- **Deny list bypass**: unit tests for base64 pipe, python -c, variable indirection, eval printf — documented as best-effort; admin exec is trusted-by-design, sandbox is the real wall
- **Fact poisoning**: L4 live tests (manipulative/transient learnings → curator discards)
- **Fact consolidation**: abort if consolidated count < 30% of originals; skip facts < 3 chars
- **Silent planning failure**: `PlanError` → system message saved to DB + webhook delivered
- **Reviewer rubber-stamp**: exit code passed to reviewer context so it knows the command actually failed
- **Replan cost**: per-message LLM call budget (`max_llm_calls_per_message`); worst-case documented in `docs/security-risks.md`
- **Paraphraser injection**: L2 live tests (literal + encoded injection → neutralized)
- **Secrets in plan detail**: `sanitize_output` applied to task `detail` before DB storage

### M22: CLI UX + architecture refinements
- Rich markdown rendering (`rich>=13`, `_render_markdown()`, graceful ASCII degradation)
- Per-step LLM call display (planner calls shown after plan header; `_KEEP_LLM_CALLS` sentinel in `update_plan_usage` to preserve calls while updating totals)
- Session-aware exec paths (`build_system_env_section(session=)`, absolute workspace in planner + exec translator context)
- Replan `PlanError` recovery → creates recovery msg task instead of silent failure
- System prompts extracted to `kiso/roles/*.md` (8 files, user-overridable from `~/.kiso/roles/`)
- Messenger goal context (`build_messenger_messages(goal=)` adds `## Current User Request`)

### M23: Verbose mode
`/verbose-on` / `/verbose-off` REPL commands; `/status?verbose=true` returns full LLM messages/responses; `render_llm_calls_verbose()` renders rich panels.

### M24: Persistent system directory + reference docs
`~/.kiso/sys/` (gitconfig, ssh/, bin/), bundled `kiso/reference/skills.md` + `connectors.md` synced on startup, extended PATH in exec env, planner prompt instructs to read reference docs before planning unfamiliar tasks.

### M25: Planner-initiated replan (discovery plans)
New `replan` task type: planner can self-direct investigation without an exec failure. Optional `extend_replan` field (+3 extra attempts). `max_replan_depth` default 3→5.

### M26: Direct pub/ file serving
Replaced DB-based publish with HMAC-based URLs: `/pub/{token}/{filename}`. `pub_token()` / `resolve_pub_token()` in `kiso/pub.py`. Exec tasks auto-report pub/ files in output. Removed `published` table.

### M27: Persistent chat input history
Readline history → `~/.kiso/.chat_history` (500 entries), loaded on startup, saved on exit.

### M28: Code quality — deduplication + readability
- `brain.py`: `_retry_llm_with_validation()` generic retry loop; fix double `discover_skills()` call; context messages default 5→7
- `worker.py`: `_deliver_webhook_if_configured()` helper; `discover_skills()` cached per plan
- `main.py`: `WorkerEntry` NamedTuple (replaces raw 3-tuple indexing)
- `store.py`: `_rows_to_dicts()` helper (replaces 12× inline list comprehension)
- `cli_skill.py`/`cli_connector.py`: shared `kiso/plugin_ops.py` (url_to_name, is_url, fetch_registry, search_entries)

### M29: Workspace file awareness
`_collect_workspace_files()` (max 30 entries, excludes `.kiso/` internals, human-readable sizes) injected into planner context as `Workspace files:` + `File search:` guidance (find/grep/rg for deeper search).

### M30: `kiso reset` — cleanup commands
Four levels: `reset session [name]`, `reset knowledge`, `reset all`, `reset factory`. Admin role + `--yes` required. CLI-direct (sync sqlite3, no server needed). `kiso-host.sh` intercepts `reset factory` for auto-restart.

### M31: Searcher role + planner hardening
New `search` task type: structured search tasks → `run_searcher()` (gemini-flash-lite:online via OpenRouter, ~$0.014/query) → results in `plan_outputs`. Task substatus (translating/executing/reviewing/searching/composing) shown in CLI spinner. `append_task_llm_call()` for incremental LLM tracking. Seven planner prompt fixes: anti-hallucination for msg detail, search task type, search-skill preference, registry check, anti-hallucination for replans, search-then-replan pattern, intermediate user feedback. pub/ auto-created before exec tasks. `install.sh` appends MODEL_DEFAULTS as commented TOML after install.

Post-M31 hardening: 11 search execution tests, 5 store function tests, search permission test, 3 live search tests, pub/ chown for sandbox, search params type validation (`max_results` bounds `[1,100]`, lang/country type guards).

Known limitation: verbose LLM panels appear after task completion, not incrementally between phases — `append_task_llm_call()` exists but worker still batches at task end.

### M32: Fast path — skip planner for simple messages
`classify_message()`: single cheap LLM call (`worker` model) returning `"plan"` or `"chat"`, conservative (doubt → plan). Chat messages bypass planner → direct to messenger via `_fast_path_chat()`. Plan + task still created for CLI compatibility. `fast_path_enabled` config (default `true`).

### M33: Worker-level retry for transient exec errors
New `retry_hint` field in reviewer schema: if non-null, reviewer believes a local retry can fix the failure. Worker re-translates with error context + hint, retries once before escalating to full replan. Saves ~2 LLM calls per successful retry. Search tasks also eligible; skill tasks excluded. `max_worker_retries` config (default 1).

### M34: Richer LLM-driven memory consolidation
Enriched facts: `category` (project/user/tool/general), `confidence` (0.0–1.0), `last_used`, `use_count`. `facts_archive` table for soft-archived low-confidence facts. Structured consolidation groups by category, merges duplicates, resolves contradictions. `update_fact_usage()` bumps count/timestamp after successful plan. Session summarizer produces structured output (Summary / Key Decisions / Open Questions / Working Knowledge). New config: `fact_decay_days` (7), `fact_decay_rate` (0.1), `fact_archive_threshold` (0.3).

### M35: CLI → root-level `cli/` package
All CLI modules moved from `kiso/` to `cli/` (8 modules: `__init__`, connector, env, plugin_ops, render, reset, session, skill). `kiso/` now contains only server/bot code. `discover_connectors` + validators extracted to `kiso/connectors.py` to break `sysenv→cli` import dependency.

### M36: Composable worker (`kiso/worker/` package)
Monolithic `kiso/worker.py` (~1400 lines) split into package: `loop.py` (orchestration: `_execute_plan`, `_process_message`, session loop), `exec.py`, `skill.py`, `search.py`, `msg.py`, `utils.py`, `context.py` (`TaskContext`/`TaskResult` dataclasses). `main.py` import unchanged.

### M37: Robustness hardening
- `rglob` on workspace/pub → depth-limited + capped (`_MAX_SCAN=1000`, `_MAX_PUB_SCAN=1000`)
- Silent JSON decode on malformed search args → `log.warning`
- Confidence values clamped to [0.0, 1.0] in `run_fact_consolidation()`
- Connector manifest read errors → `log.warning` (no longer silently skip)
- `setting_bool` no longer coerces int (TOML parses booleans natively; int → config error)
- `fact_consolidation_min_ratio` extracted to config (default 0.3)

### M38: Code quality hardening
- Fact content min length 10→3 (was silently dropping short facts like "Go", "vim", "GPT-4")
- PRAGMA f-string in migrations guarded with `assert table in _known`
- Startup recovery queue-full log message clarified (message stays in DB, retries on restart)
- SSH config: check `config` + `id_ed25519` exist before setting `GIT_SSH_COMMAND`
- Redundant `from pathlib import Path` removed from `sysenv.py` loop body
- `except Exception` in `llm.py` error parsing narrowed to `(json.JSONDecodeError, ValueError, TypeError)`
- `_WEBHOOK_BACKOFF = [1, 3, 9]` extracted from inline literal in `webhook.py`

### M39: `.env` protection + install.sh merge fix
- `install.sh`: merge API key update (preserve all other `.env` entries); always backup before Docker ops; refresh backup post-write
- `security.py`: deny patterns blocking `>` / `>>` redirects to `.kiso/.env` / `.kiso/config.toml`
- `planner.md`: explicit rule — use `kiso env set`, never write directly to config files

### M40: Planner prompt reliability
- `CRITICAL:` prefix on last-task-must-be-msg rule (models were silently skipping it under token pressure)
- Clarify `pub/` filesystem path vs `/pub/` HTTP URL (planner was generating exec tasks using the HTTP path)

### M41: CLI polling UX
- Pre-plan spinner: `planning_phase = True` when `worker_running=True` and no plan exists yet — previously the CLI showed nothing during classifier + planner calls (4–15s blank)
- `_POLL_EVERY` 6→2 (480ms→160ms): fast tasks now appear individually instead of all at once

### M42: Relevant fact retrieval via FTS5
FTS5 virtual table `kiso_facts_fts` on `facts.content` with INSERT/UPDATE/DELETE sync triggers. `_fts5_query()` sanitizes arbitrary input (extracts `\w+` tokens). `search_facts(db, query, *, session, is_admin, limit=15)`: BM25 ranking, session-scoped, fallback to `get_facts()` on empty query or no match. `_migrate()` backfills FTS index for pre-existing facts. Planner uses `search_facts()` (top-15 relevant); messenger keeps `get_facts()` for broader context.

Depends on M43 (implemented first): `search_facts()` respects session-scoping from day one.

### M43: Session-scoped fact isolation
**Problem**: `get_facts()` returned all facts globally — cross-session leakage between connectors/channels.

**Design decision**: strict session-scoping (Option B over Option A). User facts are re-learned naturally per session; complexity of cross-session user tracking not worth the gain at this stage.

**Scoping rules**:
| Category | Scope |
|---|---|
| `project`, `tool`, `general` | Global (all sessions) |
| `user` | Session where generated |
| `user` with `session=NULL` | Global (legacy compatibility) |
| Any, `is_admin=True` | Global (admin oversight) |

`get_facts()` now takes `session` + `is_admin`. Fact consolidation preserves `session` on re-insert (previously silently globalized user facts after every consolidation run).

---

## Role architecture

10 LLM roles in `kiso/roles/`:

| Prompt file | Model route | Function | Purpose |
|---|---|---|---|
| `planner.md` | `planner` | `run_planner` | Message → JSON plan |
| `worker.md` | `worker` | `run_exec_translator` | Task detail → shell command |
| `reviewer.md` | `reviewer` | `run_reviewer` | Task output → ok/replan |
| `messenger.md` | `messenger` | `run_messenger` | Task detail → user message |
| `searcher.md` | `searcher` | `run_searcher` | Search query → web results |
| `curator.md` | `curator` | `run_curator` | Learnings → facts/questions |
| `summarizer-session.md` | `summarizer` | `run_summarizer` | Messages → session summary |
| `summarizer-facts.md` | `summarizer` | `run_fact_consolidation` | Dedup/merge facts |
| `paraphraser.md` | `paraphraser` | `run_paraphraser` | Untrusted msg → safe text |
| `classifier.md` | `worker` | `classify_message` | plan vs chat routing |

Notes:
- `summarizer-session` and `summarizer-facts` share the `summarizer` model route (both compression tasks; `{model}-{action}.md` naming makes it explicit)
- `classifier.md` reuses the `worker` model route (cheap, fast, no structured output needed)
- `searcher` defaults to `google/gemini-2.5-flash-lite:online` (`:online` = Exa web search plugin on OpenRouter, ~$0.014/query)

---

## Deferred items

- **M21**: filter `save_learning` content for secret-like patterns — curator prompt already guards this, revisit if poisoning observed in production
- **M21**: soft-delete old facts instead of hard-delete during consolidation — archive table is sufficient for now
- **M21**: auto-replan on exec failure regardless of reviewer verdict — monitor rubber-stamping in production first
- **M21**: alerting/metrics on high-volume sessions from audit log — operational concern for production scale
- **M31**: true incremental verbose rendering (LLM panels per call, not per task) — `append_task_llm_call()` exists, worker batches at task end; requires design to avoid duplicate data
- **M43**: Option A (cross-session user facts following the user) — can layer on top of Option B if needed in practice
