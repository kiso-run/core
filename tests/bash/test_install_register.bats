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

@test "install register_instance: stores version and build_hash when provided" {
    run install_func register_instance mybot 8333 9000 "0.2.0" "abc1234"
    [ "$status" -eq 0 ]
    [ "$(json_field mybot version)" = "0.2.0" ]
    [ "$(json_field mybot build_hash)" = "abc1234" ]
    # installed_at should be set
    [ -n "$(json_field mybot installed_at)" ]
}

@test "install register_instance: preserves connectors on re-register" {
    # Pre-populate with connectors
    python3 -c "
import json, pathlib
path = pathlib.Path('$KISO_DIR/instances.json')
path.parent.mkdir(parents=True, exist_ok=True)
d = {'mybot': {'server_port': 8333, 'connector_port_base': 9000, 'connectors': {'telegram': {'port': 9001}}}}
path.write_text(json.dumps(d, indent=2))
"
    run install_func register_instance mybot 8334 9100 "0.3.0" "def5678"
    [ "$status" -eq 0 ]
    [ "$(json_field mybot server_port)" = "8334" ]
    [ "$(json_field mybot version)" = "0.3.0" ]
    # connectors should be preserved
    python3 -c "
import json, sys
d = json.load(open('$KISO_DIR/instances.json'))
assert d['mybot']['connectors'] == {'telegram': {'port': 9001}}, f'connectors lost: {d}'
"
}
