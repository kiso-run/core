#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

# Helper: run the safe env-read logic from install.sh against a given file.
# Prints the value of KISO_LLM_API_KEY extracted by the safe parser.
_safe_read_key() {
    local env_file="$1"
    bash -c '
        env_file="$1"
        _api_key_check="$(grep -E "^KISO_LLM_API_KEY=" "$env_file" | cut -d= -f2- | head -1)"
        echo "$_api_key_check"
    ' _ "$env_file"
}

@test "install safe env read: reads KISO_LLM_API_KEY from .env" {
    printf 'KISO_LLM_API_KEY=sk-or-v1-testkey\n' > "$BATS_TEST_TMPDIR/test.env"
    run _safe_read_key "$BATS_TEST_TMPDIR/test.env"
    [ "$status" -eq 0 ]
    [ "$output" = "sk-or-v1-testkey" ]
}

@test "install safe env read: shell command in .env is not executed" {
    local sentinel="$BATS_TEST_TMPDIR/injection_executed"
    printf 'KISO_LLM_API_KEY=mykey\ntouch %s\n' "$sentinel" > "$BATS_TEST_TMPDIR/malicious.env"
    run _safe_read_key "$BATS_TEST_TMPDIR/malicious.env"
    [ "$status" -eq 0 ]
    [ "$output" = "mykey" ]
    # The injected command must NOT have run
    [ ! -f "$sentinel" ]
}

@test "install safe env read: subshell in value is not executed" {
    local sentinel="$BATS_TEST_TMPDIR/subshell_executed"
    printf 'KISO_LLM_API_KEY=$(touch %s)\n' "$sentinel" > "$BATS_TEST_TMPDIR/subshell.env"
    run _safe_read_key "$BATS_TEST_TMPDIR/subshell.env"
    [ "$status" -eq 0 ]
    # The subshell must NOT have run
    [ ! -f "$sentinel" ]
}

@test "install safe env read: returns empty when key absent" {
    printf 'SOME_OTHER_VAR=value\n' > "$BATS_TEST_TMPDIR/no_key.env"
    run _safe_read_key "$BATS_TEST_TMPDIR/no_key.env"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "install safe env read: ignores commented-out key" {
    printf '# KISO_LLM_API_KEY=should-be-ignored\nKISO_LLM_API_KEY=real-key\n' \
        > "$BATS_TEST_TMPDIR/commented.env"
    run _safe_read_key "$BATS_TEST_TMPDIR/commented.env"
    [ "$status" -eq 0 ]
    [ "$output" = "real-key" ]
}
