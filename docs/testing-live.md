# Live LLM Integration Tests

Tests that call real LLMs via OpenRouter to verify structured output, semantic correctness, and end-to-end flows.

## Prerequisites

- `KISO_OPENROUTER_API_KEY` environment variable set with a valid API key (L1-L4)
- Internet access to `openrouter.ai` (L1-L4) and `api.github.com` (L5)
- `git` on PATH (L5 install test)

## Running

```bash
# Run all live tests (LLM + network)
KISO_OPENROUTER_API_KEY=sk-... uv run pytest tests/live/ --llm-live --live-network -v

# Run a specific level
uv run pytest tests/live/test_roles.py --llm-live -v       # L1 only
uv run pytest tests/live/test_flows.py --llm-live -v       # L2 only
uv run pytest tests/live/test_e2e.py --llm-live -v         # L3 only
uv run pytest tests/live/test_practical.py --llm-live -v   # L4 only
uv run pytest tests/live/test_cli_live.py --live-network -v # L5 only (no API key needed)

# Regular tests are unaffected (live tests skipped automatically)
uv run pytest tests/ -q

# Without flags: all live tests skipped with clear message
uv run pytest tests/live/ -v
```

## Flags

| Flag | Marker | Env var required | Purpose |
|---|---|---|---|
| `--llm-live` | `llm_live` | `KISO_OPENROUTER_API_KEY` | Enable tests that call real LLMs |
| `--live-network` | `live_network` | — | Enable tests that call external services (GitHub, git) |

## Cost

A full run of all live tests makes roughly 30-50 LLM calls using the models in `MODEL_DEFAULTS` (kimi-k2.5 for planner/reviewer/curator, deepseek-v3.2 for worker/summarizer/paraphraser). Estimated cost: **~$0.50-1.50 per full run** via OpenRouter.

## Test Levels

| Level | File | Tests | Scope | LLM calls/test | Flag |
|---|---|---|---|---|---|
| L1 | `test_roles.py` | 8 | Single brain function called in isolation | 1 | `--llm-live` |
| L2 | `test_flows.py` | 4 | 2-3 connected components | 2-3 | `--llm-live` |
| L3 | `test_e2e.py` | 4 | Full pipeline through `_execute_plan` | 3-5 | `--llm-live` |
| L4 | `test_practical.py` | 7 | Realistic user scenarios (exec chaining, full `_process_message`, multi-turn, replan, knowledge pipeline, skill execution) | 3-8 | `--llm-live` |
| L5 | `test_cli_live.py` | 5 | CLI lifecycle (skill/connector search, install/remove) | 0 | `--live-network` |

## Design Principles

- **Two-layer gating**: LLM tests require both `--llm-live` flag AND `KISO_OPENROUTER_API_KEY` env var. Network tests require `--live-network`. Missing either skips with a clear reason.
- **Structural + loose semantic assertions**: Validate JSON structure (required fields, validation passes) and loose semantics (goal mentions topic, answer present). Never exact text matching.
- **Timeouts**: Every LLM call wrapped in `asyncio.wait_for(..., timeout=60-120)` to prevent hangs. L4 tests use 120s since they involve multi-LLM-call scenarios.
- **Infrastructure isolation**: E2e and practical tests mock filesystem/security/webhook infrastructure (`mock_noop_infra` fixture) while letting real LLM calls flow through.
- **Deterministic failure**: The replan test uses a manually-built failing plan (`ls /absolutely_nonexistent_dir_xyz`) rather than relying on the LLM to produce one.
- **Temporary directories**: CLI install tests use `tmp_path` for `SKILLS_DIR` to avoid polluting `~/.kiso/skills/`.

## Troubleshooting

### All tests skipped
- Without `--llm-live`: Expected. Pass the flag to enable LLM tests.
- Without `--live-network`: Expected. Pass the flag to enable network tests.
- With `--llm-live` but skipped: Check that `KISO_OPENROUTER_API_KEY` is set in the environment.

### Timeouts
- Default timeout is 60-120s per test. OpenRouter can be slow under load.
- If tests frequently timeout, check OpenRouter status or increase `TIMEOUT` constants in test files.

### Rate limiting
- OpenRouter may rate-limit under heavy use. Space out test runs or use a higher-tier API key.
- GitHub API has unauthenticated rate limits (60 requests/hour). L5 tests make very few calls.

### Flaky assertions
- Semantic assertions are intentionally loose (e.g., `"everest" in output.lower()`).
- If a test fails on assertion, check the actual LLM output — the model may have phrased things differently.
- Never add exact text matching; adjust assertions to be more permissive if needed.

### L5 install test skipped
- Requires `git` on PATH. Install git or skip this test.
