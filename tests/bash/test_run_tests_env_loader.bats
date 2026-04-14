#!/usr/bin/env bats
#
# Tests for the `.env` loader helper used by utils/run_tests.sh.
# The loader lives in utils/_load_env.sh and exposes:
#   _load_env_file PATH   → source a .env file, idempotent:
#                            - skips missing files silently
#                            - skips comments and blank lines
#                            - never overwrites already-exported vars

load 'helpers'

LOADER="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)/utils/_load_env.sh"

setup() {
    export HOME="$BATS_TEST_TMPDIR"
    unset FOO BAR BAZ QUX
}

@test "loader: sources variables from a .env file" {
    printf 'FOO=one\nBAR=two\n' > "$BATS_TEST_TMPDIR/.env"
    run bash -c "source '$LOADER' && _load_env_file '$BATS_TEST_TMPDIR/.env' && echo \"\$FOO|\$BAR\""
    [ "$status" -eq 0 ]
    [ "$output" = "one|two" ]
}

@test "loader: does not overwrite variables already in the environment" {
    printf 'FOO=from-file\n' > "$BATS_TEST_TMPDIR/.env"
    run bash -c "export FOO=from-shell; source '$LOADER' && _load_env_file '$BATS_TEST_TMPDIR/.env' && echo \"\$FOO\""
    [ "$status" -eq 0 ]
    [ "$output" = "from-shell" ]
}

@test "loader: ignores comments and blank lines" {
    cat > "$BATS_TEST_TMPDIR/.env" <<'EOF'
# this is a comment
FOO=one

# another comment
BAR=two
EOF
    run bash -c "source '$LOADER' && _load_env_file '$BATS_TEST_TMPDIR/.env' && echo \"\$FOO|\$BAR\""
    [ "$status" -eq 0 ]
    [ "$output" = "one|two" ]
}

@test "loader: missing file is a silent no-op" {
    run bash -c "source '$LOADER' && _load_env_file '$BATS_TEST_TMPDIR/does-not-exist.env' && echo ok"
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}

@test "loader: preserves values containing '=' signs" {
    printf 'FOO=a=b=c\n' > "$BATS_TEST_TMPDIR/.env"
    run bash -c "source '$LOADER' && _load_env_file '$BATS_TEST_TMPDIR/.env' && echo \"\$FOO\""
    [ "$status" -eq 0 ]
    [ "$output" = "a=b=c" ]
}

@test "run_tests.sh: loads repo-root .env before sourcing instance .env" {
    # Create a fake repo layout with a .env at the repo root.
    # We source run_tests.sh in a sub-shell with KISO_RUN_TESTS_DRY=1 so it
    # exits before doing any pytest work, then inspect the variable.
    local repo_root
    repo_root="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
    printf 'KISO_REPO_ENV_LOADER_PROBE=yes\n' > "$repo_root/.env.bats-probe"

    run bash -c "
        cd '$repo_root'
        export KISO_ENV_FILE='/nonexistent/instance.env'
        source utils/_load_env.sh
        _load_env_file '$repo_root/.env.bats-probe'
        echo \"\$KISO_REPO_ENV_LOADER_PROBE\"
    "
    rm -f "$repo_root/.env.bats-probe"
    [ "$status" -eq 0 ]
    [ "$output" = "yes" ]
}
