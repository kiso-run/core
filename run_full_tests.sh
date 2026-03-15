#!/usr/bin/env bash
# Full test suite runner — runs everything automatically.
#
# Test levels:
#   --unit    Unit tests (host, no LLM, no network, mocked deps)
#   --func    Functional tests (Docker, real LLMs, real exec, full pipeline)
#   --docker  Docker/sandbox tests (Docker, no LLM, tests isolation/permissions)
#   --live    Live LLM tests (host, real LLMs, tests role quality, no exec)
#   --integration  Integration tests (host, mock LLM, connector protocol)
#   --plugins      Plugin tests (host, all installed tools/connectors)
#
# Auto-managed:
#   - API keys: loaded from ~/.kiso/instances/kiso/.env
#   - Docker: functional + docker tests run via docker compose (root in container)
#
# Only skips if truly unavailable:
#   - Docker not installed → skips functional + docker tests
#   - OPENROUTER_API_KEY missing (and no .env) → skips live + functional tests
#
# Usage:
#   ./run_full_tests.sh           # run everything
#   ./run_full_tests.sh --unit    # only unit tests
#   ./run_full_tests.sh --func    # only functional tests (Docker)
#   ./run_full_tests.sh --docker  # only docker/sandbox tests
#   ./run_full_tests.sh --live    # only live LLM tests

set -euo pipefail
cd "$(dirname "$0")"

# Load API keys from kiso .env (same file the server uses)
_ENV_FILE="${KISO_ENV_FILE:-$HOME/.kiso/instances/kiso/.env}"
if [[ -f "$_ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        # Only export if not already set in the environment
        if [[ -z "${!key:-}" ]]; then
            export "$key"="$value"
        fi
    done < "$_ENV_FILE"
    # Map KISO_LLM_API_KEY → OPENROUTER_API_KEY if needed
    if [[ -z "${OPENROUTER_API_KEY:-}" && -n "${KISO_LLM_API_KEY:-}" ]]; then
        export OPENROUTER_API_KEY="$KISO_LLM_API_KEY"
    fi
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

run_suite() {
    local name="$1"; shift
    echo -e "\n${YELLOW}━━━ $name ━━━${NC}"
    local rc=0
    "$@" || rc=$?
    if [[ "$rc" -eq 0 || "$rc" -eq 5 ]]; then
        # 0 = passed, 5 = no tests collected (all skipped/deselected)
        echo -e "${GREEN}✓ $name: PASSED${NC}"
    else
        echo -e "${RED}✗ $name: FAILED${NC}"
        FAILED=1
    fi
}

FAILED=0
MODE="${1:-all}"

if [[ "$MODE" == "all" || "$MODE" == "--unit" ]]; then
    run_suite "Unit tests" uv run pytest tests/ -q \
        --ignore=tests/live --ignore=tests/docker --ignore=tests/functional
fi

if [[ "$MODE" == "all" || "$MODE" == "--func" ]]; then
    # Requires: Docker + OPENROUTER_API_KEY
    # Functional tests run inside Docker (root, sandbox, deps.sh, tool install)
    if ! docker info > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠ Skipping functional tests — Docker not available${NC}"
    elif [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
        echo -e "${YELLOW}⚠ Skipping functional tests — OPENROUTER_API_KEY not set${NC}"
    else
        docker compose -f docker-compose.test.yml build test-functional
        run_suite "Functional tests" \
            docker compose -f docker-compose.test.yml run --rm \
            -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
            test-functional
    fi
fi

if [[ "$MODE" == "all" || "$MODE" == "--docker" ]]; then
    # Requires: Docker — tests run as root inside the container
    if docker info > /dev/null 2>&1; then
        docker compose -f docker-compose.test.yml build test-docker
        run_suite "Docker/sandbox tests" \
            docker compose -f docker-compose.test.yml run --rm test-docker
    else
        echo -e "${YELLOW}⚠ Skipping docker/sandbox tests — Docker not available${NC}"
    fi
fi

if [[ "$MODE" == "all" || "$MODE" == "--live" ]]; then
    # Requires: OPENROUTER_API_KEY set
    if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
        run_suite "Live tests (LLM + network)" uv run pytest tests/live/ -v --live-network --llm-live
    else
        echo -e "${YELLOW}⚠ Skipping live tests — OPENROUTER_API_KEY not set${NC}"
        echo "  export OPENROUTER_API_KEY=sk-or-..."
    fi
fi

if [[ "$MODE" == "all" || "$MODE" == "--integration" ]]; then
    run_suite "Integration tests" uv run pytest tests/integration/ -v --integration
fi

if [[ "$MODE" == "--plugins" ]]; then
    run_suite "Plugin unit tests" uv run pytest tests/integration/ -v --integration
    echo -e "${YELLOW}Note: per-plugin tests require 'kiso tool test <name>'${NC}"
fi

echo ""
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All executed suites passed.${NC}"
else
    echo -e "${RED}Some suites failed — check output above.${NC}"
    exit 1
fi
