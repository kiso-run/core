#!/usr/bin/env bats
#
# Tests for _offer_test_times_refresh in utils/run_tests.sh.
# The function must:
#   - silently skip when stdin is not a TTY (CI / pipe)
#   - silently skip when fewer than 3 suites ran
#   - silently skip when the updater script is missing
#   - silently skip when the log file is missing

load 'helpers'

RUN_TESTS="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)/utils/run_tests.sh"

# Source just the function definition out of run_tests.sh by extracting
# it between the function header and its closing brace, so we can call
# it in isolation without triggering the runner's top-level execution.
_load_offer_fn() {
    local tmp="$BATS_TEST_TMPDIR/_offer.sh"
    awk '
        /^_offer_test_times_refresh\(\) \{/ {capture=1}
        capture {print}
        capture && /^\}$/ {exit}
    ' "$RUN_TESTS" > "$tmp"
    # Stub the env the function expects.
    cat > "$BATS_TEST_TMPDIR/harness.sh" <<EOF
_REPO_ROOT="$BATS_TEST_TMPDIR/repo"
mkdir -p "\$_REPO_ROOT/utils"
YELLOW=''
DIM=''
NC=''
source "$tmp"
EOF
    echo "$BATS_TEST_TMPDIR/harness.sh"
}

@test "offer: silently skips when stdin is not a TTY" {
    local harness
    harness="$(_load_offer_fn)"
    # Create a fake log file and updater so only the TTY check remains.
    local log="$BATS_TEST_TMPDIR/run.log"
    : > "$log"
    mkdir -p "$BATS_TEST_TMPDIR/repo/utils"
    touch "$BATS_TEST_TMPDIR/repo/utils/update_test_times.py"
    # stdin redirected from /dev/null → not a TTY
    run bash -c "source '$harness' && _offer_test_times_refresh '$log' 5 < /dev/null"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "offer: skips when fewer than 3 suites ran" {
    local harness
    harness="$(_load_offer_fn)"
    local log="$BATS_TEST_TMPDIR/run.log"
    : > "$log"
    mkdir -p "$BATS_TEST_TMPDIR/repo/utils"
    touch "$BATS_TEST_TMPDIR/repo/utils/update_test_times.py"
    run bash -c "source '$harness' && _offer_test_times_refresh '$log' 2 < /dev/null"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "offer: skips when updater script missing" {
    local harness
    harness="$(_load_offer_fn)"
    local log="$BATS_TEST_TMPDIR/run.log"
    : > "$log"
    # Note: updater NOT created this time.
    run bash -c "source '$harness' && _offer_test_times_refresh '$log' 5 < /dev/null"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "offer: skips when log file missing" {
    local harness
    harness="$(_load_offer_fn)"
    mkdir -p "$BATS_TEST_TMPDIR/repo/utils"
    touch "$BATS_TEST_TMPDIR/repo/utils/update_test_times.py"
    run bash -c "source '$harness' && _offer_test_times_refresh '$BATS_TEST_TMPDIR/nope.log' 5 < /dev/null"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}
