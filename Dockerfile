FROM python:3.12-slim

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/kiso

# Install dependencies (cached layer — re-runs only when deps change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install project
COPY kiso/ kiso/
COPY cli/ cli/
RUN uv sync --frozen --no-dev

# Pre-install skills (optional — uncomment and provide config.toml):
# COPY config.toml /root/.kiso/config.toml
# RUN uv run kiso skill install search

ARG KISO_BUILD_HASH=dev
ENV KISO_BUILD_HASH=$KISO_BUILD_HASH

# Image marker for post-rebuild tool dep repair
RUN echo "$KISO_BUILD_HASH" > /opt/kiso/.image_id

EXPOSE 8333

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8333/health || exit 1

CMD ["uv", "run", "uvicorn", "kiso.main:app", "--host", "0.0.0.0", "--port", "8333"]
