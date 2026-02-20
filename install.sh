#!/usr/bin/env bash
set -euo pipefail

# ── Kiso installer ────────────────────────────────────────────────────────────
# Works two ways:
#   1. bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
#   2. git clone ... && cd core && ./install.sh
#
# When run via curl, clones the repo to a temp dir, builds, cleans up.
# When run from the repo, uses the repo directly.

KISO_REPO="https://github.com/kiso-run/core.git"
KISO_DIR="$HOME/.kiso"
CONFIG="$KISO_DIR/config.toml"
ENV_FILE="$KISO_DIR/.env"
RUNTIME_COMPOSE="$KISO_DIR/docker-compose.yml"
WRAPPER_DST="$HOME/.local/bin/kiso"
CONTAINER="kiso"
CLEANUP_DIR=""

# ── Colors ────────────────────────────────────────────────────────────────────

red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

cleanup() {
    if [[ -n "$CLEANUP_DIR" && -d "$CLEANUP_DIR" ]]; then
        rm -rf "$CLEANUP_DIR"
    fi
}
trap cleanup EXIT

# ── Parse arguments ──────────────────────────────────────────────────────────

ARG_USER=""
ARG_API_KEY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)     ARG_USER="$2";    shift 2 ;;
        --api-key)  ARG_API_KEY="$2"; shift 2 ;;
        *)          red "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

confirm() {
    local prompt="$1" default="${2:-y}"
    if [[ "$default" == "y" ]]; then
        read -rp "$prompt [Y/n] " ans
        [[ -z "$ans" || "$ans" =~ ^[Yy] ]]
    else
        read -rp "$prompt [y/N] " ans
        [[ "$ans" =~ ^[Yy] ]]
    fi
}

generate_token() {
    if command -v openssl &>/dev/null; then
        openssl rand -hex 32
    elif command -v python3 &>/dev/null; then
        python3 -c "import secrets; print(secrets.token_hex(32))"
    else
        red "Error: need openssl or python3 to generate token"
        exit 1
    fi
}

# ── 1. Check prerequisites ───────────────────────────────────────────────────

bold "Checking prerequisites..."

if ! command -v docker &>/dev/null; then
    red "Error: docker is not installed. Install Docker first."
    exit 1
fi

if ! docker compose version &>/dev/null; then
    red "Error: docker compose is not available. Install Docker Compose v2."
    exit 1
fi

if ! command -v git &>/dev/null; then
    red "Error: git is not installed."
    exit 1
fi

green "  docker, docker compose, git found"

# ── 2. Locate or clone the repo ─────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/docker-compose.yml" && -f "$SCRIPT_DIR/Dockerfile" ]]; then
    REPO_DIR="$SCRIPT_DIR"
    bold "Using repo at $REPO_DIR"
else
    CLEANUP_DIR="$(mktemp -d)"
    REPO_DIR="$CLEANUP_DIR/core"
    bold "Cloning kiso..."
    git clone --depth 1 "$KISO_REPO" "$REPO_DIR"
    green "  cloned to temp dir"
fi

REPO_COMPOSE="$REPO_DIR/docker-compose.yml"
WRAPPER_SRC="$REPO_DIR/kiso-host.sh"

# ── 3. Check existing state ─────────────────────────────────────────────────

NEED_CONFIG=true
NEED_ENV=true

if [[ -f "$CONFIG" ]]; then
    yellow "  $CONFIG already exists."
    if ! confirm "  Overwrite config.toml?"; then
        NEED_CONFIG=false
        green "  config.toml kept"
    else
        rm -f "$CONFIG"
    fi
fi

if [[ -f "$ENV_FILE" ]]; then
    yellow "  $ENV_FILE already exists."
    if ! confirm "  Overwrite .env (API key)?"; then
        NEED_ENV=false
        green "  .env kept"
    else
        rm -f "$ENV_FILE"
    fi
fi

if docker inspect "$CONTAINER" &>/dev/null; then
    state=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)
    yellow "  Container '$CONTAINER' exists (state: $state)."
    if confirm "  Recreate it?" "y"; then
        docker compose -f "$REPO_COMPOSE" down 2>/dev/null || true
    fi
fi

# ── 4. Configure ─────────────────────────────────────────────────────────────

mkdir -p "$KISO_DIR"

# config.toml: needs username + token
if [[ "$NEED_CONFIG" == true ]]; then
    default_user="$(whoami)"
    if [[ -n "$ARG_USER" ]]; then
        kiso_user="$ARG_USER"
        echo "Username: $kiso_user"
    else
        read -rp "Username [$default_user]: " kiso_user
        kiso_user="${kiso_user:-$default_user}"
    fi

    token="$(generate_token)"

    bold "Creating $CONFIG..."
    cat > "$CONFIG" <<EOF
[tokens]
cli = "$token"

[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"

[users.$kiso_user]
role = "admin"
EOF
    green "  config.toml created"
fi

# .env: needs API key
if [[ "$NEED_ENV" == true ]]; then
    if [[ -n "$ARG_API_KEY" ]]; then
        api_key="$ARG_API_KEY"
        echo "API key: (provided via --api-key)"
    else
        read -rsp "OpenRouter API key: " api_key
        echo
    fi

    if [[ -z "$api_key" ]]; then
        red "Error: API key cannot be empty."
        exit 1
    fi

    bold "Creating $ENV_FILE..."
    cat > "$ENV_FILE" <<EOF
KISO_OPENROUTER_API_KEY=$api_key
EOF
    green "  .env created"
fi

# ── 9. Build image ───────────────────────────────────────────────────────────

bold "Building Docker image..."
docker compose -f "$REPO_COMPOSE" build

# Get the built image name (e.g. "core-kiso")
IMAGE_NAME=$(docker compose -f "$REPO_COMPOSE" images --format json | grep -o '"Image":"[^"]*"' | head -1 | cut -d'"' -f4)
if [[ -z "$IMAGE_NAME" ]]; then
    IMAGE_NAME="$(basename "$REPO_DIR")-kiso"
fi

# ── 10. Write runtime compose file ───────────────────────────────────────────
# Self-contained — no dependency on the repo directory after install.

bold "Writing $RUNTIME_COMPOSE..."
cat > "$RUNTIME_COMPOSE" <<EOF
services:
  kiso:
    image: $IMAGE_NAME
    container_name: kiso
    ports:
      - "8333:8333"
    volumes:
      - ${KISO_DIR}:/root/.kiso
    restart: unless-stopped
EOF
green "  runtime compose created (image: $IMAGE_NAME)"

# ── 11. Start container ─────────────────────────────────────────────────────

bold "Starting container..."
docker compose -f "$RUNTIME_COMPOSE" up -d

# ── 12. Wait for healthcheck ─────────────────────────────────────────────────

bold "Waiting for healthcheck..."
elapsed=0
while [[ $elapsed -lt 30 ]]; do
    if curl -sf http://localhost:8333/health &>/dev/null; then
        green "  healthy!"
        break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    printf '.'
done

if [[ $elapsed -ge 30 ]]; then
    yellow "  Healthcheck timed out (30s). Container may still be starting."
    yellow "  Check with: docker logs kiso"
fi

# ── 13. Install wrapper ─────────────────────────────────────────────────────

bold "Installing kiso wrapper..."
mkdir -p "$(dirname "$WRAPPER_DST")"
cp "$WRAPPER_SRC" "$WRAPPER_DST"
chmod +x "$WRAPPER_DST"
green "  installed to $WRAPPER_DST"

# Check PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    yellow ""
    yellow "  ~/.local/bin is not in your PATH."
    yellow "  Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    yellow ""
    yellow "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    yellow ""
fi

# ── 14. Summary ──────────────────────────────────────────────────────────────

echo
green "  kiso is running!"
echo
echo "  Quick start:"
echo "    kiso                    start chatting"
echo "    kiso help               show all commands"
echo
echo "  Useful commands:"
echo "    kiso status             check if running + healthy"
echo "    kiso logs               follow container logs"
echo "    kiso restart            restart the container"
echo "    kiso down / kiso up     stop / start"
echo
echo "  Config:   $CONFIG"
echo "  API:      http://localhost:8333"
echo
