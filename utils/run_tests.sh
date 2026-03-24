#!/usr/bin/env bash
# Kiso Test Runner
#
# Three modes:
#
# 1. Interactive (default):
#   ./run_tests.sh                  # shows menu
#
# 2. Direct (by number, same as menu choices):
#   ./run_tests.sh 4                # run live tests
#   ./run_tests.sh 1,3              # run unit + integration
#   ./run_tests.sh f                 # fast all (skip pipeline tests)
#   ./run_tests.sh a                 # all automatic
#   ./run_tests.sh s "tests/live/test_roles.py::TestFoo"  # specific test
#
# 3. Auto (CI, named flags):
#   ./run_tests.sh --auto           # all automatic suites
#   ./run_tests.sh --auto --unit    # only unit
#   ./run_tests.sh --auto --unit --live   # combinable
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
# Failure summary capture
# ---------------------------------------------------------------------------
_CAPTURE_DIR="$(mktemp -d)"
_CAPTURE_LOG="$_CAPTURE_DIR/output.log"
trap 'rm -rf "$_CAPTURE_DIR"' EXIT

# Strip ANSI escape codes from a string
_strip_ansi() {
    sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\r//g'
}

# Extract a compact failure summary from captured pytest output.
# Parses the FAILURES section, extracts test name + assertion + captured log.
_extract_failure_summary() {
    local logfile="$1"
    [[ ! -s "$logfile" ]] && return

    local clean
    clean="$(_strip_ansi < "$logfile")"

    # Extract everything between "= FAILURES =" and "= short test summary"
    local failures
    failures="$(echo "$clean" | sed -n '/^=* FAILURES =*/,/^=* short test summary/p' 2>/dev/null)" || true
    [[ -z "$failures" ]] && return

    echo ""
    echo -e "${RED}${BOLD}━━━ FAILURE SUMMARY (paste into LLM) ━━━${NC}"
    echo ""

    local current_test=""
    local in_captured_log=false
    local in_captured_stderr=false
    local log_lines=()
    local error_lines=()

    while IFS= read -r line; do
        # Test name header: ___ TestClass.test_method ___
        if [[ "$line" =~ ^___+\ (.+)\ ___+$ ]]; then
            # Flush previous test
            if [[ -n "$current_test" ]]; then
                _flush_test_block "$current_test" error_lines log_lines
            fi
            current_test="${BASH_REMATCH[1]}"
            error_lines=()
            log_lines=()
            in_captured_log=false
            in_captured_stderr=false
            continue
        fi

        # Captured log/stderr sections
        if [[ "$line" =~ ^-+\ Captured\ (log|stderr)\ call\ -+$ ]]; then
            in_captured_log=true
            in_captured_stderr=true
            continue
        fi
        # End of captured section (next dashed header or blank)
        if [[ "$in_captured_log" == true ]] && [[ "$line" =~ ^-+\ Captured || "$line" =~ ^=+\ short ]]; then
            in_captured_log=false
            in_captured_stderr=false
            continue
        fi

        # Collect captured log lines (kiso.* INFO/WARNING lines)
        if [[ "$in_captured_log" == true ]]; then
            if [[ "$line" =~ kiso\. || "$line" =~ "HTTP Request" ]]; then
                log_lines+=("$line")
            fi
            continue
        fi

        # Collect error lines (E   ... assertion errors, TimeoutError)
        if [[ "$line" =~ ^E\ + ]]; then
            error_lines+=("$line")
            continue
        fi
        # Also catch the final exception line
        if [[ "$line" =~ TimeoutError || "$line" =~ AssertionError || "$line" =~ "assert " ]]; then
            error_lines+=("$line")
        fi
    done <<< "$failures"

    # Flush last test
    if [[ -n "$current_test" ]]; then
        _flush_test_block "$current_test" error_lines log_lines
    fi

    echo -e "${RED}${BOLD}━━━ END FAILURE SUMMARY ━━━${NC}"
    echo ""
}

# Extract failed test node IDs from pytest "short test summary" and print
# a ready-to-paste rerun command.
_extract_rerun_snippet() {
    local logfile="$1"
    [[ ! -s "$logfile" ]] && return

    local clean
    clean="$(_strip_ansi < "$logfile")"

    # Collect FAILED lines from "short test summary info" sections
    local -a failed_ids=()
    while IFS= read -r line; do
        if [[ "$line" =~ ^FAILED\ (tests/.+) ]]; then
            local node="${BASH_REMATCH[1]}"
            # Strip trailing " - ..." pytest reason suffix if present
            node="${node%% - *}"
            failed_ids+=("$node")
        fi
    done <<< "$clean"

    [[ ${#failed_ids[@]} -eq 0 ]] && return

    # Deduplicate (same test can appear in multiple suite runs)
    local -A seen=()
    local -a unique=()
    for id in "${failed_ids[@]}"; do
        if [[ -z "${seen[$id]:-}" ]]; then
            seen[$id]=1
            unique+=("$id")
        fi
    done

    echo -e "${YELLOW}${BOLD}━━━ RERUN FAILED TESTS ━━━${NC}"
    echo ""
    echo -e "./utils/run_tests.sh s \"${unique[*]}\""
    echo ""
}

_flush_test_block() {
    local test_name="$1"
    local -n _errors=$2
    local -n _logs=$3

    echo "## $test_name FAILED"

    # Error lines
    if [[ ${#_errors[@]} -gt 0 ]]; then
        echo "Error:"
        local i start=0
        if [[ ${#_errors[@]} -gt 5 ]]; then
            start=$(( ${#_errors[@]} - 5 ))
        fi
        for (( i=start; i<${#_errors[@]}; i++ )); do
            echo "  ${_errors[$i]}"
        done
    fi

    # Log lines (last 30)
    if [[ ${#_logs[@]} -gt 0 ]]; then
        echo "Log:"
        local i start=0
        if [[ ${#_logs[@]} -gt 30 ]]; then
            start=$(( ${#_logs[@]} - 30 ))
        fi
        for (( i=start; i<${#_logs[@]}; i++ )); do
            # Trim the verbose prefix (timestamps, module paths)
            local trimmed
            trimmed="$(echo "${_logs[$i]}" | sed 's/^.*kiso\./kiso./; s/^INFO  *//; s/^WARNING  */⚠ /')"
            echo "  $trimmed"
        done
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------
FAILED=0

_format_elapsed() {
    local secs=$1
    if [[ $secs -ge 60 ]]; then
        echo "$((secs / 60))m $((secs % 60))s"
    else
        echo "${secs}s"
    fi
}

run_suite() {
    local name="$1"; shift
    echo -e "\n${YELLOW}━━━ $name ━━━${NC}"
    local start=$SECONDS rc=0
    # Use 'script' to create a pseudo-TTY so all commands (Docker BuildKit,
    # pytest, bats) see a real terminal and emit colors. COLUMNS propagates
    # the terminal width so pytest aligns PASSED/FAILED correctly.
    FORCE_COLOR=1 PY_COLORS=1 COLUMNS="${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}" \
        script -qefc "$(printf '%q ' "$@")" /dev/null \
        | tee -a "$_CAPTURE_LOG" || rc=${PIPESTATUS[0]}
    local elapsed=$(( SECONDS - start ))
    local time_str="$(_format_elapsed $elapsed)"
    if [[ "$rc" -eq 0 || "$rc" -eq 5 ]]; then
        echo -e "${GREEN}✓ $name: PASSED${NC} ${DIM}(${time_str})${NC}"
    else
        echo -e "${RED}✗ $name: FAILED${NC} ${DIM}(${time_str})${NC}"
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
            docker compose -f docker-compose.test.yml run --build --rm \
            -e FORCE_COLOR=1 test-docker
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
        -e FORCE_COLOR=1 \
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
        -e FORCE_COLOR=1 \
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
            -e FORCE_COLOR=1 \
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
        -e FORCE_COLOR=1 \
        test-functional \
        uv run pytest tests/interactive/ -v --interactive --functional
}

# ---------------------------------------------------------------------------
# Run a specific test — auto-detect host vs Docker from path
# ---------------------------------------------------------------------------
_run_specific() {
    # $spec is intentionally unquoted in command position to allow
    # multi-arg patterns like: tests/test_brain.py -k "pip_install"
    local spec="$1"

    # Use regex prefix match (not glob) so nested paths work
    if [[ "$spec" =~ ^tests/docker/ ]]; then
        if [[ "$HAS_DOCKER" != true ]]; then
            echo -e "${RED}This test needs Docker (not available)${NC}"
            return
        fi
        run_suite "Specific test" \
            docker compose -f docker-compose.test.yml run --build --rm \
            -e FORCE_COLOR=1 \
            test-docker \
            uv run pytest $spec -v
    elif [[ "$spec" =~ ^tests/functional/ ]]; then
        if [[ "$HAS_DOCKER" != true ]]; then
            echo -e "${RED}This test needs Docker (not available)${NC}"
            return
        fi
        if [[ "$HAS_API_KEY" != true ]]; then
            echo -e "${RED}This test needs OPENROUTER_API_KEY (not set)${NC}"
            return
        fi
        run_suite "Specific test" \
            docker compose -f docker-compose.test.yml run --build --rm \
            -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
            -e FORCE_COLOR=1 \
            test-functional \
            uv run pytest $spec -v --functional --extended
    elif [[ "$spec" =~ ^tests/live/ ]]; then
        run_suite "Specific test" \
            uv run pytest $spec -v --llm-live --live-network
    elif [[ "$spec" =~ ^tests/integration/ ]]; then
        run_suite "Specific test" \
            uv run pytest $spec -v --integration
    else
        run_suite "Specific test" \
            uv run pytest $spec -v
    fi
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
        # No suite flags → run all automatic (skip only interactive)
        [[ "$skip_unit" == false ]]        && run_unit
        [[ "$skip_bash" == false ]]        && run_bash
        [[ "$skip_integration" == false ]] && run_integration
        [[ "$skip_live" == false ]]        && run_live
        [[ "$skip_docker" == false ]]      && run_docker
        [[ "$skip_plugins" == false ]]     && run_plugins ""
        [[ "$skip_func" == false ]]        && run_functional
        run_extended
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
            --fast)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_plugins ""
                ;;
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
                echo "Available: --unit --bash --integration --live --docker --func/--functional --extended --interactive --plugins[=filter] --fast --all"
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
    echo -e "  ${CYAN}a${NC}  All automatic           ${DIM}1-8 (skip 9 interactive)${NC}"
    echo -e "  ${CYAN}f${NC}  Fast all               ${DIM}1-6 (~3min, skip pipeline tests)${NC}"
    echo -e "  ${CYAN}s${NC}  Run specific test       ${DIM}path::Class::test or -k pattern${NC}"
    echo ""

    local choice
    read -rp "  Choose [1-9, a, f, s, comma-separated, or 'q' to quit]: " choice

    if [[ "$choice" == "q" || "$choice" == "Q" || -z "$choice" ]]; then
        echo "Aborted."
        exit 0
    fi

    _process_choices "$choice"
}

# Process one or more comma-separated menu choices.
# Used by both interactive menu and direct CLI invocation.
_process_choices() {
    local input="$1"
    local extra="${2:-}"  # extra args (e.g., test spec for "s")

    IFS=',' read -ra selections <<< "$input"

    for sel in "${selections[@]}"; do
        sel="${sel// /}"  # trim spaces
        case "$sel" in
            1) run_unit ;;
            2) run_bash ;;
            3) run_integration ;;
            4) run_live ;;
            5) run_docker ;;
            6)
                if [[ -n "$extra" ]]; then
                    run_plugins "$extra"
                else
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
                fi
                ;;
            7) run_functional ;;
            8) run_extended ;;
            9) run_interactive ;;
            a|A)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_plugins ""
                run_functional
                run_extended
                ;;
            f|F)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_plugins ""
                ;;
            s|S)
                if [[ -n "$extra" ]]; then
                    _run_specific "$extra"
                else
                    echo ""
                    echo -e "  ${DIM}Examples:${NC}"
                    echo -e "  ${DIM}  tests/live/test_roles.py::TestPlannerSystemPackageLive::test_python_lib_uses_uv_pip${NC}"
                    echo -e "  ${DIM}  tests/test_brain.py -k \"pip_install\"${NC}"
                    echo -e "  ${DIM}  tests/functional/test_core_flows.py::TestF18SimpleQA${NC}"
                    echo ""
                    local spec
                    read -rp "  pytest args: " spec
                    if [[ -z "$spec" ]]; then
                        echo -e "${RED}No pattern provided${NC}"
                    else
                        _run_specific "$spec"
                    fi
                fi
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
elif [[ -n "${1:-}" ]]; then
    # Direct invocation: ./run_tests.sh 4  or  ./run_tests.sh 1,3  or  ./run_tests.sh s "tests/..."
    _process_choices "$1" "${2:-}"
else
    run_interactive_menu
fi

echo ""
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All executed suites passed.${NC}"
else
    _extract_failure_summary "$_CAPTURE_LOG"
    _extract_rerun_snippet "$_CAPTURE_LOG"
    echo -e "${RED}Some suites failed — check output above.${NC}"
    exit 1
fi
