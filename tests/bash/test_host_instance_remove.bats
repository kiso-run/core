#!/usr/bin/env bats
load 'helpers'

setup() {
    setup_kiso_env
    create_instances mybot 8333 9000
}

@test "instance remove --yes: removes without prompt" {
    run kiso_run instance remove mybot --yes
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed"* ]]
    ! instance_exists mybot
}

@test "instance remove -y: short flag also skips prompt" {
    run kiso_run instance remove mybot -y
    [ "$status" -eq 0 ]
    ! instance_exists mybot
}

@test "instance remove with 'y' confirmation: removes after prompt" {
    run bash -c "export HOME='$HOME' PATH='$PATH' KISO_DIR='$KISO_DIR'; printf 'y\n' | $KISO_HOST instance remove mybot 2>&1"
    [ "$status" -eq 0 ]
    [[ "$output" == *"removed"* ]]
}

@test "instance remove with 'n' confirmation: aborts" {
    run bash -c "export HOME='$HOME' PATH='$PATH' KISO_DIR='$KISO_DIR'; printf 'n\n' | $KISO_HOST instance remove mybot 2>&1"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Aborted"* ]]
    instance_exists mybot
}

@test "instance remove: removes only the target, keeps others in instances.json" {
    create_instances mybot 8333 9000 other 8334 9100
    run kiso_run instance remove mybot --yes
    [ "$status" -eq 0 ]
    ! instance_exists mybot
    instance_exists other
}
