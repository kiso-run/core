#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

@test "port detection: no existing instances → server port 8333" {
    run kiso_run instance create fresh
    [ "$status" -eq 0 ]
    [ "$(json_field fresh server_port)" = "8333" ]
}

@test "port detection: 8333 taken in instances.json → server port 8334" {
    create_instances existing 8333 9000
    run kiso_run instance create newbot
    [ "$status" -eq 0 ]
    [ "$(json_field newbot server_port)" = "8334" ]
}

@test "port detection: no existing instances → connector base 9000" {
    run kiso_run instance create fresh
    [ "$status" -eq 0 ]
    [ "$(json_field fresh connector_port_base)" = "9000" ]
}

@test "port detection: base 9000 taken in instances.json → connector base 9100" {
    create_instances existing 8333 9000
    run kiso_run instance create newbot
    [ "$status" -eq 0 ]
    [ "$(json_field newbot connector_port_base)" = "9100" ]
}
