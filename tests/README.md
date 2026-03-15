# Test Suite

## Why six test levels?

Kiso has a complex runtime: an LLM orchestrator that classifies messages, builds plans,
executes shell commands inside Docker containers, reviews outputs with another LLM call,
manages a knowledge base, and talks back to users through connectors. No single test
strategy can cover all of this effectively, so the suite is split into six levels that
form a pyramid — fast/cheap at the bottom, slow/expensive at the top.

```
                    ┌──────────────┐
                    │  Interactive │  human at terminal
                  ┌─┤  (manual)    ├─┐
                  │ └──────────────┘ │
                ┌─┴──────────────────┴─┐
                │      Functional      │  Docker + real LLMs
                │   (full pipeline)    │
              ┌─┴──────────────────────┴─┐
              │          Live            │  real LLMs, no Docker
              │    (LLM compliance)      │
            ┌─┴──────────────────────────┴─┐
            │           Docker             │  container only, no LLM
            │     (sandbox security)       │
          ┌─┴──────────────────────────────┴─┐
          │         Unit + Bash              │  host only, everything mocked
          │    (logic, schemas, CLI, shell)   │
          └──────────────────────────────────┘
```

Each level answers a different question:

| Level | Question it answers |
|-------|-------------------|
| **Unit** | Does each function do the right thing given controlled inputs? |
| **Bash** | Do the shell helpers in `install.sh`/`host.sh` parse, validate, and derive correctly? |
| **Docker** | Can the sandbox user escape its workspace? Are tool venvs isolated? |
| **Live** | Do real LLM models understand our prompts and return schema-valid output? |
| **Functional** | Does the full pipeline work end-to-end: message in → plan → execute → response out? |
| **Interactive** | Do flows that require a human (CAPTCHA, OAuth, SSH deploy) actually work? |


## Running tests

```bash
# Full suite (recommended before merging)
./run_full_tests.sh

# Individual levels
./run_full_tests.sh --unit      # ~90s, host, no deps
./run_full_tests.sh --live      # ~8min, needs OPENROUTER_API_KEY
./run_full_tests.sh --func      # ~10min, Docker + OPENROUTER_API_KEY
./run_full_tests.sh --docker    # <1s, Docker only

# Quick unit-only during development
uv run pytest tests/ -q

# Single file
uv run pytest tests/test_brain.py -v

# Interactive (never automated, human required)
uv run pytest tests/interactive/ -v --interactive --functional
```


## Unit tests (`tests/test_*.py`)

**~60 files, ~3000+ tests, ~90 seconds on host.**

Everything external is mocked: LLM calls, database, filesystem, network. This makes
them fast, deterministic, and runnable anywhere without credentials or Docker.

Unit tests cover:
- **Schema validation** — every JSON schema the LLM must produce (plan, review, briefing,
  curator verdict) is tested with valid/invalid inputs to catch regressions when prompts change.
- **Prompt construction** — `build_planner_messages()`, `build_reviewer_messages()`, etc.
  are tested to verify they include the right context, respect token budgets, and handle
  edge cases (empty history, no tools installed, replan mode).
- **Worker loop logic** — task dispatch, replan decisions, stuck detection, cancel handling,
  knowledge extraction — all tested with mocked LLM responses and in-memory DB.
- **CLI commands** — every `kiso <subcommand>` is tested by capturing stdout/stderr with
  `capsys` and patching the HTTP layer (`httpx.request`).
- **Security** — exec deny list (dangerous commands), secret sanitization (plaintext,
  base64, URL-encoded, JSON-escaped variants), permission checks.
- **API endpoints** — FastAPI routes tested via `httpx.AsyncClient` with ASGI transport,
  no real server needed.

### Conventions

- **HTTP mocking:** always patch `httpx.request`, not `httpx.get`/`httpx.post` — the CLI
  uses a unified `httpx.request()` call in `_http.py`.
- **Error output:** CLI errors go to stderr — assert on `capsys.readouterr().err`.
- **Config patching:** `patch("cli.env.ENV_FILE", tmp_path / ".env")` for env tests.
- **Briefing mocks:** must include `"relevant_entities": []` (added in M346).
- **Learn strings:** must be >= 15 chars to pass `clean_learn_items()` filtering.
- **Retry backoff:** an autouse fixture (`_no_retry_backoff`) sets all retry delays to 0
  so tests don't wait for real backoff timers.


## Bash tests (`tests/bash/`)

**~90 tests, <5 seconds, requires `bats`.**

These test the pure-bash helper functions in `install.sh` and `host.sh` — the scripts
that manage kiso instances on a host (install, remove, list, stats). They source the
script under test and call individual functions directly, using BATS (Bash Automated
Testing System).

Covered: instance name derivation from git URLs, port allocation, name validation
(regex), `.env` safe reading (no eval), instance registration in `instances.json`,
bash completion, and host-level commands (exec, logs, remove, stats).


## Docker tests (`tests/docker/`)

**~10 tests, <1 second, runs inside Docker.**

These verify **sandbox isolation** — the security boundary between kiso and the exec
tasks it runs. Each kiso session gets a dedicated Linux user that can only access its
own workspace directory. Docker tests verify:

- The sandbox user cannot read files outside its workspace.
- The sandbox user cannot write to system directories.
- Tool virtual environments have their `bin/` correctly prepended to `PATH`.

These run inside the Docker container (via `docker compose`) because they need the real
sandbox user creation and filesystem permission setup. No LLM calls needed.


## Live tests (`tests/live/`)

**~60 tests, ~8 minutes, needs `OPENROUTER_API_KEY`.**

Live tests answer: **"Do real LLMs understand our prompts?"** They make actual API calls
to OpenRouter but mock everything else (subprocess, filesystem, tools). This isolates
the LLM-compliance question from infrastructure concerns.

The key insight: Kiso orchestrates ~10 specialized LLM roles (planner, reviewer, curator,
messenger, etc.), each with its own prompt and JSON schema. When we change a prompt or
switch models, we need to verify the LLM still produces valid, useful output. Unit tests
can't catch this because they mock the LLM response.

Live tests are organized by scope:

- **`test_roles.py`** — tests each role in isolation: "give the planner this message,
  does it produce a valid plan?" Same for reviewer, curator, exec translator, etc.
- **`test_flows.py`** — chains 2-3 roles: "classify → plan → validate" or
  "plan → review → replan". Catches integration issues between roles.
- **`test_e2e.py`** — runs `_execute_plan()` with real LLM but stubbed subprocess.
  Tests the full planning loop without actually executing shell commands.
- **`test_practical.py`** — realistic acceptance scenarios: multi-turn conversation,
  Italian language compliance, knowledge pipeline with real curator.
- **`test_cli_live.py`** — CLI commands that need real network (`kiso tool search`).
- **`test_plugins.py`** — clones official plugin repos, runs their internal tests.

**What live tests do NOT test:** actual command execution, tool installation, Docker
sandbox, filesystem side effects. Those belong to functional tests.


## Functional tests (`tests/functional/`)

**~30 tests, ~10 minutes, Docker + `OPENROUTER_API_KEY`.**

Functional tests are the most expensive and most realistic: they run the **full pipeline
end-to-end** inside a Docker container, with real LLM calls, real subprocess execution,
real tool installation, and real file I/O.

The flow mirrors what happens in production:
```
user message → classify → plan → [install tool] → execute → review → respond
```

Each test sends a natural-language message and asserts on the final response and
side effects (files created, tools installed, facts learned).

Tests are grouped by capability:

- **Browser** (F1-F2): install the browser tool, navigate to a URL, take screenshots.
- **System** (F3-F4): display SSH keys, clone a git repo and edit with aider.
- **Services** (F5-F6): sign up on Moltbook, post a tweet (destructive — real side effects).
- **Research** (F7-F8): web search → synthesize → publish markdown; write and run scripts.
- **Knowledge** (F9-F16): self-inspection, learning pipeline, entity creation, curator
  deduplication, messenger fidelity.

**Why Docker?** Functional tests need the sandbox environment: a real Linux user, real
filesystem permissions, real `apt-get install` for tool dependencies. The test fixture
creates a fresh DB per test and sends messages through `_process_message()` directly
(no HTTP server needed, but uses the real worker loop).

**Destructive tests** (F5-F6) are gated by `--destructive` because they create real
accounts on external services. They never run in CI.


## Interactive tests (`tests/interactive/`)

**Manual only, never automated, gated by `--interactive`.**

Some flows require a real human to act: solving a CAPTCHA, authorizing an OAuth flow,
deploying an SSH key on GitHub. These tests use a `HumanRelay` fixture that pauses
execution and prompts the operator to perform a specific action, then resumes and
verifies the result.

Interactive tests exist because kiso can handle tasks that involve external services
with human-gated steps. The test infrastructure (`HumanRelay`) bridges the gap between
the automated agent and the human operator.


## Pytest markers and gating

Every non-unit test is gated by a pytest marker and a CLI flag. Tests without the
matching flag are **skipped**, not errored — so `uv run pytest tests/` safely runs
only unit tests.

| Marker | Flag | What it gates |
|--------|------|---------------|
| `@pytest.mark.llm_live` | `--llm-live` | Any test making real LLM API calls |
| `@pytest.mark.live_network` | `--live-network` | Tests calling external services (GitHub, registries) |
| `@pytest.mark.functional` | `--functional` | Full pipeline tests in Docker |
| `@pytest.mark.destructive` | `--destructive` | Tests with irreversible side effects (account creation) |
| `@pytest.mark.interactive` | `--interactive` | Tests requiring a human at the terminal |

Gating logic is in `tests/conftest.py:pytest_collection_modifyitems`. The functional
marker uses `iter_markers()` instead of keyword lookup because pytest adds the directory
name "functional" as a keyword to every test in `tests/functional/`, which would falsely
match.


## Key fixtures

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `db` | function | In-memory SQLite — fresh per test, no state leaks |
| `client` | function | httpx `AsyncClient` wired to FastAPI via ASGI transport (no server) |
| `test_config` | function | Config loaded from an inline TOML with safe defaults |
| `_no_retry_backoff` | autouse | Zeros all retry/backoff delays so tests don't sleep |
| `reset_rate_limiter` | autouse | Clears rate limiter state between tests |
| `run_message` | function | Full pipeline runner for functional tests |
| `live_config` | session | Real config with OpenRouter API key for live tests |
| `mock_noop_infra` | function | Stubs subprocess/tool infra in live tests so only LLM is real |
| `human_relay` | function | Agent-to-human bridge for interactive tests |


## Directory layout

```
tests/
├── conftest.py              # Global fixtures, markers, autouse helpers
├── _cli_plugin_helpers.py   # Shared parametrize cases for tool/connector CLI
├── _cli_user_helpers.py     # Shared user CLI helpers (make_user_config, etc.)
├── fixtures/config.toml     # Minimal valid config for tests needing a real file
│
├── test_*.py                # Unit tests (~60 files)
│
├── functional/              # Full pipeline (Docker + LLM)
│   ├── conftest.py          # run_message fixture, FunctionalResult, lang helpers
│   └── test_*.py            # F1-F16 test scenarios
│
├── live/                    # LLM compliance (host + API key)
│   ├── conftest.py          # live_config, mock_noop_infra
│   └── test_*.py            # Role, flow, e2e, practical tests
│
├── docker/                  # Sandbox isolation (Docker, no LLM)
│   ├── conftest.py          # kiso_dir fixture
│   └── test_*.py            # Permission and venv tests
│
├── interactive/             # Human-in-the-loop (manual only)
│   ├── conftest.py          # HumanRelay fixture
│   └── test_*.py            # CAPTCHA, OAuth, SSH deploy
│
└── bash/                    # Shell function tests (BATS)
    ├── helpers.bash          # Shared BATS helpers
    └── test_*.bats           # install.sh and host.sh functions
```
