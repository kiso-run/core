# Docker

Kiso runs in Docker. The container provides a controlled environment with Python, `uv`, and common tools. All user data lives in a single volume.

## Volume

One volume, mounted at `~/.kiso/`:

```
~/.kiso/                    # single volume
├── config.toml
├── store.db
├── server.log
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
    ports:
      - "8333:8333"
    volumes:
      - kiso-data:/root/.kiso
    environment:
      - KISO_OPENROUTER_API_KEY
      # connector env vars
      - KISO_CONNECTOR_DISCORD_BOT_TOKEN
      # skill env vars
      - KISO_SKILL_SEARCH_API_KEY
    restart: unless-stopped

volumes:
  kiso-data:
```

## Pre-installing Skills and Connectors

Skills and connectors can be installed in two ways:

### At build time (in Dockerfile)

Baked into the image. Reproducible, immutable.

```dockerfile
FROM your-registry/kiso:latest    # your own built image

# Pre-install official skills
RUN kiso skill install search
RUN kiso skill install aider

# Pre-install connector
RUN kiso connector install discord
```

Build a base image first (`docker compose build`), then extend it. These become part of the image. Updates require a rebuild.

### At runtime (in volume)

Installed via CLI into the mounted volume. Mutable, manageable without rebuild.

```bash
docker exec -it kiso kiso skill install search
docker exec -it kiso kiso skill update search
```

These persist in the volume. Updates are immediate.

### Both

Build-time installs provide a base. Runtime installs add or override. Since both write to `~/.kiso/`, volume contents take precedence over image contents (Docker mount behavior).

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

Pass all required env vars through docker-compose or `docker run -e`:

```bash
docker run -d \
  -v kiso-data:/root/.kiso \
  -p 8333:8333 \
  -e KISO_OPENROUTER_API_KEY=sk-or-... \
  -e KISO_CONNECTOR_DISCORD_BOT_TOKEN=... \
  kiso-run/core
```

Or use an `.env` file:

```bash
# .env
KISO_OPENROUTER_API_KEY=sk-or-...
KISO_CONNECTOR_DISCORD_BOT_TOKEN=...
KISO_SKILL_SEARCH_API_KEY=...
```

```yaml
services:
  kiso:
    env_file: .env
```

## Health Check

`GET /health` returns `200 OK` when the server is running. Used by Docker's `HEALTHCHECK` directive.

## deps.sh and System Packages

When skills or connectors are installed (build-time or runtime), their `deps.sh` runs inside the container as root. This is safe because:

- The container is isolated from the host
- `apt install` works without sudo
- The script is idempotent (safe to re-run)

Build-time installs bake system deps into the image layer. Note: system packages installed by `deps.sh` at runtime live in the container filesystem, not in the volume — they are lost when the container is recreated. Python packages (installed via `uv sync` into `.venv`) persist in the volume and survive restarts.

**Recommendation**: for skills/connectors with heavy system deps, prefer build-time installation.

## Task Persistence

Tasks are stored in `store.db` (inside the volume). If the container crashes, completed tasks and their outputs are preserved. In-flight tasks are marked as `failed` on next startup.
