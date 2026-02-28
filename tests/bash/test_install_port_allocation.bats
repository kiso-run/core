#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

@test "install next_free_server_port: no instances → 8333" {
    run install_func next_free_server_port
    [ "$status" -eq 0 ]
    [ "$output" = "8333" ]
}

@test "install next_free_server_port: 8333 taken → 8334" {
    create_instances existing 8333 9000
    run install_func next_free_server_port
    [ "$status" -eq 0 ]
    [ "$output" = "8334" ]
}

@test "install next_free_connector_base: no instances → 9000" {
    run install_func next_free_connector_base
    [ "$status" -eq 0 ]
    [ "$output" = "9000" ]
}

@test "install next_free_connector_base: 9000 taken → 9100" {
    create_instances existing 8333 9000
    run install_func next_free_connector_base
    [ "$status" -eq 0 ]
    [ "$output" = "9100" ]
}
