# Docker

Kiso runs in Docker. The container provides a controlled environment with Python, `uv`, and common tools. All user data lives in a single volume.

## Volume

One volume, mounted at `~/.kiso/`:

```
~/.kiso/                    # single volume
├── config.toml
├── .env                    # deploy secrets (managed via `kiso env`)
├── store.db
├── server.log
├── audit/                  # LLM call logs, task execution logs (see audit.md)
├── roles/
├── skills/
├── connectors/
└── sessions/
```

Everything persists across container restarts: config, database, logs, installed skills/connectors, session data.

## Dockerfile

```dockerfile
FROM python:3.12-slim

# System tools
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install kiso
COPY . /opt/kiso
RUN cd /opt/kiso && uv sync

# Data directory
VOLUME /root/.kiso

EXPOSE 8333

HEALTHCHECK --interval=30s --timeout=5s \
  CMD curl -f http://localhost:8333/health || exit 1

CMD ["uv", "run", "kiso", "serve"]
```

## docker-compose.yml

```yaml
services:
  kiso:
    build: .
    container_name: kiso
    ports:
      - "8333:8333"
    volumes:
      - ${KISO_DATA:-~/.kiso}:/root/.kiso
    env_file:
      - path: .env
        required: false
    restart: unless-stopped
```

By default, `~/.kiso/` on the host is bind-mounted into the container. You can edit `config.toml` directly on the host and restart. Override with `KISO_DATA=/path/to/data docker compose up -d`.

## CLI Wrapper

`install.sh` installs a `kiso` wrapper in `~/.local/bin/` (source: `kiso-host.sh`). The wrapper intercepts Docker management commands and proxies everything else to `kiso` inside the container.

| Command | Runs |
|---|---|
| `kiso` | `docker exec -it kiso kiso` (chat REPL) |
| `kiso <args>` | `docker exec -it kiso kiso <args>` (skill, env, sessions, etc.) |
| `kiso logs` | `docker logs -f kiso` |
| `kiso up` | `docker compose up -d` |
| `kiso down` | `docker compose down` |
| `kiso restart` | `docker restart kiso` |
| `kiso shell` | `docker exec -it kiso bash` |
| `kiso status` | Container state + health check |
| `kiso health` | `curl http://localhost:8333/health` |

The wrapper reads the docker-compose.yml path from `~/.kiso/compose` (written by `install.sh`), so `kiso up` and `kiso down` work from any directory.

## Pre-installing Skills and Connectors

**Build time** (in Dockerfile): baked into image, immutable, updates require rebuild.

```dockerfile
FROM your-registry/kiso:latest
RUN kiso skill install search && kiso connector install discord
```

**Runtime** (in volume): mutable, updatable without rebuild.

```bash
kiso skill install search
```

Both can coexist — volume contents take precedence (Docker mount behavior).

## Ports

| Port | Service |
|---|---|
| `8333` | Kiso API (configurable via `config.toml`) |
| `9001+` | Connector webhooks (per-connector, configurable) |

Expose connector ports as needed:

```yaml
services:
  kiso:
    ports:
      - "8333:8333"
      - "9001:9001"   # discord connector webhook
```

## Environment Variables

Two ways to provide deploy secrets:

**1. `kiso env`** (recommended): manages `~/.kiso/.env` on the host (bind-mounted). Secrets persist across container restarts and can be hot-reloaded without restart via `kiso env reload`. See [cli.md — Deploy Secret Management](cli.md#deploy-secret-management).

```bash
kiso env set KISO_OPENROUTER_API_KEY sk-or-...
kiso env reload
```

**2. Docker env vars** (`docker run -e` or a `.env` file in the project directory): passed directly to the container process.

Both methods work. Docker env vars are applied at container start; `kiso env` manages secrets at runtime. If the same variable is set in both, the Docker env var takes precedence (standard behavior).

## deps.sh and System Packages

`deps.sh` runs inside the container as root (isolated, no sudo needed, idempotent). See [skills.md — deps.sh](skills.md#depssh).

**Important difference between build-time and runtime installs**: system packages installed at runtime live in the container filesystem (not the volume) — lost on container recreation. Python packages (`uv sync` into `.venv`) persist in the volume. For heavy system deps, prefer build-time installation.

## Task Persistence

Tasks in `store.db` (volume) survive container crashes. In-flight tasks marked `failed` on next startup. Unprocessed messages (`processed=0`) are re-enqueued on startup — see [flow.md — Message Recovery on Startup](flow.md#message-recovery-on-startup).
