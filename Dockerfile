FROM python:3.12-slim

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        git curl file jq unzip zip tree procps \
        nodejs npm && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /opt/kiso

# Install dependencies — cached layer invalidates ONLY when uv.lock changes.
# A stub pyproject.toml avoids cache busting on version bumps or code changes.
COPY uv.lock ./
RUN printf '[project]\nname = "kiso"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n' > pyproject.toml && \
    uv sync --frozen --no-dev --no-install-project && \
    rm pyproject.toml

# Copy source and install project (real pyproject.toml needed for project install)
COPY pyproject.toml ./
COPY kiso/ kiso/
COPY cli/ cli/
RUN uv sync --frozen --no-dev

ARG KISO_BUILD_HASH=dev
ENV KISO_BUILD_HASH=$KISO_BUILD_HASH

# Image marker for post-rebuild tool dep repair
RUN echo "$KISO_BUILD_HASH" > /opt/kiso/.image_id

EXPOSE 8333

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8333/health || exit 1

CMD ["uv", "run", "uvicorn", "kiso.main:app", "--host", "0.0.0.0", "--port", "8333"]
