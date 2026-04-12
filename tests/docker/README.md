# Docker Integration Tests

Tests that require root privileges or Docker-specific features (sandbox users,
wrapper venv binary detection, etc.).

## Running

```bash
# Build and run docker tests
docker compose -f docker-compose.test.yml --profile docker run --rm test-docker

# Run a specific test file
docker compose -f docker-compose.test.yml --profile docker run --rm test-docker \
    uv run pytest tests/docker/test_sandbox.py -v

# Run unit tests (excludes docker and live tests)
docker compose -f docker-compose.test.yml --profile unit run --rm test-unit

# Run live LLM tests (requires .env with API keys)
docker compose -f docker-compose.test.yml --profile live run --rm test-live
```

## Test files

- `test_sandbox.py` — Per-session exec sandbox isolation (user creation, workspace permissions)
- `test_wrapper_venv.py` — Wrapper `.venv/bin/` binary detection via `check_deps` and `build_wrapper_env`

## Prerequisites

- Docker and Docker Compose
- Tests run as root inside the container (the Dockerfile.test image runs as root by default)
