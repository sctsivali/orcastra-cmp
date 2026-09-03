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
  local escaped

  if [ -n "$value" ]; then
    # important: escape the replacement before handing it to sed. On the right-hand side of
    # s|||, a bare & expands to the whole match (silent corruption) and a bare | ends the
    # command, which makes sed exit non-zero and set -e kill the container at startup. Both
    # are reachable from an ordinary value: an OIDC end-session URL carries
    # ?post_logout_redirect_uri=...&client_id=..., and an issue-tracker URL carries query
    # parameters of its own.
    escaped=$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')
    find /app/.next -type f \( -name '*.js' -o -name '*.json' \) -exec sed -i "s|${placeholder}|${escaped}|g" {} +
    echo "[entrypoint] Replaced ${placeholder} -> ${value}"
  else
    echo "[entrypoint] WARNING: ${placeholder} has no value set, keeping the built-in default"
  fi
}

echo "[entrypoint] Applying runtime environment variables..."

replace_env "http://NEXT_PUBLIC_API_URL_PLACEHOLDER" "$NEXT_PUBLIC_API_URL"
replace_env "http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER" "$NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL"

# The pattern here is the shipped default, not a placeholder, so an unset variable leaves a
# working link in place. Keep these three strings in step: this file, frontend/Dockerfile,
# and the defaults in src/components/sidebar/support-links.ts.
replace_env "https://github.com/sctsivali/orcastra-cmp/issues/new" "$NEXT_PUBLIC_BUG_REPORT_URL"
replace_env "https://discourse.orca.id" "$NEXT_PUBLIC_COMMUNITY_URL"

# Ensure the API proxy route handler can reach the backend via Docker-internal URL.
# Default to http://backend:4050 (Docker Compose service name) if not explicitly set.
export INTERNAL_BACKEND_URL="${INTERNAL_BACKEND_URL:-http://backend:4050}"
echo "[entrypoint] INTERNAL_BACKEND_URL=${INTERNAL_BACKEND_URL}"

echo "[entrypoint] Starting Next.js server..."
exec node server.js
