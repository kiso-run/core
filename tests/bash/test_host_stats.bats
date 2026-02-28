#!/usr/bin/env bats
load 'helpers'

setup() {
    setup_kiso_env
    create_instances jarvis 8333 9000

    # Override docker to also intercept 'exec' and print a fake stats line
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "running" ;;
    info)    echo "" ;;
    exec)
        # Fake kiso stats output when called via docker exec
        echo "Token usage — last 30 days  (by model)"
        echo ""
        echo "  model           calls   input   output"
        echo "  ───────────────────────────────────────"
        echo "  gemini-flash        5     100      50"
        echo "  ───────────────────────────────────────"
        echo "  total               5     100      50"
        ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
}

@test "kiso stats: calls docker exec on the instance" {
    run kiso_run stats
    [ "$status" -eq 0 ]
    docker_was_called "exec"
    [[ "$output" == *"Token usage"* ]]
}

@test "kiso stats --since: passes flag through to kiso stats" {
    run kiso_run stats --since 7
    [ "$status" -eq 0 ]
    docker_was_called "exec"
    # --since is passed through (in the docker exec call args)
    grep -q "\-\-since 7\|--since\|7" "$BATS_TEST_TMPDIR/docker_calls"
}

@test "kiso stats --all: shows header for each instance" {
    create_instances jarvis 8333 9000 mybot 8334 9100
    run kiso_run stats --all
    [ "$status" -eq 0 ]
    [[ "$output" == *"── jarvis ──"* ]]
    [[ "$output" == *"── mybot ──"* ]]
}

@test "kiso stats --all: instance not running shows message" {
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "stopped" ;;
    info)    echo "" ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
    run kiso_run stats --all
    [ "$status" -eq 0 ]
    [[ "$output" == *"not running"* ]]
}
