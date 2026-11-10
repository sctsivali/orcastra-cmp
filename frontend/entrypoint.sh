#!/bin/sh
# Next.js bakes NEXT_PUBLIC_* values in at build time. The image is built with known
# placeholders, and this swaps in the runtime values before the server starts.

set -e

# Replace a placeholder with an env var value in every JS and JSON file
replace_env() {
  local placeholder="$1"
  local value="$2"
  local escaped

  if [ -n "$value" ]; then
    # important: escape the replacement for sed. In s|||, a bare & expands to the whole match
    # and a bare | ends the command (set -e then kills the container). Ordinary URLs contain
    # both, ex; an OIDC end-session URL with ?post_logout_redirect_uri=...&client_id=...
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

# The pattern is the shipped default, so an unset variable leaves a working link. Keep it in
# step with frontend/Dockerfile and src/components/sidebar/support-links.ts.
replace_env "https://github.com/sctsivali/orcastra-cmp/issues/new" "$NEXT_PUBLIC_BUG_REPORT_URL"
replace_env "https://discourse.orca.id" "$NEXT_PUBLIC_COMMUNITY_URL"

# The API proxy route reaches the backend over the Docker network; defaults to the compose
# service name.
export INTERNAL_BACKEND_URL="${INTERNAL_BACKEND_URL:-http://backend:4050}"
echo "[entrypoint] INTERNAL_BACKEND_URL=${INTERNAL_BACKEND_URL}"

echo "[entrypoint] Starting Next.js server..."
exec node server.js
