#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

@test "resolve: no instances.json → error" {
    run kiso_run instance start
    [ "$status" -ne 0 ]
    [[ "$output" == *"no instances configured"* ]]
}

@test "resolve: 0 instances in json → error" {
    create_instances  # creates empty {}
    run kiso_run instance start
    [ "$status" -ne 0 ]
    [[ "$output" == *"no instances configured"* ]]
}

@test "resolve: 1 instance → used implicitly" {
    create_instances jarvis 8333 9000
    run kiso_run instance start
    [ "$status" -eq 0 ]
    docker_was_called "start kiso-jarvis"
}

@test "resolve: 2+ instances with no --instance → error listing all" {
    create_instances jarvis 8333 9000 work 8334 9100
    run kiso_run instance start
    [ "$status" -ne 0 ]
    [[ "$output" == *"multiple instances"* ]]
    [[ "$output" == *"jarvis"* ]]
    [[ "$output" == *"work"* ]]
}

@test "resolve: explicit --instance with known name → used" {
    create_instances jarvis 8333 9000 work 8334 9100
    run kiso_run --instance work instance start
    [ "$status" -eq 0 ]
    docker_was_called "start kiso-work"
}

@test "resolve: explicit --instance with unknown name → error" {
    create_instances jarvis 8333 9000
    run kiso_run --instance ghost instance start
    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]]
}
