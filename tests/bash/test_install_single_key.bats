#!/usr/bin/env bats
# Single-key install.sh regression (M1533 follow-up).
#
# Validates the non-interactive curl|sh path: with only
# OPENROUTER_API_KEY set in the environment, install.sh sourced in
# library mode must resolve the API key without prompting.
#
# Also locks down the `ask_models` FALLBACK heredoc so it stays in
# sync with the v0.10 model catalog (no stale `searcher`).

load 'helpers'

setup() {
    setup_kiso_env
}

# ── ask_api_key: env-var fallback ──────────────────────────────────

@test "install ask_api_key: --api-key wins over env" {
    # Sanity — explicit --api-key is still authoritative.
    # install.sh resets ARG_API_KEY="" at top-level, so the flag
    # must be re-assigned AFTER sourcing. Stderr is redirected to
    # /dev/null so only the echoed API_KEY reaches $output.
    local out
    out=$(bash -c '
        export KISO_INSTALL_LIB=1
        export OPENROUTER_API_KEY="env-key"
        source "'"$INSTALL_SH"'" 2>/dev/null
        ARG_API_KEY="explicit-flag-key"
        ask_api_key 2>/dev/null
        printf "%s" "$API_KEY"
    ')
    [ "$out" = "explicit-flag-key" ]
}

@test "install ask_api_key: OPENROUTER_API_KEY env var is used when --api-key absent" {
    # This is the curl|sh path. No --api-key flag, no TTY, but the
    # env var is set → API_KEY must be taken from it (no prompt).
    local out
    out=$(bash -c '
        export KISO_INSTALL_LIB=1
        export OPENROUTER_API_KEY="env-provided-sk-or-v1"
        source "'"$INSTALL_SH"'" 2>/dev/null
        ask_api_key 2>/dev/null < /dev/null
        printf "%s" "$API_KEY"
    ')
    [ "$out" = "env-provided-sk-or-v1" ]
}

# ── FALLBACK heredoc: no retired roles ────────────────────────────

@test "install fallback heredoc: no retired searcher row" {
    run grep -c "^searcher|" "$INSTALL_SH"
    # grep -c prints 0 with status 1 when the pattern is absent.
    # We treat either "0 with status 1" or "0 with status 0" as pass.
    [ "$output" = "0" ]
}

@test "install fallback heredoc: has v0.10 consolidator + mcp_sampling rows" {
    run grep -c "^consolidator|" "$INSTALL_SH"
    [ "$output" = "1" ]

    run grep -c "^mcp_sampling|" "$INSTALL_SH"
    [ "$output" = "1" ]
}

# ── KISO_LLM_API_KEY gone ────────────────────────────────────────

@test "install.sh: no lingering KISO_LLM_API_KEY reference" {
    run grep -c "KISO_LLM_API_KEY" "$INSTALL_SH"
    [ "$output" = "0" ]
}
