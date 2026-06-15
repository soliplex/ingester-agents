# syntax=docker/dockerfile:1

# ---------- base: system packages and non-root user ----------
FROM python:3.13-slim-trixie AS base

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

# Git is needed at runtime for SCM CLI mode
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /bin/

RUN groupadd -g ${APP_GID} appuser && \
    useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash appuser

# ---------- builder: install dependencies and package ----------
FROM base AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.13

COPY uv.lock pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- development: full toolchain, expects bind mount ----------
FROM builder AS development

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

RUN chown -R appuser:appuser /app

USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"]

CMD ["si-agent", "serve", "--host=0.0.0.0", "--reload"]

# ---------- production: minimal, non-root, default target ----------
FROM base AS production

COPY --from=builder --chown=appuser:appuser /app /app

USER appuser

ENV PATH="/app/.venv/bin:$PATH"

LABEL org.opencontainers.image.title="soliplex-agents" \
      org.opencontainers.image.description="Agents for use with soliplex-ingester" \
      org.opencontainers.image.vendor="Enfold Systems" \
      org.opencontainers.image.authors="Enfold Systems <info@enfoldsystems.net>"

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"]

CMD ["si-agent", "serve", "--host=0.0.0.0"]
