# Backend Dockerfile for Orcastra Dashboard API
# note: pinned by digest to the base 1.0.0-RC4-hotfix1 was built on, so the OS layers do not drift.
FROM python:3.12-slim@sha256:97490e383c4cffb12825431fa24e3d2b70e39fd691a8e33c46bf4c18edca3998 as base

# Build arguments for version management
ARG APP_VERSION=1.0.0-RC2
# Provenance. APP_VERSION is a mutable label, the commit is the truth about what is running.
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

# Copy and install dependencies (this layer is cached unless requirements.txt or constraints.txt changes)
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt

# Final stage
FROM base as runtime
WORKDIR /app

# Create non-root user first
RUN useradd --create-home --shell /bin/bash appuser

# Create config directory and set ownership (for node_metadata.json etc.)
RUN mkdir -p /app/config && chown appuser:appuser /app/config

# Copy installed dependencies from builder and set ownership
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy Alembic migrations + config so the entrypoint can run `alembic upgrade head`
# on start (DB schema stays in lockstep with the code on every deploy).
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

# Provenance on the artifact as well as in the app, so the running image can be identified
# from `docker image inspect` even without reaching the API.
LABEL org.opencontainers.image.title="orcastra-dashboard-backend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# Expose port
EXPOSE 4050

# Health check.
# The probe MUST carry its own timeout. Relying on Docker's --timeout to SIGKILL a hung probe
# makes "the probe wedged" and "the app wedged" indistinguishable. 3s sits below the 5s Docker
# timeout so the probe always self-terminates first and reports a real failure.
# start-period covers `alembic upgrade head` plus Vault and LXD reconnect on a cold start; the
# previous 5s marked a normally-booting container unhealthy.
# Values are kept identical to docker-compose.prod.yml on purpose. Compose wins at runtime,
# this is what a bare `docker run` of the image gets.
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4050/health', timeout=3).status == 200 else 1)" || exit 1

# Worker count comes from WEB_CONCURRENCY (uvicorn's documented fallback for --workers) so it
# can be tuned from compose or .env during an incident without a rebuild and registry round
# trip. The default preserves the previous behaviour exactly.
ENV WEB_CONCURRENCY=4

# Entrypoint applies migrations, then runs the CMD (the app server)
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Run the application.
# --timeout-graceful-shutdown: uvicorn otherwise waits indefinitely for open connections, and
#   the long-lived WebSocket surfaces (terminal, console, monitoring, events) never close on
#   their own, so every deploy became a hard SIGKILL that cut live sessions mid-command.
# --limit-concurrency: memory guard. Beyond this uvicorn returns 503 instead of queueing
#   without bound. Sized high because WebSocket connections occupy the same budget.
#   note: this does NOT rescue a wedged worker, a blocked loop cannot return a 503 either.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4050", \
     "--timeout-graceful-shutdown", "20", \
     "--limit-concurrency", "200"]
