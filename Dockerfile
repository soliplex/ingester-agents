# Stage 1: Build
FROM redhat/ubi10-minimal AS builder

#COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /bin/
RUN microdnf install -y pip  && microdnf clean all
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12

WORKDIR /app
COPY uv.lock pyproject.toml /app/

RUN --mount=type=cache,target=/root/.cache/uv \
  pip install uv && uv sync --frozen --no-install-project --no-dev

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --frozen --no-dev

# Stage 2: Runtime
FROM redhat/ubi10-minimal

# OCI metadata labels
LABEL org.opencontainers.image.title="soliplex-agents" \
      org.opencontainers.image.description="Agents for use with soliplex-ingester" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.vendor="Enfold Systems" \
      org.opencontainers.image.authors="Enfold Systems <info@enfoldsystems.net>"

# Install Python runtime and shadow-utils for user management
RUN microdnf install -y python3.12 shadow-utils && microdnf clean all

# Create non-root user
RUN groupadd  -g 1000 -r soliplex && \
    useradd -u 1000 -r -g  soliplex -d /app -s /sbin/nologin soliplex

# Copy application with correct ownership
COPY --from=builder --chown=soliplex:soliplex /app /app

# Switch to non-root user
USER soliplex

# Configure PATH
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Document exposed port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail http://localhost:8001/health || exit 1 || exit 1


# Run application
CMD ["si-agent", "serve", "--host=0.0.0.0"]
