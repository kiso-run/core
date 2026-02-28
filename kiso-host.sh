#!/usr/bin/env bash
set -euo pipefail

# ── kiso host wrapper ────────────────────────────────────────────────────────
# Multi-instance wrapper: manages named Docker containers (kiso-{NAME}).
# Each instance is a separate bot with its own port, data dir, and Docker
# container. The core Python image is untouched.

KISO_DIR="$HOME/.kiso"
INSTANCES_JSON="$KISO_DIR/instances.json"

# ── name validation ──────────────────────────────────────────────────────────
# Names must be lowercase alphanumeric + hyphens, no leading/trailing hyphen.
# This is intentionally stricter than Docker's rules for DNS compatibility.

validate_name() {
    local name="$1"
    if [[ -z "$name" ]]; then
        echo "Error: instance name cannot be empty." >&2; exit 1
    fi
    if [[ ${#name} -gt 32 ]]; then
        echo "Error: instance name too long (max 32 chars)." >&2; exit 1
    fi
    if [[ ! "$name" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
        echo "Error: instance name must be lowercase alphanumeric + hyphens, no leading/trailing hyphen." >&2
        echo "  Valid: kiso, my-bot, bot2" >&2
        echo "  Invalid: MyBot, -bot, bot_, bot name" >&2
        exit 1
    fi
    if [[ "$name" == *- ]]; then
        echo "Error: instance name cannot end with a hyphen." >&2; exit 1
    fi
}

# ── instances.json helpers ───────────────────────────────────────────────────

_read_json() {
    [[ -f "$INSTANCES_JSON" ]] && cat "$INSTANCES_JSON" || echo "{}"
}

_instance_count() {
    _read_json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
}

_instance_field() {
    local name="$1" field="$2"
    _read_json | python3 -c "
import sys, json
d = json.load(sys.stdin)
if sys.argv[1] not in d:
    print('', end=''); sys.exit(1)
val = d[sys.argv[1]].get(sys.argv[2], '')
print(val)
" "$name" "$field"
}

instance_server_port() { _instance_field "$1" "server_port"; }
instance_connector_base() { _instance_field "$1" "connector_port_base"; }

# ── port auto-detection ──────────────────────────────────────────────────────

_next_free_server_port() {
    local port=8333
    local used
    used=$(_read_json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('\n'.join(str(v['server_port']) for v in d.values() if 'server_port' in v))
")
    while true; do
        if echo "$used" | grep -q "^${port}$"; then
            ((port++)); continue
        fi
        if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${port}$"; then
            ((port++)); continue
        fi
        break
    done
    echo "$port"
}

_next_free_connector_base() {
    local base=9000
    local used
    used=$(_read_json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('\n'.join(str(v.get('connector_port_base', 0)) for v in d.values()))
")
    while echo "$used" | grep -q "^${base}$"; do
        ((base += 100))
    done
    echo "$base"
}

# ── instance resolution ──────────────────────────────────────────────────────
# Outputs the resolved instance name, or exits with error.
# Pass explicit name as $1 (or empty string for implicit resolution).

resolve_instance() {
    local explicit="${1:-}"

    if [[ -n "$explicit" ]]; then
        validate_name "$explicit"
        if ! _read_json | python3 -c "
import sys, json
d = json.load(sys.stdin)
sys.exit(0 if sys.argv[1] in d else 1)
" "$explicit" 2>/dev/null; then
            echo "Error: instance '$explicit' not found." >&2
            echo "  Use: kiso instance list" >&2
            exit 1
        fi
        echo "$explicit"
        return
    fi

    if [[ ! -f "$INSTANCES_JSON" ]]; then
        echo "Error: no instances configured. Run: kiso instance create NAME" >&2
        exit 1
    fi

    local count
    count=$(_instance_count)
    case "$count" in
        0)
            echo "Error: no instances configured. Run: kiso instance create NAME" >&2
            exit 1
            ;;
        1)
            _read_json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(list(d.keys())[0])
"
            ;;
        *)
            echo "Error: multiple instances available. Specify: kiso --instance NAME" >&2
            echo "" >&2
            echo "Available instances:" >&2
            _read_json | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in d: print(f'  {k}')
" >&2
            exit 1
            ;;
    esac
}

# ── require container running ────────────────────────────────────────────────

require_running() {
    local container="$1"
    local status
    status=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo "not found")
    if [[ "$status" != "running" ]]; then
        local name="${container#kiso-}"
        echo "Error: container '$container' is not running (status: $status)." >&2
        echo "  Try: kiso instance start $name" >&2
        exit 1
    fi
}

# ── TTY / TERM env ───────────────────────────────────────────────────────────

TTY_FLAGS=""
[[ -t 0 && -t 1 ]] && TTY_FLAGS="-it"
TERM_ENV=(-e "TERM=${TERM:-xterm}" -e "COLORTERM=${COLORTERM:-}" -e "LANG=${LANG:-C.UTF-8}")

# ── connector port assignment (post-install hook) ────────────────────────────

_assign_connector_ports() {
    local inst="$1"
    local conn_dir="$KISO_DIR/instances/$inst/connectors"
    [[ -d "$conn_dir" ]] || return 0

    python3 - "$INSTANCES_JSON" "$inst" "$conn_dir" <<'PY'
import sys, json, pathlib, re

path, inst, conn_dir = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
d = json.loads(path.read_text()) if path.exists() else {}
inst_cfg = d.setdefault(inst, {})
base = inst_cfg.get("connector_port_base", 9000)
assigned = inst_cfg.setdefault("connectors", {})
used_ports = set(assigned.values())

def next_port():
    for i in range(1, 11):
        p = base + i
        if p not in used_ports:
            return p
    sys.exit(f"Error: no free connector ports in range {base+1}-{base+10}")

changed = False
for conn_path in sorted(conn_dir.iterdir()):
    if not conn_path.is_dir() or conn_path.name in assigned:
        continue
    port = next_port()
    used_ports.add(port)
    assigned[conn_path.name] = port
    changed = True
    print(f"Connector '{conn_path.name}' assigned port {port}")
    cfg = conn_path / "config.toml"
    if cfg.exists():
        content = cfg.read_text()
        if re.search(r'^webhook_port\s*=', content, re.MULTILINE):
            content = re.sub(r'^(webhook_port\s*=\s*).*$', f'\\g<1>{port}', content, flags=re.MULTILINE)
        else:
            content += f'\nwebhook_port = {port}\n'
        cfg.write_text(content)

if changed:
    path.write_text(json.dumps(d, indent=2) + "\n")
PY
}

# ── parse global --instance / -i ─────────────────────────────────────────────

EXPLICIT_INSTANCE=""
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance|-i)
            EXPLICIT_INSTANCE="${2:?Error: --instance requires a NAME}"
            shift 2
            ;;
        --instance=*)
            EXPLICIT_INSTANCE="${1#*=}"
            shift
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done
# Restore positional params (ARGS[@] may be empty)
if [[ ${#ARGS[@]} -gt 0 ]]; then
    set -- "${ARGS[@]}"
else
    set --
fi

# ── main dispatch ─────────────────────────────────────────────────────────────

case "${1:-}" in

    # ── instance management ──────────────────────────────────────────────────
    instance)
        shift
        case "${1:-}" in

            create)
                NAME="${2:-}"
                [[ -z "$NAME" ]] && { echo "Usage: kiso instance create NAME" >&2; exit 1; }
                validate_name "$NAME"

                if _read_json | python3 -c "
import sys,json; d=json.load(sys.stdin); sys.exit(0 if sys.argv[1] in d else 1)
" "$NAME" 2>/dev/null; then
                    echo "Error: instance '$NAME' already exists." >&2
                    echo "  Use: kiso instance list" >&2
                    exit 1
                fi

                SERVER_PORT=$(_next_free_server_port)
                CONN_BASE=$(_next_free_connector_base)
                CONTAINER="kiso-$NAME"
                INST_DIR="$KISO_DIR/instances/$NAME"

                mkdir -p "$INST_DIR"

                if [[ ! -f "$INST_DIR/config.toml" ]]; then
                    echo "Warning: $INST_DIR/config.toml not found." >&2
                    echo "  The container will start but the server may fail to initialize." >&2
                    echo "  Populate config.toml before starting, or run: kiso instance shell $NAME" >&2
                fi

                docker run -d \
                    --name "$CONTAINER" \
                    --restart unless-stopped \
                    -p "${SERVER_PORT}:8333" \
                    -p "$((CONN_BASE+1))-$((CONN_BASE+10)):$((CONN_BASE+1))-$((CONN_BASE+10))" \
                    -v "$INST_DIR:/root/.kiso" \
                    kiso:latest

                # Health check: wait up to 3s for the container to reach 'running'
                _healthy=0
                for _i in 1 2 3; do
                    _st=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "not found")
                    if [[ "$_st" == "running" ]]; then
                        _healthy=1
                        break
                    fi
                    sleep 1
                done
                if [[ "$_healthy" -eq 0 ]]; then
                    docker logs "$CONTAINER" --tail 20 >&2 || true
                    docker rm -f "$CONTAINER" 2>/dev/null || true
                    echo "Error: container '$CONTAINER' failed to start — see above." >&2
                    exit 1
                fi

                python3 - "$INSTANCES_JSON" "$NAME" "$SERVER_PORT" "$CONN_BASE" <<'PY'
import sys, json, pathlib
path = pathlib.Path(sys.argv[1])
name, sport, cbase = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
path.parent.mkdir(parents=True, exist_ok=True)
d = json.loads(path.read_text()) if path.exists() else {}
d[name] = {"server_port": sport, "connector_port_base": cbase, "connectors": {}}
path.write_text(json.dumps(d, indent=2) + "\n")
PY
                echo "Instance '$NAME' created."
                echo "  Server:         http://localhost:$SERVER_PORT"
                echo "  Connector range: $((CONN_BASE+1))-$((CONN_BASE+10))"
                echo "  Data dir:       $INST_DIR"
                ;;

            start)
                INST=$(resolve_instance "${2:-$EXPLICIT_INSTANCE}")
                docker start "kiso-$INST"
                ;;

            stop)
                INST=$(resolve_instance "${2:-$EXPLICIT_INSTANCE}")
                docker stop "kiso-$INST"
                ;;

            restart)
                INST=$(resolve_instance "${2:-$EXPLICIT_INSTANCE}")
                docker restart "kiso-$INST"
                ;;

            list)
                if [[ ! -f "$INSTANCES_JSON" ]] || [[ "$(_instance_count)" -eq 0 ]]; then
                    echo "No instances configured. Run: kiso instance create NAME"
                    exit 0
                fi
                python3 - "$INSTANCES_JSON" <<'PY'
import sys, json, subprocess
d = json.loads(open(sys.argv[1]).read())
print(f"{'NAME':<20} {'SERVER':>8} {'CONN BASE':>10} {'STATUS'}")
print("-" * 52)
for name, cfg in d.items():
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", f"kiso-{name}"],
            capture_output=True, text=True
        )
        status = r.stdout.strip() if r.returncode == 0 else "not found"
    except Exception:
        status = "unknown"
    print(f"{name:<20} {cfg.get('server_port','?'):>8} {cfg.get('connector_port_base','?'):>10} {status}")
PY
                ;;

            status)
                INST=$(resolve_instance "${2:-$EXPLICIT_INSTANCE}")
                SERVER_PORT=$(instance_server_port "$INST")
                state=$(docker inspect --format '{{.State.Status}}' "kiso-$INST" 2>/dev/null || echo "not found")
                printf 'Instance:  %s\n' "$INST"
                printf 'Container: %s\n' "$state"
                if curl -sf "http://localhost:$SERVER_PORT/health" &>/dev/null; then
                    printf 'Health:    ok (port %s)\n' "$SERVER_PORT"
                else
                    printf 'Health:    unreachable (port %s)\n' "$SERVER_PORT"
                fi
                ;;

            logs)
                # kiso instance logs [NAME] [docker-logs-flags...]
                NAME_OR_FLAG="${2:-}"
                if [[ -z "$NAME_OR_FLAG" || "$NAME_OR_FLAG" == -* ]]; then
                    INST=$(resolve_instance "$EXPLICIT_INSTANCE")
                    shift  # remove "logs"
                else
                    INST=$(resolve_instance "$NAME_OR_FLAG")
                    shift 2  # remove "logs" and NAME
                fi
                docker logs "kiso-$INST" "$@"
                ;;

            shell)
                INST=$(resolve_instance "${2:-$EXPLICIT_INSTANCE}")
                CONTAINER="kiso-$INST"
                require_running "$CONTAINER"
                docker exec $TTY_FLAGS "${TERM_ENV[@]}" "$CONTAINER" bash
                ;;

            explore)
                # kiso [--instance NAME] instance explore [SESSION]
                INST=$(resolve_instance "$EXPLICIT_INSTANCE")
                CONTAINER="kiso-$INST"
                require_running "$CONTAINER"
                if [[ -n "${2:-}" ]]; then
                    SESSION="$2"
                    SESSION_DIR="$KISO_DIR/instances/$INST/sessions/$SESSION"
                    if [[ ! -d "$SESSION_DIR" ]]; then
                        echo "Error: session '$SESSION' not found in instance '$INST'." >&2
                        exit 1
                    fi
                else
                    SESSION="$(hostname)@$(whoami)"
                fi
                docker exec $TTY_FLAGS "${TERM_ENV[@]}" -e "SESSION=$SESSION" "$CONTAINER" \
                    bash -c 'cd ~/.kiso/sessions/$SESSION 2>/dev/null && exec bash || { echo "Session workspace not found: ~/.kiso/sessions/$SESSION"; exit 1; }'
                ;;

            remove)
                INST=$(resolve_instance "${2:-$EXPLICIT_INSTANCE}")
                FORCE=""
                for arg in "${@:3}"; do
                    [[ "$arg" == "--yes" || "$arg" == "-y" ]] && FORCE="yes"
                done
                if [[ -z "$FORCE" ]]; then
                    read -r -p "Remove instance '$INST' and ALL its data? This cannot be undone. [y/N] " confirm
                    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
                fi
                docker stop "kiso-$INST" 2>/dev/null || true
                docker rm "kiso-$INST" 2>/dev/null || true
                rm -rf "$KISO_DIR/instances/$INST"
                python3 - "$INSTANCES_JSON" "$INST" <<'PY'
import sys, json, pathlib
path = pathlib.Path(sys.argv[1])
d = json.loads(path.read_text())
d.pop(sys.argv[2], None)
path.write_text(json.dumps(d, indent=2) + "\n")
PY
                echo "Instance '$INST' removed."
                ;;

            ""|--help|-h)
                cat <<'HELP'
Usage: kiso instance COMMAND [NAME] [OPTIONS]

Commands:
  create NAME              create and start a new bot instance
  start [NAME]             start a stopped instance
  stop [NAME]              stop a running instance
  restart [NAME]           restart an instance
  list                     show all instances with ports and status
  status [NAME]            container state + health check
  logs [NAME] [-f]         follow container logs
  shell [NAME]             open a bash shell inside the container
  explore [SESSION]        open a shell in the session workspace
  remove [NAME] [--yes]    remove instance and all its data

For explore, use --instance to specify the instance when multiple exist:
  kiso --instance NAME instance explore [SESSION]
HELP
                ;;

            *)
                echo "Unknown instance command: '${1:-}'" >&2
                echo "  Use: kiso instance --help" >&2
                exit 1
                ;;
        esac
        ;;

    # ── stats ────────────────────────────────────────────────────────────────
    stats)
        # Collect flags, detect --all
        ALL_INSTANCES=0
        STATS_FLAGS=()
        for _arg in "${@:2}"; do
            if [[ "$_arg" == "--all" ]]; then
                ALL_INSTANCES=1
            else
                STATS_FLAGS+=("$_arg")
            fi
        done

        if [[ "$ALL_INSTANCES" -eq 0 ]]; then
            INST=$(resolve_instance "$EXPLICIT_INSTANCE")
            CONTAINER="kiso-$INST"
            SERVER_PORT=$(instance_server_port "$INST")
            require_running "$CONTAINER"
            docker exec $TTY_FLAGS "${TERM_ENV[@]}" -w /opt/kiso "$CONTAINER" \
                uv run kiso --user "$(whoami)" --api "http://localhost:$SERVER_PORT" \
                stats "${STATS_FLAGS[@]+"${STATS_FLAGS[@]}"}"
        else
            # Aggregate stats for every instance
            _inst_list=$(python3 -c "
import json, pathlib, sys
p = pathlib.Path('$INSTANCES_JSON')
d = json.loads(p.read_text()) if p.exists() else {}
print('\n'.join(d.keys()))
" 2>/dev/null || true)
            if [[ -z "$_inst_list" ]]; then
                echo "No instances configured."
                exit 0
            fi
            while IFS= read -r _inst; do
                echo "── $_inst ──"
                _c="kiso-$_inst"
                _port=$(instance_server_port "$_inst")
                if docker inspect --format '{{.State.Status}}' "$_c" 2>/dev/null | grep -q "^running$"; then
                    docker exec $TTY_FLAGS "${TERM_ENV[@]}" -w /opt/kiso "$_c" \
                        uv run kiso --user "$(whoami)" --api "http://localhost:$_port" \
                        stats "${STATS_FLAGS[@]+"${STATS_FLAGS[@]}"}" 2>/dev/null \
                        || echo "  (error reading stats)"
                else
                    echo "  (instance not running)"
                fi
                echo
            done <<< "$_inst_list"
        fi
        ;;

    # ── reset (special: factory restart) ────────────────────────────────────
    reset)
        INST=$(resolve_instance "$EXPLICIT_INSTANCE")
        CONTAINER="kiso-$INST"
        SERVER_PORT=$(instance_server_port "$INST")
        require_running "$CONTAINER"
        docker exec $TTY_FLAGS "${TERM_ENV[@]}" -w /opt/kiso "$CONTAINER" \
            uv run kiso --user "$(whoami)" --api "http://localhost:$SERVER_PORT" "$@"
        if [[ "${2:-}" == "factory" ]]; then
            echo "Restarting container..."
            docker restart "$CONTAINER"
        fi
        ;;

    # ── completion ───────────────────────────────────────────────────────────
    completion)
        _COMP_SHELL="${2:-bash}"
        if [[ "$_COMP_SHELL" != "bash" && "$_COMP_SHELL" != "zsh" ]]; then
            echo "Usage: kiso completion [bash|zsh]" >&2; exit 1
        fi
        # Resolve instance to get the bundled script from inside the container
        _COMP_INST=$(resolve_instance "$EXPLICIT_INSTANCE" 2>/dev/null || true)
        if [[ -n "$_COMP_INST" ]]; then
            _COMP_CONTAINER="kiso-$_COMP_INST"
            _COMP_STATUS=$(docker inspect --format '{{.State.Status}}' "$_COMP_CONTAINER" 2>/dev/null || echo "not found")
            if [[ "$_COMP_STATUS" == "running" ]]; then
                docker exec "$_COMP_CONTAINER" uv run kiso completion "$_COMP_SHELL"
                exit 0
            fi
        fi
        # Fallback: read from system-installed location
        case "$_COMP_SHELL" in
            bash) cat "$HOME/.local/share/bash-completion/completions/kiso" 2>/dev/null \
                    || { echo "Error: completion not available (no running instance and not installed)." >&2; exit 1; } ;;
            zsh)  cat "$HOME/.local/share/zsh/site-functions/_kiso" 2>/dev/null \
                    || { echo "Error: completion not available (no running instance and not installed)." >&2; exit 1; } ;;
        esac
        ;;

    # ── help ─────────────────────────────────────────────────────────────────
    help|--help|-h)
        cat <<'HELP'
kiso — host wrapper for the kiso Docker instances

Usage: kiso [--instance NAME] [COMMAND] [OPTIONS]

Chat:
  kiso                           interactive chat (REPL)
  kiso msg "text"                send one message, print the response, exit

Options (for chat and msg):
  --instance NAME, -i NAME       instance to use (default: implicit if only one)
  --session NAME                 session name (default: {hostname}@{username})
  --user NAME                    username (default: system user)
  --quiet, -q                    only show message output

Skills & connectors:
  kiso skill list
  kiso skill search [query]
  kiso skill install <name>
  kiso skill update <name|all>
  kiso skill remove <name>
  kiso connector list | search | install | update | remove  (same pattern)
  kiso connector run <name>      start a connector daemon
  kiso connector stop <name>     stop a connector daemon
  kiso connector status <name>

Sessions & secrets:
  kiso sessions                  list your sessions
  kiso sessions --all            list all sessions (admin only)
  kiso env set KEY VALUE
  kiso env get KEY
  kiso env list
  kiso env delete KEY
  kiso env reload                hot-reload secrets into the server

Token usage (admin only):
  kiso stats                     token usage for the current instance (last 30 days)
  kiso stats --since N           last N days
  kiso stats --session NAME      filter by session
  kiso stats --by model|session|role  group by dimension (default: model)
  kiso stats --all               stats for every instance

Reset (admin only — requires confirmation):
  kiso reset session [name]      clear one session
  kiso reset knowledge           clear facts + learnings
  kiso reset all                 clear all sessions + knowledge + audit
  kiso reset factory             wipe everything (keeps config.toml + .env)

Instance management:
  kiso instance create NAME      create and start a new bot instance
  kiso instance start [NAME]     start a stopped instance
  kiso instance stop [NAME]      stop a running instance
  kiso instance restart [NAME]
  kiso instance list             show all instances with ports and status
  kiso instance status [NAME]    container state + health check
  kiso instance logs [NAME] [-f]
  kiso instance shell [NAME]     bash shell inside the container
  kiso instance explore [SESSION]  shell in the session workspace
  kiso instance remove [NAME] [--yes]

Config files:
  ~/.kiso/instances.json              instance registry
  ~/.kiso/instances/{name}/config.toml
  ~/.kiso/instances/{name}/.env
  ~/.kiso/instances/{name}/kiso.db

REPL slash commands:
  /help  /status  /sessions  /stats  /verbose-on  /verbose-off  /clear  /exit
HELP
        ;;

    # ── proxy (all other commands) ───────────────────────────────────────────
    *)
        INST=$(resolve_instance "$EXPLICIT_INSTANCE")
        CONTAINER="kiso-$INST"
        SERVER_PORT=$(instance_server_port "$INST")
        require_running "$CONTAINER"

        docker exec $TTY_FLAGS "${TERM_ENV[@]}" -w /opt/kiso "$CONTAINER" \
            uv run kiso --user "$(whoami)" --session "$(hostname)@$(whoami)" \
            --api "http://localhost:$SERVER_PORT" "$@"

        # Post-install hook: assign connector ports when a new connector was installed
        if [[ "${1:-}" == "connector" && "${2:-}" == "install" ]]; then
            _assign_connector_ports "$INST"
        fi
        ;;
esac
