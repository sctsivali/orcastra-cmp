#!/usr/bin/env bash
# Build the OpenSearch Dashboards keystore from secret files. Runs ON the logging host.
#
# Inputs ($ORCA_LOGGING_DIR/secrets, all mode 600, never committed):
#   kibanaserver_password        password of the internal kibanaserver user
#   opensearch_oidc_client_secret  Authentik client secret (scripts/authentik-provision.py --secret-out)
#   cookie_password              generated here on first run (>= 32 chars, encrypts session cookies)
# Output: secrets/opensearch_dashboards.keystore (owned by the container user)
set -euo pipefail

ROOT="${ORCA_LOGGING_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
S="$ROOT/secrets"
IMAGE="$(grep -m1 -E '^\s+image: opensearchproject/opensearch-dashboards' "$ROOT/compose/docker-compose.yml" | awk '{print $2}')"
OS_UID=1000

umask 077
[[ -s "$S/cookie_password" ]] || { openssl rand -base64 48 | tr -d '\n' > "$S/cookie_password"; echo "generated cookie_password"; }
for f in kibanaserver_password opensearch_oidc_client_secret cookie_password; do
  [[ -s "$S/$f" ]] || { echo "ERROR: missing $S/$f" >&2; exit 1; }
done

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
cp "$S"/kibanaserver_password "$S"/opensearch_oidc_client_secret "$S"/cookie_password "$tmp/"
chown -R "$OS_UID:$OS_UID" "$tmp"; chmod 700 "$tmp"

# Values go in over stdin so they never appear in argv or the environment.
docker run --rm --network none -v "$tmp:/in" --entrypoint bash "$IMAGE" -c '
  set -euo pipefail
  ks=bin/opensearch-dashboards-keystore
  $ks create --silent >/dev/null
  $ks add --stdin --silent opensearch.password                       < /in/kibanaserver_password
  $ks add --stdin --silent opensearch_security.openid.client_secret  < /in/opensearch_oidc_client_secret
  $ks add --stdin --silent opensearch_security.cookie.password       < /in/cookie_password
  $ks list
  cp config/opensearch_dashboards.keystore /in/out.keystore'

install -o "$OS_UID" -g "$OS_UID" -m 600 "$tmp/out.keystore" "$S/opensearch_dashboards.keystore"
echo "wrote $S/opensearch_dashboards.keystore"
