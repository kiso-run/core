#!/usr/bin/env bash
set -euo pipefail

# ── kiso host wrapper ────────────────────────────────────────────────────────
# Thin proxy: intercepts Docker management commands, passes everything else
# to `kiso` inside the container via docker exec.

CONTAINER="kiso"
COMPOSE_FILE="$HOME/.kiso/docker-compose.yml"

compose_cmd() {
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        echo "Error: $COMPOSE_FILE not found. Run install.sh first." >&2
        exit 1
    fi
    docker compose -f "$COMPOSE_FILE" "$@"
}

require_running() {
    if ! docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null | grep -q running; then
        echo "Error: container '$CONTAINER' is not running. Try: kiso up" >&2
        exit 1
    fi
}

# TTY flags: only use -it when connected to a terminal
TTY_FLAGS=""
if [[ -t 0 && -t 1 ]]; then
    TTY_FLAGS="-it"
fi

# Pass terminal capabilities into the container
TERM_ENV=(-e "TERM=${TERM:-xterm}" -e "COLORTERM=${COLORTERM:-}" -e "LANG=${LANG:-C.UTF-8}")

case "${1:-}" in
    logs)
        shift
        docker logs -f "$CONTAINER" "$@"
        ;;
    up)
        compose_cmd up -d
        ;;
    down)
        compose_cmd down
        ;;
    restart)
        docker restart "$CONTAINER"
        ;;
    shell)
        require_running
        docker exec $TTY_FLAGS "${TERM_ENV[@]}" "$CONTAINER" bash
        ;;
    explore)
        require_running
        SESSION="${2:-$(hostname)@$(whoami)}"
        docker exec $TTY_FLAGS "${TERM_ENV[@]}" "$CONTAINER" \
            bash -c "cd ~/.kiso/sessions/$SESSION 2>/dev/null && exec bash || { echo 'Session workspace not found: ~/.kiso/sessions/$SESSION'; exit 1; }"
        ;;
    status)
        state=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "not found")
        printf 'Container: %s\n' "$state"
        if curl -sf http://localhost:8333/health &>/dev/null; then
            printf 'Health:    ok\n'
        else
            printf 'Health:    unreachable\n'
        fi
        ;;
    health)
        curl -sf http://localhost:8333/health && echo || { echo '{"status": "unreachable"}'; exit 1; }
        ;;
    help|--help|-h)
        cat <<'HELP'
kiso — host wrapper for the kiso container

Usage: kiso [command]

Chat:
  kiso                      interactive chat (REPL)
  kiso msg "text"           send a message, print the response, exit
  kiso <args>               pass arguments to kiso inside the container

Container management:
  kiso up                   start the container
  kiso down                 stop the container
  kiso restart              restart the container
  kiso status               show container state + health
  kiso health               hit the /health endpoint
  kiso logs                 follow container logs
  kiso shell                open a bash shell inside the container
  kiso explore [session]    open a shell in the session workspace
  kiso completion [bash|zsh] print shell completion script

Config:
  ~/.kiso/config.toml       main configuration
  ~/.kiso/.env              deploy secrets (API keys)
  ~/.kiso/docker-compose.yml runtime compose file

Run 'kiso help' inside the container for CLI commands:
  kiso skill list           list installed skills
  kiso skill search         search available skills
  kiso env set KEY VALUE    set a deploy secret
  kiso env reload           hot-reload secrets
HELP
        ;;
    completion)
        case "${2:-bash}" in
            bash) cat "$HOME/.local/share/bash-completion/completions/kiso" 2>/dev/null || echo "Completion not installed. Run install.sh." >&2 ;;
            zsh)  cat "$HOME/.local/share/zsh/site-functions/_kiso" 2>/dev/null || echo "Completion not installed. Run install.sh." >&2 ;;
            *)    echo "Usage: kiso completion [bash|zsh]" >&2 ;;
        esac
        ;;
    *)
        require_running
        docker exec $TTY_FLAGS "${TERM_ENV[@]}" -w /opt/kiso "$CONTAINER" uv run kiso --user "$(whoami)" --session "$(hostname)@$(whoami)" "$@"
        ;;
esac
