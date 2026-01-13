# Stage 1: Build
FROM python:3.13-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.13

WORKDIR /app
COPY uv.lock pyproject.toml /app/
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --frozen --no-install-project --no-dev

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --frozen --no-dev

# Stage 2: Runtime
FROM python:3.13-slim-trixie

# OCI metadata labels
LABEL org.opencontainers.image.title="soliplex-agents" \
      org.opencontainers.image.description="Agents for use with soliplex-ingester" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.vendor="Enfold Systems" \
      org.opencontainers.image.authors="Enfold Systems <info@enfoldsystems.net>"

# Create non-root user
RUN groupadd -r appuser && \
    useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copy application with correct ownership
COPY --from=builder --chown=appuser:appuser /app /app

# Switch to non-root user
USER appuser

# Configure PATH
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Document exposed port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

# Run application
CMD ["si-agent", "serve", "--host=0.0.0.0"]
