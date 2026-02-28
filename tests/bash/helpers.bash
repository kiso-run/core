# ── Test helpers for kiso bash tests ─────────────────────────────────────────
KISO_HOST="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)/kiso-host.sh"
INSTALL_SH="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)/install.sh"

# Run kiso-host.sh merging stderr into stdout.
kiso_run() {
    "$KISO_HOST" "$@" 2>&1
}

# Source install.sh in library mode, then call the named function with args.
# Usage: install_func FUNCNAME [args...]
install_func() {
    local fn="$1"; shift
    bash -c "KISO_INSTALL_LIB=1 source \"$INSTALL_SH\" && \"$fn\" \"\$@\" 2>&1" _ "$@"
}

# Set up an isolated temp HOME with docker/ss/curl/git/openssl mocks.
# Call from each test file's setup().
setup_kiso_env() {
    export HOME="$BATS_TEST_TMPDIR"
    export KISO_DIR="$HOME/.kiso"
    mkdir -p "$KISO_DIR"
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"

    # docker spy: records every invocation to docker_calls, returns sensible defaults
    cat > "$BATS_TEST_TMPDIR/bin/docker" <<EOF
#!/bin/bash
echo "\$@" >> "$BATS_TEST_TMPDIR/docker_calls"
case "\$1" in
    inspect) echo "running" ;;
    info)    echo "" ;;
    *)       ;;
esac
exit 0
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/docker"

    # ss: no ports in use
    printf '#!/bin/bash\nexit 0\n' > "$BATS_TEST_TMPDIR/bin/ss"
    chmod +x "$BATS_TEST_TMPDIR/bin/ss"

    # curl: always succeeds
    printf '#!/bin/bash\nexit 0\n' > "$BATS_TEST_TMPDIR/bin/curl"
    chmod +x "$BATS_TEST_TMPDIR/bin/curl"

    # git: always succeeds
    printf '#!/bin/bash\nexit 0\n' > "$BATS_TEST_TMPDIR/bin/git"
    chmod +x "$BATS_TEST_TMPDIR/bin/git"

    # openssl: returns a fake 64-hex token
    printf '#!/bin/bash\necho "deadbeef1234567890abcdef1234567890abcdef1234567890abcdef12345678"\nexit 0\n' \
        > "$BATS_TEST_TMPDIR/bin/openssl"
    chmod +x "$BATS_TEST_TMPDIR/bin/openssl"
}

# Populate instances.json with one or more instances.
# Usage: create_instances NAME PORT BASE [NAME PORT BASE ...]
# With no args: creates empty instances.json ({}).
create_instances() {
    python3 - "$KISO_DIR/instances.json" "$@" <<'PY'
import sys, json, pathlib
path = pathlib.Path(sys.argv[1])
args = sys.argv[2:]
d = {}
for i in range(0, len(args), 3):
    name, port, base = args[i], int(args[i+1]), int(args[i+2])
    d[name] = {"server_port": port, "connector_port_base": base, "connectors": {}}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(d, indent=2) + "\n")
PY
}

# Returns 0 if NAME exists in instances.json, 1 otherwise.
instance_exists() {
    [[ -f "$KISO_DIR/instances.json" ]] || return 1
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if sys.argv[2] in d else 1)
" "$KISO_DIR/instances.json" "$1" 2>/dev/null
}

# Read a field from instances.json: json_field NAME FIELD
json_field() {
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get(sys.argv[2], {}).get(sys.argv[3], ''))
" "$KISO_DIR/instances.json" "$1" "$2" 2>/dev/null
}

# Returns 0 if docker was called with these args (substring match), 1 otherwise.
docker_was_called() {
    grep -qF "$*" "$BATS_TEST_TMPDIR/docker_calls" 2>/dev/null
}
