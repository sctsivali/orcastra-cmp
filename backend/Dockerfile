# Backend Dockerfile for Orcastra Dashboard API
FROM python:3.12-slim as base

# Build arguments for version management
ARG APP_VERSION=1.0.0-RC1

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_VERSION=${APP_VERSION}

# Install system dependencies in a separate layer
FROM base as builder
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies (this layer is cached unless requirements.txt changes)
COPY requirements.txt .
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

# Copy application code (this layer changes frequently)
COPY --chown=appuser:appuser app/ ./app/

# Switch to non-root user
USER appuser

# Make sure pip packages are in PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# Expose port
EXPOSE 4050

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4050/health')" || exit 1

# Run the application
# Multiple workers for concurrency; use --workers=1 for debugging
# --ws-per-message-deflate false: SPICE/terminal binary is already compressed, so
# WebSocket compression only wastes CPU and adds latency on the hot path.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4050", "--workers", "4", "--ws-per-message-deflate", "false"]
