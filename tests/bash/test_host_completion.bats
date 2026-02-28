#!/usr/bin/env bats
load 'helpers'

setup() {
    setup_kiso_env
    create_instances jarvis 8333 9000

    # Override docker to intercept 'exec' and return a fake completion script
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "running" ;;
    info)    echo "" ;;
    exec)
        # Fake kiso completion bash output
        echo "# bash completion for kiso"
        echo "_kiso() { echo hello; }"
        echo "complete -F _kiso kiso"
        ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
}

@test "kiso completion bash: calls docker exec on the instance" {
    run kiso_run completion bash
    [ "$status" -eq 0 ]
    docker_was_called "exec"
    [[ "$output" == *"complete -F"* ]]
}

@test "kiso completion zsh: calls docker exec on the instance" {
    # Override docker exec to return fake zsh script
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "running" ;;
    info)    echo "" ;;
    exec)
        echo "#compdef kiso"
        echo "_kiso() { echo hello; }"
        ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"
    run kiso_run completion zsh
    [ "$status" -eq 0 ]
    docker_was_called "exec"
    [[ "$output" == *"#compdef kiso"* ]]
}

@test "kiso completion: invalid shell shows usage error" {
    run kiso_run completion fish
    [ "$status" -ne 0 ]
    [[ "$output" == *"Usage"* ]]
}
