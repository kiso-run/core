# Testing

## Stack

| Tool | Why |
|------|-----|
| pytest | Standard Python test runner, rich plugin ecosystem |
| pytest-asyncio | Async test support for FastAPI lifespan and async fixtures |
| httpx | Async HTTP client with ASGI transport — test endpoints without a running server |
| pytest-cov | Coverage reporting with fail-under gate |

## Directory Structure

```
tests/
├── conftest.py          # shared fixtures (test config, async client)
├── fixtures/            # static test data (sample configs, etc.)
├── test_config.py       # config loading and validation
├── test_health.py       # GET /health endpoint
└── ...                  # test_{module}.py per source module
```

## Running Tests

All development happens inside the dev container. Never run tests on the host.

```bash
docker compose up -d                                         # start dev container
docker compose exec dev uv sync --group dev                  # install deps (first time / after changes)
docker compose exec dev uv run pytest                        # all tests
docker compose exec dev uv run pytest --cov=kiso -q          # with coverage
docker compose exec dev uv run pytest tests/test_config.py -v  # single file
```

Or open a shell inside the container and run directly:

```bash
docker compose exec dev bash
uv run pytest --cov=kiso --cov-fail-under=80 -q
```

## Test Categories

- **Unit** — config parsing, validation, pure functions. No I/O, no server.
- **Integration** — endpoints via httpx `ASGITransport`. Exercises the full FastAPI app without a real server process.
- **LLM** — always mocked. Never make real LLM calls in tests or CI. Mock at the `httpx` / `call_llm` boundary.

## Fixtures

Defined in `tests/conftest.py`:

- `test_config_path` — writes a valid `config.toml` to `tmp_path`, returns the `Path`
- `test_config` — calls `load_config(test_config_path)`
- `client` — async `httpx.AsyncClient` using `ASGITransport` with the app, config injected via `app.state`

All fixtures use `tmp_path` — tests never touch `~/.kiso/`.

## Conventions

- One test file per source module: `test_{module}.py`
- Test functions: `test_{behavior}` (e.g. `test_missing_tokens`)
- Error tests verify the message, not just that `SystemExit` was raised (use `capsys`)
- Config fixtures write to `tmp_path` — never read or mutate real `~/.kiso/`
- No sleeps, no network calls, no flaky tests

## CI

Single command, fail on any test failure or coverage below threshold:

```bash
docker compose exec dev uv run pytest --cov=kiso --cov-fail-under=80 -q
```
