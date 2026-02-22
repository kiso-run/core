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
ARG_BASE_URL=""
ARG_PROVIDER=""
RESET_REQUESTED=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            if [[ $# -lt 2 ]]; then red "Error: --user requires a value"; exit 1; fi
            ARG_USER="$2"; shift 2 ;;
        --api-key)
            if [[ $# -lt 2 ]]; then red "Error: --api-key requires a value"; exit 1; fi
            ARG_API_KEY="$2"; shift 2 ;;
        --base-url)
            if [[ $# -lt 2 ]]; then red "Error: --base-url requires a value"; exit 1; fi
            ARG_BASE_URL="$2"; shift 2 ;;
        --provider)
            if [[ $# -lt 2 ]]; then red "Error: --provider requires a value"; exit 1; fi
            ARG_PROVIDER="$2"; shift 2 ;;
        --reset)
            RESET_REQUESTED=true; shift ;;
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

ask_provider_name() {
    if [[ -n "$ARG_PROVIDER" ]]; then
        echo "$ARG_PROVIDER"
        return
    fi
    local name
    read -rp "Provider name [openrouter]: " name
    echo "${name:-openrouter}"
}

ask_base_url() {
    if [[ -n "$ARG_BASE_URL" ]]; then
        echo "$ARG_BASE_URL"
        return
    fi
    local url
    read -rp "LLM provider URL [https://openrouter.ai/api/v1]: " url
    echo "${url:-https://openrouter.ai/api/v1}"
}

ask_api_key() {
    if [[ -n "$ARG_API_KEY" ]]; then
        echo "API key: (provided via --api-key)" >&2
        echo "$ARG_API_KEY"
        return
    fi
    while true; do
        read -rsp "LLM API key for $base_url: " api_key
        echo >&2
        if [[ -n "$api_key" ]]; then
            echo "$api_key"
            return
        fi
        red "  API key cannot be empty. Try again."
    done
}

ask_models() {
    local roles=(
        "planner|interprets requests, creates task plans|minimax/minimax-m2.5"
        "reviewer|checks task output, decides replan|deepseek/deepseek-v3.2"
        "worker|translates tasks to shell commands|deepseek/deepseek-v3.2"
        "messenger|writes human-readable responses|deepseek/deepseek-v3.2"
        "searcher|web search (needs :online model)|google/gemini-2.5-flash-lite:online"
        "summarizer|compresses conversation history|deepseek/deepseek-v3.2"
        "curator|manages learned knowledge|deepseek/deepseek-v3.2"
        "paraphraser|prompt injection defense|deepseek/deepseek-v3.2"
    )

    # Non-interactive: use defaults
    if [[ -n "$ARG_USER" && -n "$ARG_API_KEY" ]]; then
        local result=""
        for entry in "${roles[@]}"; do
            IFS='|' read -r role _ default <<< "$entry"
            result+="$role = \"$default\"\n"
        done
        printf '%b' "$result"
        return
    fi

    echo >&2
    bold "Models — press Enter to keep default:" >&2
    echo >&2
    local result=""
    for entry in "${roles[@]}"; do
        IFS='|' read -r role desc default <<< "$entry"
        printf '  \033[1m%-12s\033[0m  \033[0;90m%s\033[0m\n' "$role" "$desc" >&2
        printf '               [\033[0;33m%s\033[0m]: ' "$default" >&2
        read -r choice
        choice="${choice:-$default}"
        result+="$role = \"$choice\"\n"
        echo >&2
    done
    printf '%b' "$result"
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

if ! docker info &>/dev/null; then
    red "Error: cannot connect to Docker daemon."
    red "  Either Docker is not running, or your user needs permission."
    red "  Fix with: sudo usermod -aG docker \$USER  (then log out and back in)"
    red "  Or for this shell only: newgrp docker"
    exit 1
fi

if ! command -v git &>/dev/null; then
    red "Error: git is not installed."
    exit 1
fi

green "  docker, docker compose, git found"
echo

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

bold "Checking existing installation..."

NEED_CONFIG=true
NEED_ENV=true

if [[ -f "$CONFIG" ]]; then
    yellow "  $CONFIG already exists. Current contents:"
    echo
    printf '\033[0;36m'
    cat "$CONFIG"
    printf '\033[0m'
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
echo

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

# ── 3c. Clean root-owned files ────────────────────────────────────────────

# The container runs as root, so files it creates (store.db, session.log,
# workspace files, sys/, reference/) are owned by root and cannot be deleted
# by the host user.  Use a throwaway container to remove them.
if [[ "$NEED_BUILD" == true && -d "$KISO_DIR" ]]; then
    # Check if any root-owned files exist
    if find "$KISO_DIR" -not -user "$(id -u)" -print -quit 2>/dev/null | grep -q .; then
        bold "Cleaning root-owned files from previous install..."
        docker run --rm -v "${KISO_DIR}:/mnt/kiso" alpine sh -c '
            # Remove directories that contain root-owned runtime data
            for d in sessions audit sys reference skills/*/; do
                rm -rf "/mnt/kiso/$d" 2>/dev/null
            done
            # Remove root-owned files (but never config.toml, .env, docker-compose.yml)
            rm -f /mnt/kiso/store.db /mnt/kiso/server.log /mnt/kiso/.chat_history 2>/dev/null
            # Fix ownership on remaining files so host user can manage them
            chown -R '"$(id -u):$(id -g)"' /mnt/kiso/ 2>/dev/null
        ' && green "  cleaned" || yellow "  warning: could not clean all root-owned files"
    fi
fi

# ── 4. Configure ─────────────────────────────────────────────────────────────

bold "Configuring..."
mkdir -p "$KISO_DIR"

if [[ "$NEED_CONFIG" == true ]]; then
    kiso_user="$(ask_username)"
    echo "  username: $kiso_user"

    bot_name="$(ask_bot_name)"
    echo "  bot name: $bot_name"

    provider_name="$(ask_provider_name)"
    echo "  provider: $provider_name"

    base_url="$(ask_base_url)"
    echo "  base url: $base_url"

    token="$(generate_token)"

    models_section="$(ask_models)"

    bold "Config preview:"
    cat <<EOF
[tokens]
cli = "$token"

[providers.$provider_name]
base_url = "$base_url"

[users.$kiso_user]
role = "admin"

[settings]
bot_name = "$bot_name"

[models]
$(printf '%b' "$models_section")
EOF

    confirm "Write this config to $CONFIG?"

    cat > "$CONFIG" <<CONF
[tokens]
cli = "$token"

[providers.$provider_name]
base_url = "$base_url"

[users.$kiso_user]
role = "admin"

[settings]
bot_name = "$bot_name"

[models]
$(printf '%b' "$models_section")
CONF
    green "  config.toml created"
fi
echo

# Ensure base_url is set for the API key prompt (may not be set if config was kept)
base_url="${base_url:-https://openrouter.ai/api/v1}"

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
echo

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

# ── 5a. Factory reset if requested ──────────────────────────────────────────

if [[ "$RESET_REQUESTED" == true ]]; then
    bold "Running factory reset..."
    docker exec "$CONTAINER" uv run kiso reset factory --yes
    docker restart "$CONTAINER"
    green "  factory reset complete"
fi

# ── 5b. Restore files if Docker wiped them ──────────────────────────────────

# Old Docker images (before commit 4caab64) had a VOLUME directive that could
# cause .env to disappear on rebuild.  Stale layers / anonymous volumes may
# still trigger this.  Restore from backup if needed.
if [[ -n "$ENV_BACKUP" && ! -s "$ENV_FILE" ]]; then
    yellow "  .env was unexpectedly removed or emptied during build — restoring from backup"
    cp "$ENV_BACKUP" "$ENV_FILE"
    green "  .env restored"
fi
if [[ -n "$CONFIG_BACKUP" && ! -s "$CONFIG" ]]; then
    yellow "  config.toml was unexpectedly removed or emptied during build — restoring from backup"
    cp "$CONFIG_BACKUP" "$CONFIG"
    green "  config.toml restored"
fi
[[ -n "$ENV_BACKUP" ]] && rm -f "$ENV_BACKUP"
[[ -n "$CONFIG_BACKUP" ]] && rm -f "$CONFIG_BACKUP"

# ── 6. Install wrapper ─────────────────────────────────────────────────────
echo
bold "Installing kiso wrapper..."
mkdir -p "$(dirname "$WRAPPER_DST")"
cp "$WRAPPER_SRC" "$WRAPPER_DST"
chmod +x "$WRAPPER_DST"
green "  installed to $WRAPPER_DST"

# ── 6b. Install completions ──────────────────────────────────────────────
echo
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
