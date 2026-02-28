#!/usr/bin/env bash
set -euo pipefail

# ── Kiso installer ────────────────────────────────────────────────────────────
# Works two ways:
#   1. bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)
#   2. git clone ... && cd core && ./install.sh
#
# When run via curl, clones the repo to a temp dir, builds, cleans up.
# When run from the repo, uses the repo directly.
#
# Each call creates or reconfigures one named instance. Multiple instances
# can be installed on the same machine; each gets its own Docker container,
# data directory, and port.

KISO_REPO="https://github.com/kiso-run/core.git"
KISO_DIR="$HOME/.kiso"
INSTANCES_JSON="$KISO_DIR/instances.json"
IMAGE="kiso:latest"
WRAPPER_DST="$HOME/.local/bin/kiso"
CLEANUP_DIR=""
USERNAME_RE='^[a-z_][a-z0-9_-]{0,31}$'
INSTANCE_NAME_RE='^[a-z0-9][a-z0-9-]*$'

# ── Colors ────────────────────────────────────────────────────────────────────

red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

# Print a colorized, boxed TOML preview (reads content from first argument)
print_config_preview() {
    local content="$1"
    local sep="────────────────────────────────────────────────────────"
    printf '\033[2m  %s\033[0m\n' "$sep"
    while IFS= read -r line; do
        if [[ "$line" =~ ^\[.+\] ]]; then
            printf '  \033[1;36m%s\033[0m\n' "$line"
        elif [[ "$line" == *" = "* ]]; then
            local k="${line%% = *}" v="${line#* = }"
            printf '  \033[2m%s\033[0m = %s\n' "$k" "$v"
        elif [[ -z "$line" ]]; then
            printf '\n'
        else
            printf '  %s\n' "$line"
        fi
    done <<< "$content"
    printf '\033[2m  %s\033[0m\n' "$sep"
}

cleanup() {
    if [[ -n "$CLEANUP_DIR" && -d "$CLEANUP_DIR" ]]; then
        rm -rf "$CLEANUP_DIR"
    fi
    [[ -n "${ENV_BACKUP:-}" ]] && rm -f "$ENV_BACKUP"
    [[ -n "${CONFIG_BACKUP:-}" ]] && rm -f "$CONFIG_BACKUP"
}
[[ "${KISO_INSTALL_LIB:-}" != "1" ]] && trap cleanup EXIT

# ── Parse arguments ──────────────────────────────────────────────────────────

ARG_NAME=""
ARG_USER=""
ARG_API_KEY=""
ARG_BASE_URL=""
ARG_PROVIDER=""
RESET_REQUESTED=false

if [[ "${KISO_INSTALL_LIB:-}" != "1" ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --name)
                if [[ $# -lt 2 ]]; then red "Error: --name requires a value"; exit 1; fi
                ARG_NAME="$2"; shift 2 ;;
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
fi

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

validate_instance_name() {
    local name="$1"
    if [[ -z "$name" ]]; then
        red "Error: instance name cannot be empty."; return 1
    fi
    if [[ ${#name} -gt 32 ]]; then
        red "Error: instance name too long (max 32 chars)."; return 1
    fi
    if [[ ! "$name" =~ $INSTANCE_NAME_RE ]]; then
        red "Error: instance name must be lowercase alphanumeric + hyphens, no leading/trailing hyphen."
        red "  Valid: kiso, my-bot, bot2"
        return 1
    fi
    if [[ "$name" == *- ]]; then
        red "Error: instance name cannot end with a hyphen."; return 1
    fi
    return 0
}

_instance_exists_in_json() {
    local name="$1"
    [[ -f "$INSTANCES_JSON" ]] && python3 -c "
import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if sys.argv[2] in d else 1)
" "$INSTANCES_JSON" "$name" 2>/dev/null
}

# Transform a human-readable bot name into a valid instance identifier.
# "My Jarvis!" → "my-jarvis", "Work Bot 2" → "work-bot-2"
_derive_instance_name() {
    local raw="${1,,}"                        # lowercase
    raw="${raw//[ _]/-}"                      # spaces/underscores → hyphens
    raw=$(printf '%s' "$raw" | tr -cd 'a-z0-9-')  # strip everything else
    # Collapse consecutive hyphens
    while [[ "$raw" == *--* ]]; do raw="${raw//--/-}"; done
    raw="${raw#-}"                            # strip leading hyphen
    raw="${raw%-}"                            # strip trailing hyphen
    raw="${raw:0:32}"                         # max 32 chars
    echo "${raw:-kiso}"                       # fallback if empty
}

# Ask for the bot display name first, then derive (and confirm) the instance identifier.
# Sets globals BOT_NAME and INST_NAME.
BOT_NAME=""
INST_NAME=""
ask_bot_and_instance_name() {
    # Non-interactive: --name was passed directly
    if [[ -n "$ARG_NAME" ]]; then
        if ! validate_instance_name "$ARG_NAME"; then exit 1; fi
        if _instance_exists_in_json "$ARG_NAME"; then
            red "Error: instance '$ARG_NAME' already exists."
            exit 1
        fi
        INST_NAME="$ARG_NAME"
        BOT_NAME="${ARG_NAME^}"   # capitalize as default display name
        return
    fi

    echo >&2
    bold "Bot name" >&2
    yellow "  What do you want to call your bot? This is the name it will use in conversation." >&2
    echo >&2

    local bot_name inst_name derived
    while true; do
        read -rp "  Bot name [Kiso]: " bot_name
        bot_name="${bot_name:-Kiso}"

        derived="$(_derive_instance_name "$bot_name")"

        echo >&2
        yellow "  Instance identifier (used for Docker, data dir, and CLI):" >&2
        while true; do
            read -rp "  Identifier [$derived]: " inst_name
            inst_name="${inst_name:-$derived}"
            inst_name="${inst_name,,}"
            if ! validate_instance_name "$inst_name" 2>&1 >&2; then continue; fi
            if _instance_exists_in_json "$inst_name"; then
                yellow "  '$inst_name' already exists. Choose a different name." >&2
                yellow "  (To reconfigure it: re-run install.sh and choose update)" >&2
                yellow "  (To remove it: kiso instance remove $inst_name)" >&2
                continue
            fi
            break
        done
        break
    done

    BOT_NAME="$bot_name"
    INST_NAME="$inst_name"
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
        "searcher|web search (native search)|perplexity/sonar"
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

# ── Port auto-detection ────────────────────────────────────────────────────────

next_free_server_port() {
    local port=8333
    local used=""
    [[ -f "$INSTANCES_JSON" ]] && used=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print('\n'.join(str(v['server_port']) for v in d.values() if 'server_port' in v))
" "$INSTANCES_JSON" 2>/dev/null || true)
    while true; do
        if echo "$used" | grep -q "^${port}$"; then ((port++)); continue; fi
        if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${port}$"; then ((port++)); continue; fi
        break
    done
    echo "$port"
}

next_free_connector_base() {
    local base=9000
    local used=""
    [[ -f "$INSTANCES_JSON" ]] && used=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print('\n'.join(str(v.get('connector_port_base',0)) for v in d.values()))
" "$INSTANCES_JSON" 2>/dev/null || true)
    while echo "$used" | grep -q "^${base}$"; do ((base+=100)); done
    echo "$base"
}

register_instance() {
    local name="$1" sport="$2" cbase="$3"
    mkdir -p "$KISO_DIR"
    python3 - "$INSTANCES_JSON" "$name" "$sport" "$cbase" <<'PY'
import sys, json, pathlib
path = pathlib.Path(sys.argv[1])
name, sport, cbase = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
path.parent.mkdir(parents=True, exist_ok=True)
d = json.loads(path.read_text()) if path.exists() else {}
d[name] = {"server_port": sport, "connector_port_base": cbase, "connectors": {}}
path.write_text(json.dumps(d, indent=2) + "\n")
PY
}

# ── Source-only mode (for testing) ──────────────────────────────────────────
# When KISO_INSTALL_LIB=1: functions are defined, main execution is skipped.
[[ "${KISO_INSTALL_LIB:-}" == "1" ]] && return 0

# ── 1. Check prerequisites ───────────────────────────────────────────────────

bold "Checking prerequisites..."

if ! command -v docker &>/dev/null; then
    red "Error: docker is not installed. Install Docker first."
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

green "  docker, git found"
echo

# ── 2. Locate or clone the repo ─────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/Dockerfile" ]]; then
    REPO_DIR="$SCRIPT_DIR"
    bold "Using repo at $REPO_DIR"
else
    CLEANUP_DIR="$(mktemp -d)"
    REPO_DIR="$CLEANUP_DIR/core"
    bold "Cloning kiso..."
    git clone --depth 1 "$KISO_REPO" "$REPO_DIR"
    green "  cloned to temp dir"
fi

WRAPPER_SRC="$REPO_DIR/kiso-host.sh"

# ── 3. Check for existing installations ─────────────────────────────────────

bold "Checking existing installation..."

# Detect old single-instance layout (config.toml directly in ~/.kiso/)
OLD_CONFIG="$KISO_DIR/config.toml"
if [[ -f "$OLD_CONFIG" && ! -f "$INSTANCES_JSON" ]]; then
    echo
    yellow "  Detected old single-instance layout (~/.kiso/config.toml)."
    yellow "  To migrate to multi-instance, run:"
    yellow "    mkdir -p ~/.kiso/instances/kiso"
    yellow "    mv ~/.kiso/config.toml ~/.kiso/instances/kiso/"
    yellow "    mv ~/.kiso/.env ~/.kiso/instances/kiso/ 2>/dev/null || true"
    yellow "    mv ~/.kiso/kiso.db ~/.kiso/instances/kiso/ 2>/dev/null || true"
    yellow "  Then re-run install.sh."
    echo
    if ! confirm "  Continue with fresh install (old config untouched)?"; then
        echo "Aborted. Migrate manually and re-run install.sh."
        exit 0
    fi
fi

# If existing instances, offer to add new or update image
EXISTING_COUNT=0
if [[ -f "$INSTANCES_JSON" ]]; then
    EXISTING_COUNT=$(python3 -c "import json; print(len(json.load(open('$INSTANCES_JSON'))))" 2>/dev/null || echo 0)
fi

MODE="new"  # "new" or "update-image"
if [[ "$EXISTING_COUNT" -gt 0 ]]; then
    echo
    yellow "  Found $EXISTING_COUNT existing instance(s):"
    python3 -c "
import json
d=json.load(open('$INSTANCES_JSON'))
for k,v in d.items():
    print(f'    {k}  →  port {v.get(\"server_port\",\"?\")}')
" 2>/dev/null || true
    echo
    echo "  Options:"
    echo "    1) Add a new instance"
    echo "    2) Update Docker image (rebuild + restart all instances)"
    read -rp "  Choice [1]: " MODE_CHOICE
    MODE_CHOICE="${MODE_CHOICE:-1}"
    if [[ "$MODE_CHOICE" == "2" ]]; then
        MODE="update-image"
    fi
fi

echo

# ── Update-image mode ────────────────────────────────────────────────────────

if [[ "$MODE" == "update-image" ]]; then
    bold "Rebuilding Docker image..."
    if ! docker build -t "$IMAGE" "$REPO_DIR"; then
        yellow "  Build failed — pruning build cache and retrying without cache..."
        docker builder prune -f &>/dev/null || true
        if ! docker build --no-cache -t "$IMAGE" "$REPO_DIR"; then
            red "Error: Docker build failed."; exit 1
        fi
    fi
    green "  image rebuilt: $IMAGE"

    bold "Restarting all instances..."
    if [[ -f "$INSTANCES_JSON" ]]; then
        python3 -c "
import json; d=json.load(open('$INSTANCES_JSON')); print('\n'.join(d.keys()))
" 2>/dev/null | while IFS= read -r name; do
            if docker inspect "kiso-$name" &>/dev/null; then
                echo "  Restarting kiso-$name..."
                docker restart "kiso-$name" || yellow "  Warning: could not restart kiso-$name"
            fi
        done
    fi
    green "  done. All instances updated."
    exit 0
fi

# ── New instance flow ────────────────────────────────────────────────────────

# ── 3b. Bot name + instance identifier ──────────────────────────────────────

ask_bot_and_instance_name
CONTAINER="kiso-$INST_NAME"
INST_DIR="$KISO_DIR/instances/$INST_NAME"
CONFIG="$INST_DIR/config.toml"
ENV_FILE="$INST_DIR/.env"

echo
bold "Instance: $INST_NAME"
echo "  Bot name:   $BOT_NAME"
echo "  Container:  $CONTAINER"
echo "  Data dir:   $INST_DIR"
echo

# ── 3c. Check per-instance existing state ───────────────────────────────────

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

# ── 3d. Back up files that should survive ───────────────────────────────────

ENV_BACKUP=""
CONFIG_BACKUP=""
if [[ -f "$ENV_FILE" ]]; then
    ENV_BACKUP="$(mktemp)"
    cp "$ENV_FILE" "$ENV_BACKUP"
fi
if [[ "$NEED_CONFIG" == false && -f "$CONFIG" ]]; then
    CONFIG_BACKUP="$(mktemp)"
    cp "$CONFIG" "$CONFIG_BACKUP"
fi

# ── 3e. Clean root-owned files in instance dir ───────────────────────────────

if [[ "$NEED_BUILD" == true && -d "$INST_DIR" ]]; then
    if find "$INST_DIR" -not -user "$(id -u)" -print -quit 2>/dev/null | grep -q .; then
        bold "Cleaning root-owned files from previous install..."
        docker run --rm -v "${INST_DIR}:/mnt/kiso" alpine sh -c '
            for d in sessions audit sys reference skills/*/; do
                rm -rf "/mnt/kiso/$d" 2>/dev/null
            done
            rm -f /mnt/kiso/kiso.db /mnt/kiso/server.log /mnt/kiso/.chat_history 2>/dev/null
            chown -R '"$(id -u):$(id -g)"' /mnt/kiso/ 2>/dev/null
        ' && green "  cleaned" || yellow "  warning: could not clean all root-owned files"
    fi
fi

# ── 4. Configure ─────────────────────────────────────────────────────────────

bold "Configuring..."
mkdir -p "$INST_DIR"

if [[ "$NEED_CONFIG" == true ]]; then
    kiso_user="$(ask_username)"
    echo "  username: $kiso_user"

    bot_name="$BOT_NAME"
    echo "  bot name: $bot_name"

    provider_name="$(ask_provider_name)"
    echo "  provider: $provider_name"

    base_url="$(ask_base_url)"
    echo "  base url: $base_url"

    token="$(generate_token)"

    models_section="$(ask_models)"

    config_body=$(cat <<PREVIEW
[tokens]
cli = "$token"

[providers.$provider_name]
base_url = "$base_url"

[users.$kiso_user]
role = "admin"

[settings]
bot_name                     = "$bot_name"

# Conversation
context_messages             = 7      # messages kept in context window
summarize_threshold          = 30     # messages before auto-summarize

# Memory / knowledge
knowledge_max_facts          = 50     # max stored facts per session
fact_decay_days              = 7
fact_decay_rate              = 0.1
fact_archive_threshold       = 0.3
fact_consolidation_min_ratio = 0.3

# Planning & execution
max_plan_tasks               = 20
max_replan_depth             = 3
max_validation_retries       = 3
max_worker_retries           = 1
exec_timeout                 = 120    # seconds
planner_timeout              = 120    # seconds (planner + messenger LLM calls)
max_output_size              = 1048576  # bytes (1 MB)
fast_path_enabled            = true   # skip planner for simple chat messages

# Limits
max_llm_calls_per_message    = 200
max_message_size             = 65536  # bytes
max_queue_size               = 50

# Server
host                         = "0.0.0.0"
port                         = 8333

# Worker
worker_idle_timeout          = 300    # seconds

# Webhooks
webhook_require_https        = true
webhook_secret               = ""
webhook_max_payload          = 1048576  # bytes
webhook_allow_list           = []

[models]
$(printf '%b' "$models_section")
PREVIEW
)

    echo
    printf '  \033[1mConfig preview\033[0m — \033[2m%s\033[0m\n' "$CONFIG"
    echo
    print_config_preview "$config_body"
    echo
    confirm "Write this config to $CONFIG?"

    printf '%s\n' "$config_body" > "$CONFIG"
    green "  config.toml created"
fi
echo

base_url="${base_url:-https://openrouter.ai/api/v1}"

if [[ "$NEED_ENV" == true ]]; then
    api_key="$(ask_api_key)"

    if [[ -f "$ENV_FILE" ]]; then
        bold "Updating $ENV_FILE..."
        tmpfile="$(mktemp)"
        grep -v '^KISO_LLM_API_KEY=' "$ENV_FILE" > "$tmpfile" || true
        printf 'KISO_LLM_API_KEY=%s\n' "$api_key" >> "$tmpfile"
        mv "$tmpfile" "$ENV_FILE"
        green "  .env updated (other entries preserved)"
    else
        bold "Creating $ENV_FILE..."
        printf 'KISO_LLM_API_KEY=%s\n' "$api_key" > "$ENV_FILE"
        green "  .env created"
    fi

    set -a; source "$ENV_FILE"; set +a
    if [[ -z "${KISO_LLM_API_KEY:-}" ]]; then
        yellow "  warning: KISO_LLM_API_KEY is empty after loading .env"
    fi

    if [[ -n "$ENV_BACKUP" ]]; then
        cp "$ENV_FILE" "$ENV_BACKUP"
    fi
fi
echo

# ── 5. Auto-detect ports ─────────────────────────────────────────────────────

SERVER_PORT="$(next_free_server_port)"
CONN_BASE="$(next_free_connector_base)"
bold "Ports"
echo "  Server:          $SERVER_PORT"
echo "  Connector range: $((CONN_BASE+1))-$((CONN_BASE+10))"
echo

# ── 6. Build and start ──────────────────────────────────────────────────────

if [[ "$NEED_BUILD" == true ]]; then
    docker image prune -f &>/dev/null || true

    bold "Building Docker image..."
    if ! docker build -t "$IMAGE" "$REPO_DIR"; then
        yellow "  Build failed — pruning build cache and retrying without cache..."
        docker builder prune -f &>/dev/null || true
        if ! docker build --no-cache -t "$IMAGE" "$REPO_DIR"; then
            echo
            red "Error: Docker build failed."
            red "  Try:"
            red "    docker builder prune --all -f"
            red "    docker system prune -f"
            red "    ./install.sh"
            red "  Or restart Docker: sudo systemctl restart docker"
            exit 1
        fi
    fi
    green "  image: $IMAGE"

    bold "Starting container $CONTAINER..."
    docker run -d \
        --name "$CONTAINER" \
        --restart unless-stopped \
        -p "${SERVER_PORT}:8333" \
        -p "$((CONN_BASE+1))-$((CONN_BASE+10)):$((CONN_BASE+1))-$((CONN_BASE+10))" \
        --env-file "$ENV_FILE" \
        -v "$INST_DIR:/root/.kiso" \
        "$IMAGE"

    bold "Waiting for healthcheck..."
    elapsed=0
    while [[ $elapsed -lt 30 ]]; do
        if curl -sf "http://localhost:$SERVER_PORT/health" &>/dev/null; then
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
        yellow "  Check with: kiso instance logs $INST_NAME"
    fi
else
    green "  Skipping build (container kept)."
    SERVER_PORT=$(python3 -c "
import json
d=json.load(open('$INSTANCES_JSON'))
print(d.get('$INST_NAME', {}).get('server_port', 8333))
" 2>/dev/null || echo 8333)
    CONN_BASE=$(python3 -c "
import json
d=json.load(open('$INSTANCES_JSON'))
print(d.get('$INST_NAME', {}).get('connector_port_base', 9000))
" 2>/dev/null || echo 9000)
fi

# ── 6a. Factory reset if requested ───────────────────────────────────────────

if [[ "$RESET_REQUESTED" == true ]]; then
    bold "Running factory reset..."
    docker exec "$CONTAINER" uv run kiso reset factory --yes
    docker restart "$CONTAINER"
    green "  factory reset complete"
fi

# ── 6b. Restore files if Docker wiped them ──────────────────────────────────

if [[ -n "$ENV_BACKUP" && ! -s "$ENV_FILE" ]]; then
    yellow "  .env was unexpectedly removed or emptied — restoring from backup"
    cp "$ENV_BACKUP" "$ENV_FILE"
    green "  .env restored"
fi
if [[ -n "$CONFIG_BACKUP" && ! -s "$CONFIG" ]]; then
    yellow "  config.toml was unexpectedly removed or emptied — restoring from backup"
    cp "$CONFIG_BACKUP" "$CONFIG"
    green "  config.toml restored"
fi
[[ -n "$ENV_BACKUP" ]] && rm -f "$ENV_BACKUP"
[[ -n "$CONFIG_BACKUP" ]] && rm -f "$CONFIG_BACKUP"

# ── 6c. Register instance ─────────────────────────────────────────────────────

register_instance "$INST_NAME" "$SERVER_PORT" "$CONN_BASE"
green "  registered in $INSTANCES_JSON"

# ── 7. Install wrapper ─────────────────────────────────────────────────────

echo
bold "Installing kiso wrapper..."
mkdir -p "$(dirname "$WRAPPER_DST")"
cp "$WRAPPER_SRC" "$WRAPPER_DST"
chmod +x "$WRAPPER_DST"
green "  installed to $WRAPPER_DST"

# ── 7b. Install completions ──────────────────────────────────────────────────

echo
bold "Installing shell completions..."
BASH_COMP_DIR="$HOME/.local/share/bash-completion/completions"
ZSH_COMP_DIR="$HOME/.local/share/zsh/site-functions"

mkdir -p "$BASH_COMP_DIR" "$ZSH_COMP_DIR"
cp "$REPO_DIR/completions/kiso.bash" "$BASH_COMP_DIR/kiso"
cp "$REPO_DIR/completions/kiso.zsh"  "$ZSH_COMP_DIR/_kiso"
green "  completions installed"

if command -v zsh &>/dev/null; then
    if ! zsh -c 'echo "$fpath"' 2>/dev/null | grep -q "$HOME/.local/share/zsh/site-functions"; then
        yellow ""
        yellow "  For zsh completion, add this BEFORE compinit in your ~/.zshrc:"
        yellow ""
        yellow "    fpath=(\$HOME/.local/share/zsh/site-functions \$fpath)"
        yellow ""
    fi
fi

PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
_needs_source=false
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    _needs_source=true
    if [[ -f "$HOME/.zshrc" ]]; then
        _profile="$HOME/.zshrc"
    else
        _profile="$HOME/.bashrc"
    fi
    if ! grep -qF "$PATH_LINE" "$_profile" 2>/dev/null; then
        printf '\n%s\n' "$PATH_LINE" >> "$_profile"
        green "  added ~/.local/bin to PATH in $_profile"
    fi
fi

# ── 8. Summary ──────────────────────────────────────────────────────────────

echo
green "  $INST_NAME is running!"
echo
echo "  Quick start:"
echo "    kiso                           start chatting"
[[ "$EXISTING_COUNT" -gt 0 ]] && \
echo "    kiso --instance $INST_NAME      (specify instance if multiple exist)"
echo "    kiso msg \"hello\"               send a message, get a response"
echo "    kiso help                      show all commands"
echo
echo "  Manage this instance:"
echo "    kiso instance status $INST_NAME"
echo "    kiso instance logs $INST_NAME"
echo "    kiso instance start/stop/restart $INST_NAME"
echo
echo "  Config:   $CONFIG"
echo "  API:      http://localhost:$SERVER_PORT"
echo "  Registry: $INSTANCES_JSON"
echo

if [[ "$_needs_source" == true ]]; then
    echo "  ┌─────────────────────────────────────────────────┐"
    bold "  │  Run this command to activate kiso:             │"
    bold "  │                                                 │"
    bold "  │    source $_profile"
    bold "  │                                                 │"
    echo "  │  (or open a new terminal)                       │"
    echo "  └─────────────────────────────────────────────────┘"
    echo
fi
