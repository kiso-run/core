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
        docker exec -it "$CONTAINER" bash
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
    *)
        docker exec -it "$CONTAINER" kiso "$@"
        ;;
esac
