# Docker

Kiso runs in Docker. The container provides a controlled environment with Python, `uv`, and common tools. All user data lives in a volume that survives container restarts, rebuilds, and upgrades.

## Multi-instance architecture

Each instance is a named Docker container (`kiso-{name}`) with its own port, data directory, and isolated environment. The core Python image is the same for all instances — only configuration and data differ.

```
~/.kiso/
├── instances.json             # instance registry
└── instances/
    ├── jarvis/                # data for container kiso-jarvis
    │   ├── config.toml
    │   ├── .env
    │   ├── store.db
    │   ├── server.log
    │   ├── audit/
    │   ├── roles/
    │   ├── skills/
    │   ├── connectors/
    │   └── sessions/
    └── work/                  # data for container kiso-work
        ├── config.toml
        └── ...
```

**`instances.json`** is the registry of all instances and their ports:

```json
{
  "jarvis": { "server_port": 8333, "connector_port_base": 9000, "connectors": { "discord": 9001 } },
  "work":   { "server_port": 8334, "connector_port_base": 9100, "connectors": {} }
}
```

## Why native Docker (no compose at runtime)

Production instances are started with plain `docker run`, not docker-compose. This gives the wrapper precise control over port ranges — each instance gets an auto-detected server port and a dedicated connector port range, without the indirection of a compose file per instance. The `docker-compose.yml` in the repo is kept only as a development helper.

## Ports

Each instance gets two dedicated port ranges, auto-detected at install time:

| Range | Role |
|---|---|
| `8333+` | Server API — one port per instance (sequential) |
| `9001–9010`, `9101–9110`, … | Connector webhooks — 10-port block per instance, multiples of 100 |

The port block for connectors means: instance with base `9000` uses ports `9001–9010`; instance with base `9100` uses `9101–9110`; and so on.

**Internal = external port**: connector ports are the same inside and outside the container (no asymmetric NAT). A connector process listening on `9001` inside the container is reachable at `localhost:9001` on the host — no extra configuration needed.

Port registration in `docker run`:

```bash
docker run -d --name kiso-jarvis \
  -p 8333:8333 \
  -p 9001-9010:9001-9010 \
  -v ~/.kiso/instances/jarvis:/root/.kiso \
  --env-file ~/.kiso/instances/jarvis/.env \
  --restart unless-stopped \
  kiso:latest
```

## Installation

`install.sh` handles the full setup: prompts for configuration, builds the Docker image, starts the container, and registers the instance.

```bash
# First install
bash <(curl -fsSL https://raw.githubusercontent.com/kiso-run/core/main/install.sh)

# From the repo
./install.sh

# Non-interactive (CI / automated)
./install.sh --name jarvis --user marco --api-key sk-or-... --provider openrouter
```

Re-running `install.sh` on an existing installation offers two options:

- **Add a new instance** — runs through full configuration for a new bot name
- **Update Docker image** — rebuilds `kiso:latest` and restarts all running instances

## Instance management

All container lifecycle is managed via `kiso instance`:

```bash
kiso instance list                        # all instances: name, port, status
kiso instance create NAME                 # start a new container (config must exist)
kiso instance start [NAME]                # docker start kiso-{NAME}
kiso instance stop [NAME]                 # docker stop kiso-{NAME}
kiso instance restart [NAME]              # docker restart kiso-{NAME}
kiso instance status [NAME]               # container state + health endpoint
kiso instance logs [NAME] [-f]            # docker logs kiso-{NAME}
kiso instance shell [NAME]                # bash inside the container
kiso instance explore [SESSION]           # shell in the session workspace
kiso instance remove [NAME] [--yes]       # docker rm + rm -rf instances/{NAME}/
```

When only one instance is installed, the `[NAME]` argument is optional — the wrapper resolves it implicitly. With multiple instances, use `kiso --instance NAME` or pass `NAME` explicitly.

## Multi-instance CLI

All chat and management commands accept an optional `--instance NAME` (or `-i NAME`) flag:

```bash
kiso --instance jarvis                    # chat with the jarvis bot
kiso --instance work msg "deploy to prod"
kiso --instance jarvis skill install search
kiso --instance work connector install discord
```

With a single instance, `--instance` can be omitted everywhere.

## Instance names

Instance names are the bot's identifier: used for the Docker container name, data directory, and `--instance` flag. Rules:

- Lowercase alphanumeric + hyphens only: `^[a-z0-9][a-z0-9-]*$`
- No leading or trailing hyphens
- Max 32 characters

Valid: `kiso`, `jarvis`, `work-bot`, `bot2`. Invalid: `MyBot`, `-bot`, `bot_`, `bot name`.

## Dockerfile

The image is built once and shared by all instances. The `CMD` starts the HTTP server directly via uvicorn — no dependency on the Python CLI at container startup.

```dockerfile
FROM python:3.12-slim

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/kiso
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY kiso/ kiso/
COPY cli/ cli/
RUN uv sync --frozen --no-dev

EXPOSE 8333

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8333/health || exit 1

CMD ["uv", "run", "uvicorn", "kiso.server:app", "--host", "0.0.0.0", "--port", "8333"]
```

## Pre-installing skills and connectors

**Build time** (baked into image): immutable, updates require rebuild.

```dockerfile
FROM kiso:latest
RUN kiso skill install search
```

**Runtime** (in volume): mutable, updatable without rebuild.

```bash
kiso skill install search
```

Volume contents take precedence over build-time installs (Docker mount behavior).

## Environment variables

Two ways to provide deploy secrets:

**1. `kiso env`** (recommended): manages `~/.kiso/instances/{name}/.env`. Secrets persist across restarts and can be hot-reloaded via `kiso env reload`.

```bash
kiso env set KISO_LLM_API_KEY sk-or-...
kiso env reload
```

**2. `--env-file`** at container start: set at `docker run` time, requires container restart to update.

## Task persistence

Tasks in `store.db` (volume) survive container crashes. In-flight tasks are marked `failed` on next startup. Unprocessed messages (`processed=0`) are re-enqueued — see [flow.md — Message Recovery on Startup](flow.md#message-recovery-on-startup).

## deps.sh and system packages

`deps.sh` runs inside the container as root (isolated, no sudo needed, idempotent). System packages installed at runtime live in the container filesystem — lost on container recreation. Python packages (`uv sync` into `.venv`) persist in the volume.

See [skills.md — deps.sh](skills.md#depssh).
