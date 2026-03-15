#!/usr/bin/env bash
# Kiso Test Runner
#
# Interactive by default — shows a menu to pick which suites to run.
# Use --auto for CI/scripting (non-interactive, combinable flags).
#
# Interactive:
#   ./run_tests.sh                  # menu
#
# Auto (CI):
#   ./run_tests.sh --auto           # all automatic suites (no interactive)
#   ./run_tests.sh --auto --unit    # only unit
#   ./run_tests.sh --auto --bash    # only bash/BATS
#   ./run_tests.sh --auto --unit --live   # unit + live (combinable)
#   ./run_tests.sh --auto --all     # everything including interactive
#   ./run_tests.sh --auto --interactive   # only interactive

set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Load API keys
# ---------------------------------------------------------------------------
_ENV_FILE="${KISO_ENV_FILE:-$HOME/.kiso/instances/kiso/.env}"
if [[ -f "$_ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        if [[ -z "${!key:-}" ]]; then
            export "$key"="$value"
        fi
    done < "$_ENV_FILE"
    if [[ -z "${OPENROUTER_API_KEY:-}" && -n "${KISO_LLM_API_KEY:-}" ]]; then
        export OPENROUTER_API_KEY="$KISO_LLM_API_KEY"
    fi
fi

# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------
HAS_DOCKER=false
if docker info > /dev/null 2>&1; then
    HAS_DOCKER=true
fi

HAS_API_KEY=false
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    HAS_API_KEY=true
fi

HAS_BATS=false
if command -v bats > /dev/null 2>&1; then
    HAS_BATS=true
fi

# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------
FAILED=0

run_suite() {
    local name="$1"; shift
    echo -e "\n${YELLOW}━━━ $name ━━━${NC}"
    local rc=0
    "$@" || rc=$?
    if [[ "$rc" -eq 0 || "$rc" -eq 5 ]]; then
        echo -e "${GREEN}✓ $name: PASSED${NC}"
    else
        echo -e "${RED}✗ $name: FAILED${NC}"
        FAILED=1
    fi
}

# ---------------------------------------------------------------------------
# Suite definitions
# ---------------------------------------------------------------------------
run_unit() {
    run_suite "Unit tests" uv run pytest tests/ -q \
        --ignore=tests/live --ignore=tests/docker \
        --ignore=tests/functional --ignore=tests/integration \
        --ignore=tests/interactive
}

run_bash() {
    if [[ "$HAS_BATS" == true ]]; then
        run_suite "Bash tests" bats tests/bash/
    else
        echo -e "${YELLOW}⚠ Skipping bash tests — bats not installed (npm install -g bats)${NC}"
    fi
}

run_integration() {
    run_suite "Integration tests" uv run pytest tests/integration/ -v --integration
}

run_live() {
    if [[ "$HAS_API_KEY" == true ]]; then
        run_suite "Live tests" uv run pytest tests/live/ -v --live-network --llm-live
    else
        echo -e "${YELLOW}⚠ Skipping live tests — OPENROUTER_API_KEY not set${NC}"
    fi
}

run_docker() {
    if [[ "$HAS_DOCKER" == true ]]; then
        docker compose -f docker-compose.test.yml build test-docker
        run_suite "Docker tests" \
            docker compose -f docker-compose.test.yml run --rm test-docker
    else
        echo -e "${YELLOW}⚠ Skipping docker tests — Docker not available${NC}"
    fi
}

run_functional() {
    if [[ "$HAS_DOCKER" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping functional tests — Docker not available${NC}"
        return
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping functional tests — OPENROUTER_API_KEY not set${NC}"
        return
    fi
    docker compose -f docker-compose.test.yml build test-functional
    run_suite "Functional tests" \
        docker compose -f docker-compose.test.yml run --rm \
        -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
        test-functional
}

run_interactive() {
    if [[ "$HAS_DOCKER" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping interactive tests — Docker not available${NC}"
        return
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping interactive tests — OPENROUTER_API_KEY not set${NC}"
        return
    fi
    docker compose -f docker-compose.test.yml build test-functional
    run_suite "Interactive tests" \
        docker compose -f docker-compose.test.yml run --rm \
        -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
        test-functional \
        uv run pytest tests/interactive/ -v --interactive --functional
}

# ---------------------------------------------------------------------------
# Auto mode (CI / scripting)
# ---------------------------------------------------------------------------
run_auto() {
    shift  # remove --auto

    if [[ $# -eq 0 ]]; then
        # No suite flags → run all automatic (no interactive)
        run_unit
        run_bash
        run_integration
        run_live
        run_docker
        run_functional
        return
    fi

    for flag in "$@"; do
        case "$flag" in
            --unit)         run_unit ;;
            --bash)         run_bash ;;
            --integration)  run_integration ;;
            --live)         run_live ;;
            --docker)       run_docker ;;
            --func)         run_functional ;;
            --interactive)  run_interactive ;;
            --all)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_functional
                run_interactive
                ;;
            *)
                echo -e "${RED}Unknown flag: $flag${NC}"
                echo "Available: --unit --bash --integration --live --docker --func --interactive --all"
                exit 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Interactive mode (default)
# ---------------------------------------------------------------------------
run_interactive_menu() {
    # Availability tags
    local docker_tag=""
    local api_tag=""
    local bats_tag=""
    if [[ "$HAS_DOCKER" != true ]]; then
        docker_tag="${DIM} (Docker not available)${NC}"
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        api_tag="${DIM} (API key not set)${NC}"
    fi
    if [[ "$HAS_BATS" != true ]]; then
        bats_tag="${DIM} (bats not installed)${NC}"
    fi

    echo ""
    echo -e "  ${BOLD}Kiso Test Runner${NC}"
    echo ""
    echo -e "  ${CYAN}1${NC}  Unit tests           ${DIM}~90s, host${NC}"
    echo -e "  ${CYAN}2${NC}  Bash tests            ${DIM}<5s, host, bats${NC}${bats_tag}"
    echo -e "  ${CYAN}3${NC}  Integration tests     ${DIM}~7s, host, mock LLM${NC}"
    echo -e "  ${CYAN}4${NC}  Live tests            ${DIM}~8min, API key${NC}${api_tag}"
    echo -e "  ${CYAN}5${NC}  Docker tests          ${DIM}<1s, Docker${NC}${docker_tag}"
    echo -e "  ${CYAN}6${NC}  Functional tests      ${DIM}~10min, Docker + API key${NC}${docker_tag}${api_tag}"
    echo -e "  ${CYAN}7${NC}  Interactive tests      ${DIM}Docker + human${NC}${docker_tag}${api_tag}"
    echo -e "  ${CYAN}8${NC}  All automatic          ${DIM}1-6, skip interactive${NC}"
    echo ""

    local choice
    read -rp "  Choose [1-8, comma-separated, or 'q' to quit]: " choice

    if [[ "$choice" == "q" || "$choice" == "Q" || -z "$choice" ]]; then
        echo "Aborted."
        exit 0
    fi

    # Parse comma-separated choices
    IFS=',' read -ra selections <<< "$choice"

    for sel in "${selections[@]}"; do
        sel="${sel// /}"  # trim spaces
        case "$sel" in
            1) run_unit ;;
            2) run_bash ;;
            3) run_integration ;;
            4) run_live ;;
            5) run_docker ;;
            6) run_functional ;;
            7) run_interactive ;;
            8)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_functional
                ;;
            *)
                echo -e "${RED}Invalid choice: $sel${NC}"
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--auto" ]]; then
    run_auto "$@"
else
    run_interactive_menu
fi

echo ""
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All executed suites passed.${NC}"
else
    echo -e "${RED}Some suites failed — check output above.${NC}"
    exit 1
fi
