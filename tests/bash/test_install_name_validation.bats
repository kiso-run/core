#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

# Helper: call validate_instance_name from install.sh in lib mode
_validate() {
    install_func validate_instance_name "$1"
}

@test "install validate_instance_name: 'kiso' is valid" {
    run _validate kiso
    [ "$status" -eq 0 ]
}

@test "install validate_instance_name: 'my-bot' is valid" {
    run _validate my-bot
    [ "$status" -eq 0 ]
}

@test "install validate_instance_name: uppercase 'MyBot' is rejected" {
    run _validate MyBot
    [ "$status" -ne 0 ]
    [[ "$output" == *"lowercase"* ]]
}

@test "install validate_instance_name: trailing hyphen 'bot-' is rejected" {
    run _validate bot-
    [ "$status" -ne 0 ]
}

@test "install validate_instance_name: 33-char name is rejected" {
    local long
    long="$(python3 -c "print('a' * 33)")"
    run _validate "$long"
    [ "$status" -ne 0 ]
    [[ "$output" == *"too long"* ]]
}

@test "install validate_instance_name: empty string is rejected" {
    run _validate ""
    [ "$status" -ne 0 ]
}
