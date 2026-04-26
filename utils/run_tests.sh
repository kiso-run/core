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
# Precedence (highest to lowest): parent shell > repo `.env` > instance `.env`.
# The repo `.env` is the canonical place for developer/test credentials and
# lives alongside the code; the instance `.env` belongs to an installed Kiso
# runtime and is used as a fallback for environments that only have the
# runtime file configured.
_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=_load_env.sh
source "$_REPO_ROOT/utils/_load_env.sh"
_load_env_file "$_REPO_ROOT/.env"
_ENV_FILE="${KISO_ENV_FILE:-$HOME/.kiso/instances/kiso/.env}"
_load_env_file "$_ENV_FILE"

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
# Parallel + tmpfs flags for fast tiers (unit, integration)
# ---------------------------------------------------------------------------
# pytest-xdist parallelizes test execution across CPU cores. Each xdist
# worker is a separate subprocess so module-level state in kiso.main
# (`_workers`, `_worker_phases`, `_rate_limiter`) is naturally isolated
# per worker.
#
# tmpfs (`/dev/shm`) gives RAM-speed storage for the test temp tree
# without requiring any test code changes (init_db keeps using a real
# filesystem path, just one that lives in RAM). On non-Linux platforms
# we silently fall back to the default basetemp.
PYTEST_PARALLEL=(-n auto)
PYTEST_BASETEMP=()
if [[ -d /dev/shm ]]; then
    _SHM_BASETEMP="/dev/shm/pytest-kiso-$$"
    PYTEST_BASETEMP=(--basetemp="$_SHM_BASETEMP")
    trap 'rm -rf "$_SHM_BASETEMP" "$_CAPTURE_DIR"' EXIT
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
        if [[ "$line" =~ ^FAILED\ (\([0-9.smh\ ]+\)\ )?(tests/.+) ]]; then
            local node="${BASH_REMATCH[2]}"
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

    # Group by test type so each command gets the right runner flags
    local -a functional=() live=() integration=() docker_tests=() other=()
    for id in "${unique[@]}"; do
        if [[ "$id" =~ ^tests/functional/ ]]; then
            functional+=("$id")
        elif [[ "$id" =~ ^tests/live/ ]]; then
            live+=("$id")
        elif [[ "$id" =~ ^tests/integration/ ]]; then
            integration+=("$id")
        elif [[ "$id" =~ ^tests/docker/ ]]; then
            docker_tests+=("$id")
        else
            other+=("$id")
        fi
    done

    echo -e "${YELLOW}${BOLD}━━━ RERUN FAILED TESTS ━━━${NC}"
    echo ""
    local -a cmds=()
    [[ ${#other[@]} -gt 0 ]]        && cmds+=("./utils/run_tests.sh s \"${other[*]}\"")
    [[ ${#live[@]} -gt 0 ]]         && cmds+=("./utils/run_tests.sh s \"${live[*]}\"")
    [[ ${#integration[@]} -gt 0 ]]  && cmds+=("./utils/run_tests.sh s \"${integration[*]}\"")
    [[ ${#docker_tests[@]} -gt 0 ]] && cmds+=("./utils/run_tests.sh s \"${docker_tests[*]}\"")
    [[ ${#functional[@]} -gt 0 ]]   && cmds+=("./utils/run_tests.sh s \"${functional[*]}\"")
    local result="${cmds[0]}"
    local i
    for (( i=1; i<${#cmds[@]}; i++ )); do
        result+=" && ${cmds[$i]}"
    done
    echo -e "$result"
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

# Track suite results for the recap
declare -a _SUITE_NAMES=()
declare -a _SUITE_STATUSES=()   # "passed" | "failed" | "skipped"
declare -a _SUITE_TIMES=()
declare -a _SUITE_DETAILS=()    # "N passed, M failed" or skip reason

_format_elapsed() {
    local secs=$1
    if [[ $secs -ge 60 ]]; then
        echo "$((secs / 60))m $((secs % 60))s"
    else
        echo "${secs}s"
    fi
}

# Record a skipped suite (called when prerequisites are missing).
_record_skip() {
    local name="$1" reason="$2"
    _SUITE_NAMES+=("$name")
    _SUITE_STATUSES+=("skipped")
    _SUITE_TIMES+=("-")
    _SUITE_DETAILS+=("$reason")
}

# Extract test counts from captured output.  Sets _PYTEST_SUMMARY.
# Handles pytest ("3 passed, 1 failed in 2.50s") and bats ("ok 1 ..").
_extract_pytest_counts() {
    local logfile="$1"
    _PYTEST_SUMMARY=""
    [[ ! -s "$logfile" ]] && return
    local clean
    clean="$(_strip_ansi < "$logfile")"

    # pytest: grab the last summary line including "in X.XXs"
    local line
    line="$(echo "$clean" | grep -oP '\d+ (passed|failed|skipped|error|warnings?|deselected)(,\s*\d+ (passed|failed|skipped|error|warnings?|deselected))*(\s+in\s+[\d.]+s)?' | tail -1)" || true
    if [[ -n "$line" ]]; then
        _PYTEST_SUMMARY="$line"
        return
    fi

    # bats: TAP format ("ok N"/"not ok N") or pretty ("N/M ✓"/"N/M ✗")
    local ok not_ok
    ok="$(echo "$clean" | grep -cP '(^ok \d+|\d+/\d+\s*✓)' 2>/dev/null)" || ok=0
    not_ok="$(echo "$clean" | grep -cP '(^not ok \d+|\d+/\d+\s*✗)' 2>/dev/null)" || not_ok=0
    if [[ $(( ok + not_ok )) -gt 0 ]]; then
        _PYTEST_SUMMARY="${ok} passed"
        if [[ $not_ok -gt 0 ]]; then
            _PYTEST_SUMMARY+=", ${not_ok} failed"
        fi
    fi
}

run_suite() {
    local name="$1"; shift
    echo -e "\n${YELLOW}━━━ $name ━━━${NC}"
    local start=$SECONDS rc=0
    # Capture this suite's output separately for per-suite counting
    local _suite_log
    _suite_log="$(mktemp "$_CAPTURE_DIR/suite_XXXX.log")"
    # Use 'script' to create a pseudo-TTY for colors.
    # Prefer tput (reads real TTY) over COLUMNS env.
    local _cols
    _cols="$(tput cols 2>/dev/null)" || _cols="${COLUMNS:-120}"
    FORCE_COLOR=1 PY_COLORS=1 \
        script -qefc "export COLUMNS=$_cols; stty columns $_cols 2>/dev/null; $(printf '%q ' "$@")" /dev/null \
        | tee -a "$_CAPTURE_LOG" "$_suite_log" || rc=${PIPESTATUS[0]}
    local elapsed=$(( SECONDS - start ))
    local time_str="$(_format_elapsed $elapsed)"

    _extract_pytest_counts "$_suite_log"
    local detail="${_PYTEST_SUMMARY:-done}"

    if [[ "$rc" -eq 0 || "$rc" -eq 5 ]]; then
        echo -e "${GREEN}✓ $name: PASSED${NC} ${DIM}(${time_str})${NC}"
        _SUITE_NAMES+=("$name")
        _SUITE_STATUSES+=("passed")
        _SUITE_TIMES+=("$time_str")
        _SUITE_DETAILS+=("$detail")
    else
        echo -e "${RED}✗ $name: FAILED${NC} ${DIM}(${time_str})${NC}"
        FAILED=1
        _SUITE_NAMES+=("$name")
        _SUITE_STATUSES+=("failed")
        _SUITE_TIMES+=("$time_str")
        _SUITE_DETAILS+=("$detail")
    fi
    rm -f "$_suite_log"
}

# ---------------------------------------------------------------------------
# Suite definitions
# ---------------------------------------------------------------------------
run_unit() {
    run_suite "Unit tests" uv run pytest tests/ -q \
        --ignore=tests/live --ignore=tests/docker \
        --ignore=tests/functional --ignore=tests/integration \
        --ignore=tests/interactive \
        "${PYTEST_PARALLEL[@]}" "${PYTEST_BASETEMP[@]}"
}

run_bash() {
    if [[ "$HAS_BATS" == true ]]; then
        run_suite "Bash tests" bats tests/bash/
    else
        echo -e "${YELLOW}⚠ Skipping bash tests — bats not installed${NC}"
        _record_skip "Bash tests" "no bats"
    fi
}

run_integration() {
    run_suite "Integration tests" uv run pytest tests/integration/ -v --integration \
        "${PYTEST_PARALLEL[@]}" "${PYTEST_BASETEMP[@]}"
}

run_live() {
    if [[ "$HAS_API_KEY" == true ]]; then
        run_suite "Live tests" uv run pytest tests/live/ -v --live-network --llm-live
    else
        echo -e "${YELLOW}⚠ Skipping live tests — no API key${NC}"
        _record_skip "Live tests" "no API key"
    fi
}

run_docker() {
    if [[ "$HAS_DOCKER" == true ]]; then
        run_suite "Docker tests" \
            docker compose -f docker-compose.test.yml run --build --rm \
            -e FORCE_COLOR=1 test-docker
    else
        echo -e "${YELLOW}⚠ Skipping docker tests — no Docker${NC}"
        _record_skip "Docker tests" "no Docker"
    fi
}

run_functional() {
    if [[ "$HAS_DOCKER" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping functional tests — no Docker${NC}"
        _record_skip "Functional tests" "no Docker"
        return
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping functional tests — no API key${NC}"
        _record_skip "Functional tests" "no API key"
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
        echo -e "${YELLOW}⚠ Skipping extended tests — no Docker${NC}"
        _record_skip "Extended tests" "no Docker"
        return
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping extended tests — no API key${NC}"
        _record_skip "Extended tests" "no API key"
        return
    fi
    run_suite "Extended tests" \
        docker compose -f docker-compose.test.yml run --build --rm \
        -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
        -e FORCE_COLOR=1 \
        test-functional \
        uv run pytest tests/functional/ -v --functional --extended -m extended
}

run_interactive() {
    if [[ "$HAS_DOCKER" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping interactive tests — no Docker${NC}"
        _record_skip "Interactive tests" "no Docker"
        return
    fi
    if [[ "$HAS_API_KEY" != true ]]; then
        echo -e "${YELLOW}⚠ Skipping interactive tests — no API key${NC}"
        _record_skip "Interactive tests" "no API key"
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
    local skip_live=false skip_docker=false skip_func=false
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
            --fast)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                ;;
            --all)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_functional
                run_extended
                run_interactive
                ;;
            *)
                echo -e "${RED}Unknown flag: $flag${NC}"
                echo "Available: --unit --bash --integration --live --docker --func/--functional --extended --interactive --fast --all"
                echo "Skip flags: --no-unit --no-bash --no-integration --no-live --no-docker --no-func"
                exit 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Menu helpers: read count + avg from utils/test_times.json and format
# "N tests, ~Xs/~Xm" for each tier. Refresh the JSON after a full run via
# `uv run python utils/update_test_times.py <run-log>`.
# ---------------------------------------------------------------------------
_tier_line() {
    local tier="$1"
    local json="$_REPO_ROOT/utils/test_times.json"
    if [[ ! -f "$json" ]]; then
        echo "— no data —"
        return
    fi
    uv run python -c "
import json, sys
tier = sys.argv[1]
data = json.load(open(sys.argv[2]))
entry = data.get(tier)
if not entry:
    print('— no data —')
    sys.exit(0)
count = entry['count']
avg = entry['avg_seconds']
total = count * avg
if total < 60:
    t = f'~{total:.0f}s'
elif total < 3600:
    mins = total / 60
    t = f'~{mins:.0f}m' if mins >= 2 else f'~{total:.0f}s'
else:
    t = f'~{total/3600:.1f}h'
print(f'{count} tests, {t}')
" "$tier" "$json" 2>/dev/null || echo "— no data —"
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

    # Numbers computed from utils/test_times.json. Refresh after a
    # full matrix run via `uv run python utils/update_test_times.py <log>`.
    local t_unit t_bash t_int t_live t_docker t_plugin t_func t_ext
    t_unit="$(_tier_line 'Unit tests')"
    t_bash="$(_tier_line 'Bash tests')"
    t_int="$(_tier_line 'Integration tests')"
    t_live="$(_tier_line 'Live tests')"
    t_docker="$(_tier_line 'Docker tests')"
    t_func="$(_tier_line 'Functional tests')"
    t_ext="$(_tier_line 'Extended tests')"

    echo ""
    echo -e "  ${BOLD}Kiso Test Runner${NC}"
    echo ""
    echo -e "  ${DIM}── Fast (host only) ──────────────────────────${NC}"
    echo -e "  ${CYAN}1${NC}  Unit tests              ${DIM}${t_unit} (xdist)${NC}"
    echo -e "  ${CYAN}2${NC}  Bash tests              ${DIM}${t_bash}${NC}${miss_bats}"
    echo -e "  ${CYAN}3${NC}  Integration tests       ${DIM}${t_int}, mock LLM (xdist)${NC}"
    echo ""
    echo -e "  ${DIM}── Real LLM (needs API key) ──────────────────${NC}"
    echo -e "  ${CYAN}4${NC}  Live tests              ${DIM}${t_live}${NC}${miss_api}"
    echo -e "     ${DIM}LLM compliance — prompts, schemas, roles${NC}"
    echo ""
    echo -e "  ${DIM}── Docker container ──────────────────────────${NC}"
    echo -e "  ${CYAN}5${NC}  Docker tests            ${DIM}${t_docker}${NC}${miss_docker}"
    echo ""
    echo -e "  ${DIM}── Full pipeline (Docker + API key) ─────────${NC}"
    echo -e "  ${CYAN}6${NC}  Functional tests        ${DIM}${t_func}${NC}${miss_docker}${miss_api}"
    echo -e "     ${DIM}Single-plan end-to-end: classify → plan → exec → msg${NC}"
    echo -e "  ${CYAN}7${NC}  Extended tests          ${DIM}${t_ext}, nightly${NC}${miss_docker}${miss_api}"
    echo -e "     ${DIM}Multi-plan orchestration (tool install → use → report)${NC}"
    echo ""
    echo -e "  ${DIM}── Special ──────────────────────────────────${NC}"
    echo -e "  ${CYAN}8${NC}  Interactive tests       ${DIM}requires human at terminal${NC}${miss_docker}${miss_api}"
    echo -e "  ${CYAN}a${NC}  All automatic           ${DIM}1-7 (skip 8 interactive)${NC}"
    echo -e "  ${CYAN}f${NC}  Fast all                ${DIM}1-5 (~3min, skip pipeline tests)${NC}"
    echo -e "  ${CYAN}s${NC}  Run specific test       ${DIM}path::Class::test or -k pattern${NC}"
    echo ""

    local choice
    read -rp "  Choose [1-8, a, f, s, comma-separated, or 'q' to quit]: " choice

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
            6) run_functional ;;
            7) run_extended ;;
            8) run_interactive ;;
            a|A)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
                run_functional
                run_extended
                ;;
            f|F)
                run_unit
                run_bash
                run_integration
                run_live
                run_docker
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

# ---------------------------------------------------------------------------
# Recap
# ---------------------------------------------------------------------------
_print_recap() {
    local n_passed=0 n_failed=0 n_skipped=0
    for s in "${_SUITE_STATUSES[@]}"; do
        case "$s" in
            passed)  n_passed=$(( n_passed + 1 )) ;;
            failed)  n_failed=$(( n_failed + 1 )) ;;
            skipped) n_skipped=$(( n_skipped + 1 )) ;;
        esac
    done
    local total=$(( n_passed + n_failed + n_skipped ))
    [[ $total -eq 0 ]] && return

    echo ""
    echo -e "${BOLD}━━━ RECAP ━━━${NC}"
    echo ""

    for i in "${!_SUITE_NAMES[@]}"; do
        local name="${_SUITE_NAMES[$i]}"
        local status="${_SUITE_STATUSES[$i]}"
        local time="${_SUITE_TIMES[$i]}"
        local detail="${_SUITE_DETAILS[$i]}"

        local icon color
        case "$status" in
            passed)  icon="✓"; color="$GREEN" ;;
            failed)  icon="✗"; color="$RED" ;;
            skipped) icon="⊘"; color="$YELLOW" ;;
        esac

        printf "${color}  %s %-24s${NC}" "$icon" "$name"
        if [[ "$status" == "skipped" ]]; then
            echo -e " ${DIM}($detail)${NC}"
        elif [[ "$detail" == *" in "* ]]; then
            # pytest summary already includes timing (e.g. "3696 passed in 94s")
            echo -e " ${DIM}${detail}${NC}"
        else
            echo -e " ${DIM}${detail} (${time})${NC}"
        fi
    done

    echo ""
    local summary="${GREEN}${n_passed} passed${NC}"
    if [[ $n_failed -gt 0 ]]; then
        summary+=", ${RED}${n_failed} failed${NC}"
    fi
    if [[ $n_skipped -gt 0 ]]; then
        summary+=", ${YELLOW}${n_skipped} skipped${NC}"
    fi
    echo -e "  ${BOLD}Suites:${NC} $summary"
    echo ""
}

# Tee the recap through _CAPTURE_LOG so the block lands in the file
# that update_test_times.py reads when the user accepts the refresh
# prompt below. Without this, _print_recap would write to stdout only
# and the updater would report "no recap block found in input".
_print_recap | tee -a "$_CAPTURE_LOG"

# Offer to refresh utils/test_times.json from the captured recap so
# the interactive menu's per-tier estimates stay accurate. Only
# prompts when:
#   - stdin is attached to a TTY (non-interactive CI runs skip)
#   - at least 3 suites ran (single-test reruns don't trigger the prompt)
#   - the updater script exists in the repo
# Declining leaves the JSON untouched and prints a manual-run reminder.
_offer_test_times_refresh() {
    local log_file="$1"
    local suite_count="$2"
    local updater="$_REPO_ROOT/utils/update_test_times.py"
    [[ -t 0 ]] || return 0
    [[ "$suite_count" -ge 3 ]] || return 0
    [[ -f "$updater" ]] || return 0
    [[ -f "$log_file" ]] || return 0
    echo ""
    local answer=""
    read -rp "Update utils/test_times.json from this run? [y/N] " answer || return 0
    case "$answer" in
        y|Y|yes|YES)
            uv run python "$updater" "$log_file" || {
                echo -e "${YELLOW}test_times.json refresh failed — check the run log${NC}"
                return 0
            }
            ;;
        *)
            echo -e "${DIM}skipped — run manually with: uv run python utils/update_test_times.py $log_file${NC}"
            ;;
    esac
}

_offer_test_times_refresh "$_CAPTURE_LOG" "${#_SUITE_NAMES[@]}"

if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All executed suites passed.${NC}"
else
    _extract_failure_summary "$_CAPTURE_LOG"
    _extract_rerun_snippet "$_CAPTURE_LOG"
    echo -e "${RED}Some suites failed — check output above.${NC}"
    exit 1
fi
