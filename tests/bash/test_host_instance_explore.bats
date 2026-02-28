#!/usr/bin/env bats
load 'helpers'

setup() {
    setup_kiso_env
    create_instances jarvis 8333 9000
}

@test "explore with SESSION: docker exec gets -e SESSION=<name>" {
    mkdir -p "$KISO_DIR/instances/jarvis/sessions/mysession"
    run kiso_run instance explore mysession
    [ "$status" -eq 0 ]
    docker_was_called "-e SESSION=mysession"
}

@test "explore: SESSION not found on host → error" {
    run kiso_run instance explore nosuchsession
    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]]
}

@test "explore without SESSION: uses hostname@user default and passes it via -e SESSION" {
    run kiso_run instance explore
    [ "$status" -eq 0 ]
    docker_was_called "exec"
    grep -q "SESSION=" "$BATS_TEST_TMPDIR/docker_calls"
}

@test "explore: container not running → error" {
    # Override docker mock to return 'stopped' for inspect
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "stopped" ;;
    *)       ;;
esac
exit 0
EOF
    run kiso_run instance explore mysession
    [ "$status" -ne 0 ]
    [[ "$output" == *"not running"* ]]
}
