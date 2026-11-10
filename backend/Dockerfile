# Backend Dockerfile for the Orcastra CMP API
# Pinned by digest so rebuilds are reproducible.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 as base

# Build arguments for version management
# Empty by default, so an unstamped build reports "unknown" instead of an old release.
ARG APP_VERSION=""
# APP_VERSION is a label; the commit identifies what is running.
ARG BUILD_DATE=""
ARG BUILD_COMMIT=""

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_VERSION=${APP_VERSION} \
    BUILD_DATE=${BUILD_DATE} \
    BUILD_COMMIT=${BUILD_COMMIT}

# Install system dependencies in a separate layer
FROM base as builder
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencies (cached unless requirements.txt or constraints.txt changes)
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt

# Final stage
FROM base as runtime
WORKDIR /app

# Non-root user.
#
# important: the uid is pinned. The uploads directory is a host bind mount
# (/var/orcastra/uploads in docker-compose.prod.yml) that Docker creates root-owned when missing,
# so operators must chown it to this exact uid.
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser

# Directories the app writes to, owned by appuser. /app/uploads is included for when the bind
# mount is absent; otherwise the first upload PATCH fails with a 500 and strands a session slot.
RUN mkdir -p /app/config /app/uploads \
    && chown appuser:appuser /app/config /app/uploads

# Copy installed dependencies from builder and set ownership
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Alembic migrations and config, applied by the entrypoint on every start.
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser alembic ./alembic

# Copy application code (this layer changes frequently)
COPY --chown=appuser:appuser app/ ./app/

# Entrypoint runs migrations then execs the CMD
COPY --chown=appuser:appuser docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Switch to non-root user
USER appuser

# Make sure pip packages are in PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# Image labels, so `docker image inspect` identifies the build without reaching the API.
LABEL org.opencontainers.image.title="orcastra-dashboard-backend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# Expose port
EXPOSE 4050

# Health check, identical to docker-compose.prod.yml (compose overrides it at runtime).
# The probe's own 3s timeout sits below Docker's 5s, so a hung probe reports a real failure
# instead of being killed. start-period covers migrations plus Vault and LXD reconnects.
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4050/health', timeout=3).status == 200 else 1)" || exit 1

# Worker count from WEB_CONCURRENCY (uvicorn's fallback for --workers), tunable without a rebuild.
ENV WEB_CONCURRENCY=4

# Entrypoint applies migrations, then runs the CMD (the app server)
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Run the application.
# --timeout-graceful-shutdown: long-lived WebSockets (terminal, console, monitoring, events)
#   never close on their own, so without a bound every deploy ends in SIGKILL.
# --limit-concurrency: memory guard; beyond it uvicorn answers 503 instead of queueing. Sized
#   high because WebSockets share the budget. It cannot help a worker whose loop is blocked.
# --ws-per-message-deflate false: SPICE frames are already compressed, and zlib on every frame
#   would run on the event loop and add console latency for no gain.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4050", \
     "--timeout-graceful-shutdown", "20", \
     "--limit-concurrency", "200", \
     "--ws-per-message-deflate", "false"]
