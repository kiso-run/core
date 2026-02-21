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
USERNAME_RE='^[a-z_][a-z0-9_-]{0,31}$'

# ── Colors ────────────────────────────────────────────────────────────────────

red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

cleanup() {
    if [[ -n "$CLEANUP_DIR" && -d "$CLEANUP_DIR" ]]; then
        rm -rf "$CLEANUP_DIR"
    fi
    [[ -n "${ENV_BACKUP:-}" ]] && rm -f "$ENV_BACKUP"
    [[ -n "${CONFIG_BACKUP:-}" ]] && rm -f "$CONFIG_BACKUP"
}
trap cleanup EXIT

# ── Parse arguments ──────────────────────────────────────────────────────────

ARG_USER=""
ARG_API_KEY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            if [[ $# -lt 2 ]]; then red "Error: --user requires a value"; exit 1; fi
            ARG_USER="$2"; shift 2 ;;
        --api-key)
            if [[ $# -lt 2 ]]; then red "Error: --api-key requires a value"; exit 1; fi
            ARG_API_KEY="$2"; shift 2 ;;
        *)
            red "Unknown option: $1"; exit 1 ;;
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

ask_username() {
    local default_user kiso_user
    default_user="$(whoami)"
    if [[ -n "$ARG_USER" ]]; then
        if [[ ! "$ARG_USER" =~ $USERNAME_RE ]]; then
            red "Error: username '$ARG_USER' is invalid (must match $USERNAME_RE)"
            exit 1
        fi
        echo "$ARG_USER"
        return
    fi
    while true; do
        read -rp "Username [$default_user]: " kiso_user
        kiso_user="${kiso_user:-$default_user}"
        if [[ "$kiso_user" =~ $USERNAME_RE ]]; then
            echo "$kiso_user"
            return
        fi
        red "  Invalid: must be lowercase, start with a-z or _, max 32 chars."
    done
}

ask_bot_name() {
    local bot_name
    read -rp "Bot name [Kiso]: " bot_name
    bot_name="${bot_name:-Kiso}"
    echo "$bot_name"
}

ask_api_key() {
    if [[ -n "$ARG_API_KEY" ]]; then
        echo "API key: (provided via --api-key)" >&2
        echo "$ARG_API_KEY"
        return
    fi
    while true; do
        read -rsp "OpenRouter API key: " api_key
        echo >&2
        if [[ -n "$api_key" ]]; then
            echo "$api_key"
            return
        fi
        red "  API key cannot be empty. Try again."
    done
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
    yellow "  $CONFIG already exists. Current contents:"
    echo
    cat "$CONFIG"
    echo
    if ! confirm "  Overwrite config.toml?" "n"; then
        NEED_CONFIG=false
        green "  config.toml kept"
    fi
fi

if [[ -f "$ENV_FILE" ]]; then
    yellow "  $ENV_FILE already exists (contains API key — not shown)."
    if ! confirm "  Overwrite .env (API key)?" "n"; then
        NEED_ENV=false
        green "  .env kept"
    fi
else
    yellow "  $ENV_FILE not found — will ask for API key."
    yellow "  (API key is stored in .env, separate from config.toml)"
fi

NEED_BUILD=true

if docker inspect "$CONTAINER" &>/dev/null; then
    state=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)
    yellow "  Container '$CONTAINER' exists (state: $state)."
    if confirm "  Rebuild and restart?" "y"; then
        docker rm -f "$CONTAINER" &>/dev/null || true
        green "  old container removed"
    else
        NEED_BUILD=false
        green "  container kept"
    fi
fi

# ── 3b. Back up files that should survive ──────────────────────────────────

# Belt-and-suspenders: back up .env and config.toml before Docker operations
# so we can restore them if anything (stale VOLUME metadata, etc.) wipes them.
ENV_BACKUP=""
CONFIG_BACKUP=""
if [[ "$NEED_ENV" == false && -f "$ENV_FILE" ]]; then
    ENV_BACKUP="$(mktemp)"
    cp "$ENV_FILE" "$ENV_BACKUP"
fi
if [[ "$NEED_CONFIG" == false && -f "$CONFIG" ]]; then
    CONFIG_BACKUP="$(mktemp)"
    cp "$CONFIG" "$CONFIG_BACKUP"
fi

# ── 4. Configure ─────────────────────────────────────────────────────────────

mkdir -p "$KISO_DIR"

if [[ "$NEED_CONFIG" == true ]]; then
    kiso_user="$(ask_username)"
    echo "  username: $kiso_user"

    bot_name="$(ask_bot_name)"
    echo "  bot name: $bot_name"

    token="$(generate_token)"

    bold "Config preview:"
    cat <<EOF
[tokens]
cli = "$token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.$kiso_user]
role = "admin"

[settings]
bot_name = "$bot_name"
EOF

    confirm "Write this config to $CONFIG?"

    cat > "$CONFIG" <<EOF
[tokens]
cli = "$token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.$kiso_user]
role = "admin"

[settings]
bot_name = "$bot_name"
EOF
    green "  config.toml created"
fi

if [[ "$NEED_ENV" == true ]]; then
    api_key="$(ask_api_key)"

    bold "Creating $ENV_FILE..."
    # Use printf to avoid shell expansion of special chars in API key
    printf 'KISO_LLM_API_KEY=%s\n' "$api_key" > "$ENV_FILE"
    green "  .env created"

    # Verify the env var is loadable
    set -a; source "$ENV_FILE"; set +a
    if [[ -z "${KISO_LLM_API_KEY:-}" ]]; then
        yellow "  warning: KISO_LLM_API_KEY is empty after loading .env"
    fi
fi

# ── 5. Build and start ──────────────────────────────────────────────────────

if [[ "$NEED_BUILD" == true ]]; then
    # Remove dangling images that may carry stale VOLUME metadata from old builds
    docker image prune -f &>/dev/null || true

    bold "Building Docker image..."
    docker compose -f "$REPO_COMPOSE" build

    # Get the built image name (e.g. "core-kiso")
    IMAGE_NAME=$(docker compose -f "$REPO_COMPOSE" images --format json 2>/dev/null | grep -o '"Image":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
    if [[ -z "$IMAGE_NAME" ]]; then
        IMAGE_NAME="$(basename "$REPO_DIR")-kiso"
    fi
    green "  image: $IMAGE_NAME"

    # Write runtime compose (self-contained, no dependency on repo)
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
    env_file:
      - path: ${KISO_DIR}/.env
        required: false
    restart: unless-stopped
EOF
    green "  runtime compose created"

    bold "Starting container..."
    docker compose -f "$RUNTIME_COMPOSE" up -d

    bold "Waiting for healthcheck..."
    elapsed=0
    while [[ $elapsed -lt 30 ]]; do
        if curl -sf http://localhost:8333/health &>/dev/null; then
            echo
            green "  healthy!"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        printf '.'
    done

    if [[ $elapsed -ge 30 ]]; then
        echo
        yellow "  Healthcheck timed out (30s). Container may still be starting."
        yellow "  Check with: docker logs kiso"
    fi
else
    green "  Skipping build (container kept)."
fi

# ── 5b. Restore files if Docker wiped them ──────────────────────────────────

# Old Docker images (before commit 4caab64) had a VOLUME directive that could
# cause .env to disappear on rebuild.  Stale layers / anonymous volumes may
# still trigger this.  Restore from backup if needed.
if [[ -n "$ENV_BACKUP" && ! -f "$ENV_FILE" ]]; then
    yellow "  .env was unexpectedly removed during build — restoring from backup"
    cp "$ENV_BACKUP" "$ENV_FILE"
    green "  .env restored"
fi
if [[ -n "$CONFIG_BACKUP" && ! -f "$CONFIG" ]]; then
    yellow "  config.toml was unexpectedly removed during build — restoring from backup"
    cp "$CONFIG_BACKUP" "$CONFIG"
    green "  config.toml restored"
fi
[[ -n "$ENV_BACKUP" ]] && rm -f "$ENV_BACKUP"
[[ -n "$CONFIG_BACKUP" ]] && rm -f "$CONFIG_BACKUP"

# ── 6. Install wrapper ─────────────────────────────────────────────────────

bold "Installing kiso wrapper..."
mkdir -p "$(dirname "$WRAPPER_DST")"
cp "$WRAPPER_SRC" "$WRAPPER_DST"
chmod +x "$WRAPPER_DST"
green "  installed to $WRAPPER_DST"

# ── 6b. Install completions ──────────────────────────────────────────────
bold "Installing shell completions..."
BASH_COMP_DIR="$HOME/.local/share/bash-completion/completions"
ZSH_COMP_DIR="$HOME/.local/share/zsh/site-functions"

mkdir -p "$BASH_COMP_DIR" "$ZSH_COMP_DIR"
cp "$REPO_DIR/completions/kiso.bash" "$BASH_COMP_DIR/kiso"
cp "$REPO_DIR/completions/kiso.zsh"  "$ZSH_COMP_DIR/_kiso"
green "  completions installed"

# Hint for zsh fpath
if command -v zsh &>/dev/null; then
    if ! zsh -c 'echo "$fpath"' 2>/dev/null | grep -q "$HOME/.local/share/zsh/site-functions"; then
        yellow ""
        yellow "  For zsh completion, add this BEFORE compinit in your ~/.zshrc:"
        yellow ""
        yellow "    fpath=(\$HOME/.local/share/zsh/site-functions \$fpath)"
        yellow ""
    fi
fi

# Check PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    yellow ""
    yellow "  ~/.local/bin is not in your PATH."
    yellow "  Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    yellow ""
    yellow "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    yellow ""
fi

# ── 7. Summary ──────────────────────────────────────────────────────────────

echo
green "  kiso is running!"
echo
echo "  Quick start:"
echo "    kiso                    start chatting"
echo "    kiso msg \"hello\"        send a message, get a response"
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
