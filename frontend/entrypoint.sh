#!/bin/sh
# Runtime environment variable replacement for Next.js standalone build
# Replaces build-time placeholders with actual env var values at container startup
#
# This solves the Next.js limitation where NEXT_PUBLIC_* vars are baked at build time.
# We build with known placeholders, then swap them for real values before starting.

set -e

# replace placeholder with env var value in all JS and JSON files
replace_env() {
  local placeholder="$1"
  local value="$2"

  if [ -n "$value" ]; then
    find /app/.next -type f \( -name '*.js' -o -name '*.json' \) -exec sed -i "s|${placeholder}|${value}|g" {} +
    echo "[entrypoint] Replaced ${placeholder} -> ${value}"
  else
    echo "[entrypoint] WARNING: ${placeholder} has no value set, skipping"
  fi
}

echo "[entrypoint] Applying runtime environment variables..."

replace_env "http://NEXT_PUBLIC_API_URL_PLACEHOLDER" "$NEXT_PUBLIC_API_URL"
replace_env "http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER" "$NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL"
# NEXT_PUBLIC_AUTH_MODE is baked at BUILD time (used in === comparisons that get
# constant-folded), so there is no placeholder to swap here.

# Ensure the API proxy route handler can reach the backend via Docker-internal URL.
# Default to http://backend:4050 (Docker Compose service name) if not explicitly set.
export INTERNAL_BACKEND_URL="${INTERNAL_BACKEND_URL:-http://backend:4050}"
echo "[entrypoint] INTERNAL_BACKEND_URL=${INTERNAL_BACKEND_URL}"

echo "[entrypoint] Starting Next.js server..."
exec node server.js
