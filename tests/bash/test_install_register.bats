#!/usr/bin/env bats
load 'helpers'

setup() { setup_kiso_env; }

@test "install register_instance: creates instances.json when missing" {
    run install_func register_instance mybot 8333 9000
    [ "$status" -eq 0 ]
    instance_exists mybot
}

@test "install register_instance: adds entry without overwriting existing instances" {
    create_instances existing 8333 9000
    run install_func register_instance newbot 8334 9100
    [ "$status" -eq 0 ]
    instance_exists existing
    instance_exists newbot
}

@test "install register_instance: stores correct server_port and connector_port_base" {
    run install_func register_instance mybot 8444 9200
    [ "$status" -eq 0 ]
    [ "$(json_field mybot server_port)" = "8444" ]
    [ "$(json_field mybot connector_port_base)" = "9200" ]
}
