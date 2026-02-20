#!/usr/bin/env bash
set -euo pipefail

# ── Kiso installer ────────────────────────────────────────────────────────────
# Automates: prereq check → config → docker build → healthcheck → wrapper install

KISO_DIR="$HOME/.kiso"
CONFIG="$KISO_DIR/config.toml"
ENV_FILE="$KISO_DIR/.env"
COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"
COMPOSE_PATH_FILE="$KISO_DIR/compose"
WRAPPER_SRC="$(cd "$(dirname "$0")" && pwd)/kiso-host.sh"
WRAPPER_DST="$HOME/.local/bin/kiso"
CONTAINER="kiso"

# ── Colors ────────────────────────────────────────────────────────────────────

red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

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

green "  docker and docker compose found"

# ── 2. Check existing state ──────────────────────────────────────────────────

if [[ -f "$CONFIG" ]]; then
    yellow "  $CONFIG already exists."
    if ! confirm "  Overwrite?"; then
        bold "Keeping existing config."
    else
        rm -f "$CONFIG"
    fi
fi

if docker inspect "$CONTAINER" &>/dev/null; then
    state=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)
    yellow "  Container '$CONTAINER' exists (state: $state)."
    if confirm "  Recreate it?" "y"; then
        docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true
    fi
fi

# ── 3. Ask username ──────────────────────────────────────────────────────────

default_user="$(whoami)"
if [[ -n "$ARG_USER" ]]; then
    kiso_user="$ARG_USER"
    echo "Username: $kiso_user"
else
    read -rp "Username [$default_user]: " kiso_user
    kiso_user="${kiso_user:-$default_user}"
fi

# ── 4. Ask API key ───────────────────────────────────────────────────────────

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

# ── 5. Generate token ────────────────────────────────────────────────────────

token="$(generate_token)"

# ── 6. Create config ─────────────────────────────────────────────────────────

mkdir -p "$KISO_DIR"

if [[ ! -f "$CONFIG" ]]; then
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
else
    green "  config.toml kept (existing)"
fi

# ── 7. Create .env ───────────────────────────────────────────────────────────

bold "Creating $ENV_FILE..."
cat > "$ENV_FILE" <<EOF
KISO_OPENROUTER_API_KEY=$api_key
EOF
green "  .env created"

# ── 8. Save compose path ─────────────────────────────────────────────────────

printf '%s' "$COMPOSE_FILE" > "$COMPOSE_PATH_FILE"

# ── 9. Build and start ───────────────────────────────────────────────────────

bold "Building Docker image..."
docker compose -f "$COMPOSE_FILE" build

bold "Starting container..."
docker compose -f "$COMPOSE_FILE" up -d

# ── 10. Wait for healthcheck ─────────────────────────────────────────────────

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

# ── 11. Install wrapper ──────────────────────────────────────────────────────

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

# ── 12. Summary ──────────────────────────────────────────────────────────────

echo
green "  kiso is running!"
echo
echo "  Chat:     kiso"
echo "  API:      http://localhost:8333"
echo
echo "  Logs:     kiso logs"
echo "  Stop:     kiso down"
echo "  Restart:  kiso restart"
echo "  Config:   $CONFIG"
echo
