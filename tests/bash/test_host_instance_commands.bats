#!/usr/bin/env bats
load 'helpers'

setup() {
    setup_kiso_env
    create_instances jarvis 8333 9000
}

@test "instance start NAME: calls docker start kiso-NAME" {
    run kiso_run instance start jarvis
    [ "$status" -eq 0 ]
    docker_was_called "start kiso-jarvis"
}

@test "instance stop NAME: calls docker stop kiso-NAME" {
    run kiso_run instance stop jarvis
    [ "$status" -eq 0 ]
    docker_was_called "stop kiso-jarvis"
}

@test "instance restart NAME: calls docker restart kiso-NAME" {
    run kiso_run instance restart jarvis
    [ "$status" -eq 0 ]
    docker_was_called "restart kiso-jarvis"
}

@test "instance list: shows instance name and server port" {
    run kiso_run instance list
    [ "$status" -eq 0 ]
    [[ "$output" == *"jarvis"* ]]
    [[ "$output" == *"8333"* ]]
}

@test "instance list: no instances shows empty message" {
    rm -f "$KISO_DIR/instances.json"
    run kiso_run instance list
    [ "$status" -eq 0 ]
    [[ "$output" == *"No instances configured"* ]]
}

@test "instance logs NAME: calls docker logs kiso-NAME" {
    run kiso_run instance logs jarvis
    [ "$status" -eq 0 ]
    docker_was_called "logs kiso-jarvis"
}

@test "instance logs NAME -f: passes -f flag to docker logs" {
    run kiso_run instance logs jarvis -f
    [ "$status" -eq 0 ]
    docker_was_called "logs kiso-jarvis -f"
}

@test "instance logs -f (no NAME): implicit instance, passes -f flag" {
    run kiso_run instance logs -f
    [ "$status" -eq 0 ]
    docker_was_called "logs kiso-jarvis -f"
}

@test "instance create: container fails to start → instances.json not written" {
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "exited" ;;
    info)    echo "" ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
    run kiso_run instance create newbot
    [ "$status" -ne 0 ]
    [[ "$output" == *"failed to start"* ]]
    ! instance_exists newbot
}

@test "instance create: docker run fails → exits with error" {
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    run)     exit 1 ;;
    info)    echo "" ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
    run kiso_run instance create newbot
    [ "$status" -ne 0 ]
    ! instance_exists newbot
}

@test "instance create: health check fails → existing instances preserved" {
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "exited" ;;
    info)    echo "" ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
    run kiso_run instance create newbot
    [ "$status" -ne 0 ]
    instance_exists jarvis
    ! instance_exists newbot
}
