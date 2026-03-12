#!/usr/bin/env bash
# Full test suite runner — runs everything automatically.
#
# Auto-managed:
#   - kiso server: started/stopped automatically for functional tests
#   - API keys: loaded from ~/.kiso/instances/kiso/.env
#   - Docker sandbox tests: run via docker compose (as root in container)
#
# Only skips if truly unavailable:
#   - Docker not installed → skips sandbox tests
#   - OPENROUTER_API_KEY missing (and no .env) → skips live tests
#
# Usage:
#   ./run_full_tests.sh           # run everything
#   ./run_full_tests.sh --live    # only live network tests
#   ./run_full_tests.sh --func    # only functional tests
#   ./run_full_tests.sh --docker  # only docker tests

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
    _STARTED_SERVER=0
    if ! curl -sf http://localhost:8333/health > /dev/null 2>&1; then
        echo -e "${YELLOW}Starting kiso server for functional tests…${NC}"
        uv run python -m kiso.main &
        _SERVER_PID=$!
        _STARTED_SERVER=1
        # Wait for server to be healthy (up to 15s)
        for i in $(seq 1 30); do
            if curl -sf http://localhost:8333/health > /dev/null 2>&1; then break; fi
            sleep 0.5
        done
        if ! curl -sf http://localhost:8333/health > /dev/null 2>&1; then
            echo -e "${RED}✗ kiso server failed to start${NC}"
            kill "$_SERVER_PID" 2>/dev/null || true
            FAILED=1
            _STARTED_SERVER=0
        fi
    fi
    if curl -sf http://localhost:8333/health > /dev/null 2>&1; then
        run_suite "Functional tests" uv run pytest tests/functional/ -v
    fi
    if [[ "$_STARTED_SERVER" -eq 1 ]]; then
        kill "$_SERVER_PID" 2>/dev/null || true
        wait "$_SERVER_PID" 2>/dev/null || true
    fi
fi

if [[ "$MODE" == "all" || "$MODE" == "--docker" ]]; then
    # Requires: Docker — tests run as root inside the container
    if docker info > /dev/null 2>&1; then
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

echo ""
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All executed suites passed.${NC}"
else
    echo -e "${RED}Some suites failed — check output above.${NC}"
    exit 1
fi
