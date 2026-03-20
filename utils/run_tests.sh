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
#   ./run_tests.sh --auto           # all automatic suites (no interactive/extended)
#   ./run_tests.sh --auto --unit    # only unit
#   ./run_tests.sh --auto --bash    # only bash/BATS
#   ./run_tests.sh --auto --unit --live   # unit + live (combinable)
#   ./run_tests.sh --auto --all     # everything including interactive + extended
#   ./run_tests.sh --auto --no-live # all automatic except live tests
#   ./run_tests.sh --auto --extended      # only extended (nightly)

set -euo pipefail
cd "$(dirname "$0")/.."

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
        run_suite "Docker tests" \
            docker compose -f docker-compose.test.yml run --build --rm test-docker
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
    # Exclude extended tests — those run separately via run_extended()
    run_suite "Functional tests" \
        docker compose -f docker-compose.test.yml run --build --rm \
        -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
        test-functional \
        uv run pytest tests/functional/ -v --functional -m "functional and not extended"
}

run_extended() {
    if [[ "$HAS_DOCKER" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping extended tests — Docker not available${NC}"
        return
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping extended tests — OPENROUTER_API_KEY not set${NC}"
        return
    fi
    run_suite "Extended tests" \
        docker compose -f docker-compose.test.yml run --build --rm \
        -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
        test-functional \
        uv run pytest tests/functional/ -v --functional --extended -m extended
}

run_plugins() {
    local filter="${1:-}"
    if [[ "$HAS_DOCKER" == true ]]; then
        # Docker: same environment as production, with dep-cache volume
        local cmd="uv run python -m cli.plugin_test_runner ${filter}"
        run_suite "Plugin tests${filter:+ ($filter)}" \
            docker compose -f docker-compose.test.yml run --build --rm \
            test-plugins $cmd
    else
        run_suite "Plugin tests${filter:+ ($filter)}" \
            uv run python -m cli.plugin_test_runner "$filter"
    fi
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
    run_suite "Interactive tests" \
        docker compose -f docker-compose.test.yml run --build --rm \
        -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
        test-functional \
        uv run pytest tests/interactive/ -v --interactive --functional
}

# ---------------------------------------------------------------------------
# Auto mode (CI / scripting)
# ---------------------------------------------------------------------------
run_auto() {
    shift  # remove --auto

    # Collect --no-X skip flags
    local skip_unit=false skip_bash=false skip_integration=false
    local skip_live=false skip_docker=false skip_func=false skip_plugins=false
    local has_suite=false
    local flags=()
    for arg in "$@"; do
        case "$arg" in
            --no-unit)        skip_unit=true ;;
            --no-bash)        skip_bash=true ;;
            --no-integration) skip_integration=true ;;
            --no-live)        skip_live=true ;;
            --no-docker)      skip_docker=true ;;
            --no-func|--no-functional) skip_func=true ;;
            --no-plugins)     skip_plugins=true ;;
            *)                flags+=("$arg"); has_suite=true ;;
        esac
    done

    if [[ "$has_suite" == false ]]; then
        # No suite flags → run all automatic (no interactive/extended)
        [[ "$skip_unit" == false ]]        && run_unit
        [[ "$skip_bash" == false ]]        && run_bash
        [[ "$skip_integration" == false ]] && run_integration
        [[ "$skip_live" == false ]]        && run_live
        [[ "$skip_docker" == false ]]      && run_docker
        [[ "$skip_func" == false ]]        && run_functional
        [[ "$skip_plugins" == false ]]     && run_plugins ""
        return
    fi

    for flag in "${flags[@]}"; do
        case "$flag" in
            --unit)         run_unit ;;
            --bash)         run_bash ;;
            --integration)  run_integration ;;
            --live)         run_live ;;
            --docker)       run_docker ;;
            --func|--functional) run_functional ;;
            --extended)     run_extended ;;
            --interactive)  run_interactive ;;
            --plugins)      run_plugins "" ;;
            --plugins=*)    run_plugins "${flag#*=}" ;;
            --all)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_functional
                run_extended
                run_plugins ""
                run_interactive
                ;;
            *)
                echo -e "${RED}Unknown flag: $flag${NC}"
                echo "Available: --unit --bash --integration --live --docker --func/--functional --extended --interactive --plugins[=filter] --all"
                echo "Skip flags: --no-unit --no-bash --no-integration --no-live --no-docker --no-func --no-plugins"
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
    local miss_docker="" miss_api="" miss_bats=""
    if [[ "$HAS_DOCKER" != true ]]; then
        miss_docker=" ${RED}✗ no Docker${NC}"
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        miss_api=" ${RED}✗ no API key${NC}"
    fi
    if [[ "$HAS_BATS" != true ]]; then
        miss_bats=" ${RED}✗ no bats${NC}"
    fi

    echo ""
    echo -e "  ${BOLD}Kiso Test Runner${NC}"
    echo ""
    echo -e "  ${DIM}── Fast (host only) ──────────────────────────${NC}"
    echo -e "  ${CYAN}1${NC}  Unit tests              ${DIM}~3650 tests, ~90s${NC}"
    echo -e "  ${CYAN}2${NC}  Bash tests              ${DIM}89 tests, <5s${NC}${miss_bats}"
    echo -e "  ${CYAN}3${NC}  Integration tests       ${DIM}9 tests, ~10s, mock LLM${NC}"
    echo ""
    echo -e "  ${DIM}── Real LLM (needs API key) ──────────────────${NC}"
    echo -e "  ${CYAN}4${NC}  Live tests              ${DIM}72 tests, ~15min${NC}${miss_api}"
    echo -e "     ${DIM}LLM compliance — prompts, schemas, roles${NC}"
    echo ""
    echo -e "  ${DIM}── Docker container ──────────────────────────${NC}"
    echo -e "  ${CYAN}5${NC}  Docker tests            ${DIM}10 tests, <1s${NC}${miss_docker}"
    echo -e "  ${CYAN}6${NC}  Plugin tests            ${DIM}~700 tests, ~35s${NC}"
    echo -e "     ${DIM}Clone + build + test each official plugin${NC}"
    echo ""
    echo -e "  ${DIM}── Full pipeline (Docker + API key) ─────────${NC}"
    echo -e "  ${CYAN}7${NC}  Functional tests        ${DIM}~55 tests, ~10min${NC}${miss_docker}${miss_api}"
    echo -e "     ${DIM}Single-plan end-to-end: classify → plan → exec → msg${NC}"
    echo -e "  ${CYAN}8${NC}  Extended tests          ${DIM}~15min, nightly${NC}${miss_docker}${miss_api}"
    echo -e "     ${DIM}Multi-plan orchestration (tool install → use → report)${NC}"
    echo ""
    echo -e "  ${DIM}── Special ──────────────────────────────────${NC}"
    echo -e "  ${CYAN}9${NC}  Interactive tests       ${DIM}requires human at terminal${NC}${miss_docker}${miss_api}"
    echo -e "  ${CYAN}10${NC} All automatic           ${DIM}1-7 (skip 8, 9)${NC}"
    echo ""

    local choice
    read -rp "  Choose [1-10, comma-separated, or 'q' to quit]: " choice

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
            6)
                echo ""
                echo -e "  ${BOLD}What to test?${NC}"
                echo -e "  a) All tools"
                echo -e "  b) All connectors"
                echo -e "  c) All plugins (tools + connectors)"
                echo -e "  d) Specific (enter names)"
                echo ""
                local pchoice
                read -rp "  Choice [a/b/c/d]: " pchoice
                case "$pchoice" in
                    a) run_plugins "tools" ;;
                    b) run_plugins "connectors" ;;
                    c) run_plugins "" ;;
                    d)
                        local pnames
                        read -rp "  Names (comma-separated): " pnames
                        run_plugins "$pnames"
                        ;;
                    *) echo -e "${RED}Invalid choice${NC}" ;;
                esac
                ;;
            7) run_functional ;;
            8) run_extended ;;
            9) run_interactive ;;
            10)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_plugins ""
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
