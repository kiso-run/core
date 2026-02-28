#!/usr/bin/env bats
# Tests for install.sh helper functions.
# Run with: bats tests/bash/test_install.bats

INSTALL_SH="$BATS_TEST_DIRNAME/../../install.sh"

# Source only the helper functions we need (skip the main body).
# We accomplish this by sourcing after defining a guard that prevents execution.
setup() {
    # Provide stubs for functions/vars that the sourced file might reference at
    # parse/load time. The main body is never reached because we only call
    # individual functions.
    export HOME="/tmp"
    export KISO_DIR="/tmp/.kiso"
    export INSTANCES_JSON="$KISO_DIR/instances.json"
    export IMAGE="kiso:latest"
    export WRAPPER_DST="$HOME/.local/bin/kiso"
    export CLEANUP_DIR=""
    export USERNAME_RE='^[a-z_][a-z0-9_-]{0,31}$'
    export INSTANCE_NAME_RE='^[a-z0-9][a-z0-9-]*$'
    export ARG_USER=""
    export ARG_API_KEY=""
    export ARG_NAME=""
    export ARG_HOST=""

    # Source only up to the first interactive section.
    # We use a subshell trick: redirect stdin from /dev/null to prevent any
    # interactive prompt from blocking, and wrap sourcing in a function.
    _source_helpers() {
        # Extract and eval only the pure helper functions (no side-effects).
        # We eval just _derive_instance_name.
        eval "$(sed -n '/_derive_instance_name()/,/^}/p' "$INSTALL_SH")"
    }
    _source_helpers
}

# --- _derive_instance_name ---

@test "_derive_instance_name: lowercase conversion" {
    result="$(_derive_instance_name "MyBot")"
    [ "$result" = "mybot" ]
}

@test "_derive_instance_name: spaces become hyphens" {
    result="$(_derive_instance_name "my bot")"
    [ "$result" = "my-bot" ]
}

@test "_derive_instance_name: underscores become hyphens" {
    result="$(_derive_instance_name "my_bot")"
    [ "$result" = "my-bot" ]
}

@test "_derive_instance_name: consecutive hyphens collapsed" {
    result="$(_derive_instance_name "my---bot")"
    [ "$result" = "my-bot" ]
}

@test "_derive_instance_name: many consecutive hyphens (O(n) not O(n^2))" {
    # Generate a 100-hyphen string — should collapse to single hyphen quickly
    input="a$(printf '%0.s-' {1..100})b"
    result="$(_derive_instance_name "$input")"
    [ "$result" = "a-b" ]
}

@test "_derive_instance_name: strips leading and trailing hyphens" {
    result="$(_derive_instance_name "-mybot-")"
    [ "$result" = "mybot" ]
}

@test "_derive_instance_name: strips non-alphanumeric chars" {
    result="$(_derive_instance_name "my@bot!")"
    [ "$result" = "mybot" ]
}

@test "_derive_instance_name: truncates to 32 chars" {
    input="$(printf '%0.sa' {1..40})"  # 40 'a' chars
    result="$(_derive_instance_name "$input")"
    [ "${#result}" -le 32 ]
}

@test "_derive_instance_name: empty input returns 'kiso'" {
    result="$(_derive_instance_name "!!!")"
    [ "$result" = "kiso" ]
}

@test "_derive_instance_name: mixed uppercase, spaces, special chars" {
    result="$(_derive_instance_name "My AI Bot 2.0!")"
    [ "$result" = "my-ai-bot-20" ]
}
