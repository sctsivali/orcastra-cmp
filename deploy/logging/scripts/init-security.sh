#!/usr/bin/env bash
# FRESH INSTALL ONLY: initialise the security index of an empty cluster. Runs ON the host.
# Existing clusters keep their security index in the data volume; use apply-security.sh there.
#
# Builds the full config set: config/roles/rolesmapping from the repo, the image's defaults for
# the remaining types, and internal users admin + kibanaserver hashed from:
#   compose/.env                    OPENSEARCH_ADMIN_PASSWORD
#   secrets/kibanaserver_password
# Further accounts (fluentbit writers, audit_viewer) are added with the REST API afterwards.
set -euo pipefail

ROOT="${ORCA_LOGGING_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
IMAGE="$(grep -m1 -E '^\s+image: opensearchproject/opensearch:' "$ROOT/compose/docker-compose.yml" | awk '{print $2}')"
NETWORK="${OS_NETWORK:-orcastra-logging_opensearch-net}"
CERTS="$ROOT/certs"

admin_pw="$(grep -E '^OPENSEARCH_ADMIN_PASSWORD=' "$ROOT/compose/.env" | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/')"
kib_pw="$(cat "$ROOT/secrets/kibanaserver_password")"
[[ ${#admin_pw} -ge 16 && ${#kib_pw} -ge 16 ]] || { echo "ERROR: passwords must be >= 16 chars" >&2; exit 1; }

# Refuse to run against a cluster that already has a security index: -cd would replace every
# user and role in it.
code="$(docker run --rm --network "$NETWORK" -v "$CERTS/root-ca.pem:/ca.pem:ro" --entrypoint curl "$IMAGE" \
  -s -o /dev/null -w '%{http_code}' --cacert /ca.pem https://opensearch:9200/ || true)"
[[ "$code" == 503 ]] || { echo "ERROR: expected 503 (security not initialised), got $code. Use apply-security.sh." >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT; chmod 755 "$work"
hash() {  # password via stdin to the image's hash tool, so it never appears in argv
  docker run --rm -i --entrypoint bash "$IMAGE" -c \
    'read -r p; /usr/share/opensearch/plugins/opensearch-security/tools/hash.sh -p "$p" 2>/dev/null | tail -1' <<<"$1"
}
# Defaults for the types the repo does not manage.
docker run --rm -v "$work:/out" --user 0 --entrypoint bash "$IMAGE" -c \
  'cp /usr/share/opensearch/config/opensearch-security/{action_groups,tenants,nodes_dn,allowlist,audit}.yml /out/'
cp "$ROOT/opensearch/security/"{config,roles,roles_mapping}.yml "$work/"
cat > "$work/internal_users.yml" <<EOF
---
_meta:
  type: "internalusers"
  config_version: 2
admin:
  hash: "$(hash "$admin_pw")"
  reserved: true
  backend_roles: ["admin"]
  description: "Break-glass admin"
kibanaserver:
  hash: "$(hash "$kib_pw")"
  reserved: true
  description: "OpenSearch Dashboards server user"
EOF
chmod 644 "$work"/*.yml
grep -q '^  hash: "\$2' "$work/internal_users.yml" || { echo "ERROR: hashing failed" >&2; exit 1; }

docker run --rm --network "$NETWORK" \
  -v "$CERTS/root-ca.pem:/certs/root-ca.pem:ro" -v "$CERTS/admin.pem:/certs/admin.pem:ro" \
  -v "$CERTS/admin-key.pem:/certs/admin-key.pem:ro" -v "$work:/work" --user 0 \
  --entrypoint /usr/share/opensearch/plugins/opensearch-security/tools/securityadmin.sh "$IMAGE" \
  -h opensearch -p 9200 -icl -cacert /certs/root-ca.pem -cert /certs/admin.pem -key /certs/admin-key.pem -cd /work \
  | grep -E 'Done with success|ERR' | grep -v httpclient
echo "==> security index initialised; next: scripts/apply-ingest-config.sh, scripts/provision-stack.sh"
