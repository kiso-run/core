#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

# Helper: call _derive_instance_name from install.sh in lib mode
_derive() {
    install_func _derive_instance_name "$1"
}

@test "install _derive_instance_name: simple name passthrough" {
    run _derive "kiso"
    [ "$status" -eq 0 ]
    [ "$output" = "kiso" ]
}

@test "install _derive_instance_name: uppercase → lowercase" {
    run _derive "Jarvis"
    [ "$status" -eq 0 ]
    [ "$output" = "jarvis" ]
}

@test "install _derive_instance_name: spaces → hyphens" {
    run _derive "My Bot"
    [ "$status" -eq 0 ]
    [ "$output" = "my-bot" ]
}

@test "install _derive_instance_name: underscores → hyphens" {
    run _derive "work_bot"
    [ "$status" -eq 0 ]
    [ "$output" = "work-bot" ]
}

@test "install _derive_instance_name: special chars stripped" {
    run _derive "My Bot!"
    [ "$status" -eq 0 ]
    [ "$output" = "my-bot" ]
}

@test "install _derive_instance_name: consecutive hyphens collapsed" {
    run _derive "My  Bot"
    [ "$status" -eq 0 ]
    [ "$output" = "my-bot" ]
}

@test "install _derive_instance_name: leading/trailing hyphens stripped" {
    run _derive " - bot - "
    [ "$status" -eq 0 ]
    [ "$output" = "bot" ]
}

@test "install _derive_instance_name: truncated at 32 chars" {
    run _derive "$(python3 -c "print('a' * 40)")"
    [ "$status" -eq 0 ]
    [ "${#output}" -le 32 ]
}

@test "install _derive_instance_name: empty input → 'kiso' fallback" {
    run _derive "!!!"
    [ "$status" -eq 0 ]
    [ "$output" = "kiso" ]
}

@test "install _derive_instance_name: digits preserved" {
    run _derive "Work Bot 2"
    [ "$status" -eq 0 ]
    [ "$output" = "work-bot-2" ]
}
