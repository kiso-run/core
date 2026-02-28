#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

# ── valid names ───────────────────────────────────────────────────────────────

@test "name validation: 'kiso' is valid" {
    run kiso_run instance create kiso
    [ "$status" -eq 0 ]
}

@test "name validation: 'my-bot' with hyphen is valid" {
    run kiso_run instance create my-bot
    [ "$status" -eq 0 ]
}

@test "name validation: single character 'a' is valid" {
    run kiso_run instance create a
    [ "$status" -eq 0 ]
}

@test "name validation: alphanumeric 'bot2' is valid" {
    run kiso_run instance create bot2
    [ "$status" -eq 0 ]
}

# ── invalid names ─────────────────────────────────────────────────────────────

@test "name validation: uppercase 'MyBot' is rejected" {
    run kiso_run instance create MyBot
    [ "$status" -ne 0 ]
    [[ "$output" == *"lowercase"* ]]
}

@test "name validation: underscore 'bot_name' is rejected" {
    run kiso_run instance create bot_name
    [ "$status" -ne 0 ]
}

@test "name validation: trailing hyphen 'bot-' is rejected" {
    run kiso_run instance create bot-
    [ "$status" -ne 0 ]
    [[ "$output" == *"hyphen"* ]]
}

@test "name validation: name longer than 32 chars is rejected" {
    local long_name
    long_name="$(python3 -c "print('a' * 33)")"
    run kiso_run instance create "$long_name"
    [ "$status" -ne 0 ]
    [[ "$output" == *"too long"* ]]
}

@test "name validation: empty name is rejected" {
    run kiso_run instance create ""
    [ "$status" -ne 0 ]
}
