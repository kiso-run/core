# Test Suite

## Test levels

Kiso has a complex runtime: an LLM orchestrator that classifies messages, builds plans,
executes shell commands inside Docker containers, reviews outputs with another LLM call,
manages a knowledge base, and talks back to users through connectors. The suite is split
into levels that form a pyramid — fast/cheap at the bottom, slow/expensive at the top.

```
                      ┌────────────────┐
                      │  Interactive   │  human at terminal
                    ┌─┤  (manual)      ├─┐
                    │ └────────────────┘ │
                  ┌─┴────────────────────┴─┐
                  │       Extended         │  multi-plan, nightly
                ┌─┤  (orchestration)       ├─┐
                │ └────────────────────────┘ │
              ┌─┴──────────────────────────┴─┐
              │        Functional            │  Docker + real LLMs
              │     (single-plan e2e)        │
            ┌─┴──────────────────────────────┴─┐
            │     Live  +  Plugins             │  real LLMs / real repos
            │  (LLM compliance + plugin tests) │
          ┌─┴──────────────────────────────────┴─┐
          │    Integration  +  Docker              │  mock LLM + container
          │  (HTTP API + sandbox security)         │
        ┌─┴────────────────────────────────────────┴─┐
        │           Unit  +  Bash                      │  host only, all mocked
        │      (logic, schemas, CLI, shell)             │
        └──────────────────────────────────────────────┘
```

Each level answers a different question:

| Level | Question | Requires | ~Time |
|-------|----------|----------|-------|
| **Unit** | Does each function do the right thing? | Host only | ~45s (4045 tests, xdist) |
| **Bash** | Do shell helpers parse/validate correctly? | bats | <5s |
| **Integration** | Does the HTTP API + worker pipeline work? | Host (mock LLM) | ~6s (25 tests, xdist) |
| **Docker** | Can the sandbox user escape its workspace? | Docker | <1s |
| **Plugin** | Do official plugins build and pass their tests? | Docker | ~35s |
| **Live** | Do real LLMs understand our prompts? | API key | ~15min |
| **Functional** | Does the full pipeline work end-to-end? | Docker + API key | ~10min |
| **Extended** | Do multi-plan orchestrations work? | Docker + API key | ~15min |
| **Interactive** | Do human-gated flows (CAPTCHA, OAuth) work? | Docker + human | manual |

### Performance baseline (M1267, 2026-04-08)

| Tier | Before (sequential) | After (xdist + tmpfs) | Speedup |
|------|---------------------|------------------------|---------|
| **Unit** | 137s (4045 tests) | 42s | **3.2x** |
| **Integration** | 10s (25 tests) | 6s | **1.8x** |

The default `./utils/run_tests.sh` invocation now uses `pytest-xdist`
(`-n auto`) to parallelize across CPU cores, and `--basetemp=/dev/shm/...`
on Linux for RAM-speed test storage. xdist works out-of-the-box because
each xdist worker is a separate subprocess so module-level state in
`kiso.main` (`_workers`, `_worker_phases`, `_rate_limiter`) is isolated
per worker. On non-Linux platforms the runner silently falls back to the
default basetemp.

These flags only apply to the **unit** and **integration** tiers (the
fast deterministic ones). Live, functional, docker, plugin, and
interactive tiers intentionally stay sequential because they hit real
LLMs, real services, or single Docker containers.

## Confidence tiers

Test level alone is not enough. Some suites are realistic but still weak if the
oracle is only "non-empty output" or "assistant said it worked". Treat tests by
confidence tier as well as by execution level:

| Tier | Meaning | Typical suites |
|------|---------|----------------|
| **Blocking semantic** | Deterministic or near-deterministic tests with concrete state/behavior assertions | Unit, Bash, Integration, Docker, most semantic Live tests |
| **Optional smoke** | Real network/LLM/service checks that validate reachability or broad workflow health but may depend on external drift | Some Live network tests, plugin fetch checks, third-party smoke tests |
| **Manual acceptance** | Human-gated or externally fragile workflows where operator judgment is part of the oracle | Interactive tests, CAPTCHA/OAuth/social signups |

When adding or editing tests:

- Prefer semantic assertions over prompt wording or exact prose.
- Prefer observable effects over generic success flags:
  plan/task shape, DB state, created files, published artifacts, exit status,
  persisted knowledge, reachable URLs, or concrete command output.
- Prefer structured runtime contracts over incidental text:
  `TaskContract`, `TaskResult`, `file_refs`, `artifact_refs`, dependency links,
  and classified failure modes are stronger oracles than prompt snippets.
- Use language-quality heuristics only when language selection is itself the
  product requirement.
- Avoid adding blocking tests whose only oracle is a substring in a prompt file
  or a success-like word in the assistant message.

### Preferred oracles after the structured-runtime redesign

When a feature touches planner, worker, replans, or memory, prefer asserting:

- normalized task contracts instead of the raw planner prose alone
- canonical task results instead of ad-hoc `output` strings alone
- file/artifact identity and dependency links instead of guessed filenames
- failure classes and retry hints instead of generic "replan happened"
- operational vs semantic memory slices instead of one merged context blob

If an old test only checks that some prompt section contains a phrase, that is
now usually the wrong level unless the phrase itself is the product contract.

For planner workspace-context tests, patch the current runtime seam
(`ExecutionState` / `_build_execution_state`) rather than superseded helpers
like `_list_session_files()`. The planner no longer assembles session files
through that older helper path.

### Public import surfaces

Complexity-reduction refactors are allowed to move internal code aggressively,
so tests should distinguish between intentional import boundaries and
implementation details.

Protected import surfaces:

- `kiso.worker` exposes only `run_worker`
- `kiso.main` keeps the runtime app/startup seam (`app`, `_init_app_state`)
- `kiso.brain` keeps the high-level orchestration surface:
  role runners, validators, message builders, classifier helpers, and the
  stable runtime exceptions/constants consumed by `kiso.main`, `kiso.worker`,
  and live/integration tests
- `kiso.store` keeps the storage facade functions/types used by runtime modules
  and API layers, but not its underscore helpers

Not protected as public API:

- underscore helpers in `kiso.brain`
- underscore helpers in `kiso.store`
- concrete modules under `kiso.worker.*`
- concrete modules under `kiso.store.*`
- route/helper internals in `kiso.main`

When a test is not specifically about a public boundary, import the concrete
implementation module instead of relying on package-level re-exports or a large
monolithic module shape.

### Prompt-test guardrails

Prompt-file assertions are allowed in blocking suites only for lightweight smoke
coverage:

- role file exists and loads
- modular prompt section loads
- coarse prompt budget checks
- stable, structural tokens that define a schema or hard contract

Prompt-file assertions are **not** the primary place to test planner/worker/
reviewer behavior. If the behavior is observable through plan validation,
message builders, worker execution, live role calls, or functional flows, write
the test there instead.

### Live and functional rerun limits

When `KISO_LLM_API_KEY` is unavailable locally, use `--collect-only` only as a
suite-wiring check:

- collection proves the tests still import, parse, and register correctly
- collection does **not** validate prompt quality, provider behavior, timing, or
  full end-to-end semantics
- classify missing-credential runs separately from true product regressions
- treat provider/network timeouts as external fragility unless the same behavior
  is reproducible in unit/integration coverage or in a credentialed rerun


## Running tests

```bash
# Interactive menu — pick which suites to run
./utils/run_tests.sh

# CI / scripting (non-interactive, combinable flags)
# Direct (by number or letter — same as menu choices)
./utils/run_tests.sh 4                        # run live tests
./utils/run_tests.sh 1,3                      # run unit + integration
./utils/run_tests.sh a                        # all automatic
./utils/run_tests.sh f                        # fast all (skip pipeline tests)
./utils/run_tests.sh s "tests/functional/test_core_flows.py::TestF23CrossSessionKnowledge"  # specific test (any type — autodetects flags)

# Auto (CI, named flags)
./utils/run_tests.sh --auto                   # all automatic
./utils/run_tests.sh --auto --unit            # only unit
./utils/run_tests.sh --auto --unit --live     # combinable
./utils/run_tests.sh --auto --no-live         # all automatic except live
./utils/run_tests.sh --auto --extended        # only extended (nightly)
./utils/run_tests.sh --auto --all             # everything including interactive + extended

# Quick unit-only during development
uv run pytest tests/ -q

# Single file
uv run pytest tests/test_brain.py -v
```

### Interactive menu

```
  Kiso Test Runner

  ── Fast (host only) ──────────────────────────
  1  Unit tests              ~3650 tests, ~90s
  2  Bash tests              89 tests, <5s
  3  Integration tests       9 tests, ~10s, mock LLM

  ── Real LLM (needs API key) ──────────────────
  4  Live tests              72 tests, ~15min
     LLM compliance — prompts, schemas, roles

  ── Docker container ──────────────────────────
  5  Docker tests            10 tests, <1s
  6  Plugin tests            ~700 tests, ~35s
     Clone + build + test each official plugin

  ── Full pipeline (Docker + API key) ─────────
  7  Functional tests        ~55 tests, ~10min
     Single-plan end-to-end: classify → plan → exec → msg
  8  Extended tests          ~15min, nightly
     Multi-plan orchestration (tool install → use → report)

  ── Special ──────────────────────────────────
  9  Interactive tests       requires human at terminal
  10 All automatic           1-8 (skip 9 interactive)
  s  Run specific test       path::Class::test or -k pattern
```

Option `s` auto-detects which flags and environment (host vs Docker) are needed
based on the test path prefix. Examples:
```bash
# Run a single live test
s → tests/live/test_roles.py::TestPlannerSystemPackageLive::test_python_lib_uses_uv_pip

# Run unit tests matching a keyword
s → tests/test_brain.py -k "pip_install"

# Run a specific functional test (auto-detects Docker)
s → tests/functional/test_core_flows.py::TestF18SimpleQA
```


## Unit tests (`tests/test_*.py`)

**~3650 tests, ~90 seconds on host.**

Everything external is mocked: LLM calls, database, filesystem, network. Fast,
deterministic, runnable anywhere without credentials or Docker.

Covers: schema validation, prompt construction, worker loop logic, CLI commands,
security (exec deny lists, secret sanitization), API endpoints.

Notable test files (M1023-M1034):
- `test_hooks.py` — Pre/post execution hook tests
- `test_consolidation.py` — Consolidation (knowledge quality review) tests
- `test_cli_config.py` — Config CLI command tests

### Conventions

- **HTTP mocking:** patch `httpx.request`, not `httpx.get`/`httpx.post`
- **Error output:** CLI errors go to stderr — assert on `capsys.readouterr().err`
- **Briefing mocks:** must include `"exclude_recipes": [], "relevant_entities": []`
- **Learn strings:** must be >= 15 chars


## Bash tests (`tests/bash/`)

**89 tests, <5 seconds, requires `bats`.**

Tests pure-bash helper functions in `install.sh` and `host.sh` — instance name
derivation, port allocation, name validation, `.env` safe reading, instance
registration, bash completion, host-level commands.


## Integration tests (`tests/integration/`)

**9 tests, ~10 seconds, host only (mock LLM).**

Tests the HTTP API and connector flow without the cost of real LLM + Docker.
LLM calls are mocked with role-appropriate responses.

Covers: session registration, message submission, webhook delivery, polling
fallback, install proposal + approval flow, multi-turn conversations, cancel.


## Docker tests (`tests/docker/`)

**10 tests, <1 second, runs inside Docker.**

Verifies sandbox isolation — the security boundary between kiso and exec tasks.
No LLM calls needed.


## Plugin tests (`cli/plugin_test_runner.py`)

**~600 tests across ~9 plugins, ~35 seconds.**

Clones each official plugin from the registry, installs deps, and runs its
internal test suite. Validates that plugins build and pass in a clean environment
with no secrets leaked from the parent process.


## Live tests (`tests/live/`)

**72 tests, ~15 minutes, needs API key.**

Real LLM API calls but everything else is mocked (subprocess, filesystem, tools).
Isolates the LLM-compliance question from infrastructure concerns.

Organized by scope: `test_roles.py` (each role in isolation), `test_flows.py`
(role chains), `test_e2e.py` (full planning loop), `test_practical.py`
(acceptance scenarios), `test_cli_live.py` (CLI with real network),
`test_plugins.py` (clone + test official plugins).

Use live tests for semantic LLM-compliance questions. Real-network tests with
only weak stdout-based oracles belong in optional smoke, not as the primary
coverage for a feature.


## Functional tests (`tests/functional/`)

**~55 tests, ~10 minutes, Docker + API key.**

Full pipeline end-to-end: real LLM, real subprocess execution, real tool
installation, real file I/O. The most expensive and most realistic level.

Each test sends a natural-language message through `_process_message()` and
asserts on the response and side effects. Grouped by capability: browser,
system, services, research, knowledge, core flows.

Prefer assertions on side effects and workflow structure:

- task types and plan shapes
- created/reused workspace files
- published artifacts and URLs
- persisted DB/session/project state
- concrete exec/search/tool outputs

Assistant wording checks should be secondary unless the user-facing wording is
the feature being tested.

**Extended tests** (`@pytest.mark.extended`) include multi-plan tests and
post-preset workflow tests (tools pre-installed via session fixture).
Excluded from option 7 and run separately via option 8 or `--extended`.

Post-preset workflows (`test_preset_workflows.py`): install browser/ocr/aider
once, then test real workflows without install flow fragility (F27-F30).

**Destructive tests** (`@pytest.mark.destructive`) create real accounts on
external services. Gated by `--destructive`, never run in CI.

Third-party service tests with externally controlled state should usually also
be `@pytest.mark.extended` and treated as smoke coverage unless they assert a
concrete remote effect that is stable enough to act as a semantic oracle.


## Interactive tests (`tests/interactive/`)

**Manual only, gated by `--interactive`.**

Flows requiring a real human: CAPTCHA solving, OAuth authorization, SSH key
deploy. Uses a `HumanRelay` fixture that pauses for operator action.

Interactive tests are manual acceptance checks, not blocking CI coverage. Keep
their scope narrow and do not rely on them as the only proof of a feature that
can be exercised semantically in unit/live/functional suites.


## Pytest markers

| Marker | Flag | What it gates |
|--------|------|---------------|
| `llm_live` | `--llm-live` | Real LLM API calls |
| `live_network` | `--live-network` | External services (GitHub, registries) |
| `functional` | `--functional` | Full pipeline in Docker |
| `extended` | `--extended` | Long-running multi-plan tests |
| `destructive` | `--destructive` | Irreversible side effects |
| `integration` | `--integration` | HTTP API integration tests |
| `interactive` | `--interactive` | Human at terminal |

Gating logic is in `tests/conftest.py:pytest_collection_modifyitems`.


## Directory layout

```
tests/
├── conftest.py              # Global fixtures, markers, autouse helpers
├── _cli_plugin_helpers.py   # Shared parametrize cases for tool/connector CLI
├── _cli_user_helpers.py     # Shared user CLI helpers
│
├── test_*.py                # Unit tests (~72 files)
│
├── integration/             # HTTP API + mock LLM
│   ├── conftest.py          # kiso_client, webhook_collector, mock_call_llm
│   └── test_*.py            # Connector protocol, multi-turn, cancel
│
├── functional/              # Full pipeline (Docker + LLM)
│   ├── conftest.py          # run_message, FunctionalResult, lang helpers
│   └── test_*.py            # F1-F23 test scenarios
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
    └── test_*.bats          # install.sh and host.sh functions
```
