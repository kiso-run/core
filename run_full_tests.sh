#!/usr/bin/env bash
# Full test suite runner — includes tests that need infrastructure.
#
# Prerequisites:
#   1. kiso server running locally (for functional tests)
#   2. OPENROUTER_API_KEY set (for live LLM tests)
#   3. Docker running + kiso image built (for sandbox tests)
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
    uv run pytest "$@" || rc=$?
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
    run_suite "Unit tests" tests/ -q \
        --ignore=tests/live --ignore=tests/docker --ignore=tests/functional
fi

if [[ "$MODE" == "all" || "$MODE" == "--func" ]]; then
    # Requires: kiso server running on localhost:8333
    if curl -sf http://localhost:8333/health > /dev/null 2>&1; then
        run_suite "Functional tests" tests/functional/ -v
    else
        echo -e "${YELLOW}⚠ Skipping functional tests — kiso server not running on :8333${NC}"
        echo "  Start it with: uv run python -m kiso.main"
    fi
fi

if [[ "$MODE" == "all" || "$MODE" == "--docker" ]]; then
    # Requires: Docker running + kiso image
    if docker info > /dev/null 2>&1; then
        run_suite "Docker tests" tests/docker/ -v
    else
        echo -e "${YELLOW}⚠ Skipping docker tests — Docker not available${NC}"
    fi
fi

if [[ "$MODE" == "all" || "$MODE" == "--live" ]]; then
    # Requires: OPENROUTER_API_KEY set
    if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
        run_suite "Live tests (LLM + network)" tests/live/ -v --live-network --llm-live
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
