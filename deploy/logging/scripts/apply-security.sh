#!/usr/bin/env bash
# Apply the security plugin config from the repo (config, roles, rolesmapping). Runs ON the host.
#
# Internal users are deliberately NOT applied from files: they are managed through the REST API
# (provision-stack.sh, runbook) so password hashes never live in git, and applying a users file
# would wipe accounts created at runtime.
#
# Every run first backs up the complete live security index to /root/backups/<ts>/security.
# Rollback: scripts/apply-security.sh --restore /root/backups/<ts>/security
set -euo pipefail

ROOT="${ORCA_LOGGING_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
IMAGE="$(grep -m1 -E '^\s+image: opensearchproject/opensearch:' "$ROOT/compose/docker-compose.yml" | awk '{print $2}')"
NETWORK="${OS_NETWORK:-orcastra-logging_opensearch-net}"
CERTS="${OS_ADMIN_CERTS:-$ROOT/certs}"
SEC="$ROOT/opensearch/security"

restore=""
[[ "${1:-}" == "--restore" ]] && restore="${2:?backup dir required}"

# securityadmin runs in a throwaway container on the compose network, with the admin key mounted
# only for the duration of the call. The admin key is never mounted into the long-running node.
secadmin() {
  docker run --rm --network "$NETWORK" \
    -v "$CERTS/root-ca.pem:/certs/root-ca.pem:ro" \
    -v "$CERTS/admin.pem:/certs/admin.pem:ro" \
    -v "$CERTS/admin-key.pem:/certs/admin-key.pem:ro" \
    -v "$1:/work" --user 0 --entrypoint /usr/share/opensearch/plugins/opensearch-security/tools/securityadmin.sh \
    "$IMAGE" -h opensearch -p 9200 -icl -cacert /certs/root-ca.pem -cert /certs/admin.pem -key /certs/admin-key.pem "${@:2}"
}

ts="$(date -u +%Y%m%d-%H%M%S)"
bk="/root/backups/$ts/security"; mkdir -p "$bk"; chmod 700 "/root/backups" "/root/backups/$ts"
echo "==> backup live security index to $bk"
secadmin "$bk" -backup /work >/dev/null 2>&1 || true
[[ -s "$bk/roles.yml" && -s "$bk/internal_users.yml" ]] || { echo "ERROR: backup incomplete, nothing applied" >&2; exit 1; }

if [[ -n "$restore" ]]; then
  echo "==> restoring full security config from $restore"
  out="$(secadmin "$restore" -cd /work 2>&1)"
  grep -q "Done with success" <<<"$out" || { echo "$out" >&2; echo "ERROR: restore failed" >&2; exit 1; }
  echo "==> restored"; exit 0
fi

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
cp "$SEC/config.yml" "$SEC/roles.yml" "$SEC/roles_mapping.yml" "$work/"
chmod 644 "$work"/*
for pair in config:config.yml roles:roles.yml rolesmapping:roles_mapping.yml; do
  t="${pair%%:*}"; f="${pair#*:}"
  echo "==> apply $t"
  out="$(secadmin "$work" -f "/work/$f" -t "$t")"
  # note: here-strings, not `echo | grep -q`: under pipefail an early-exiting grep -q makes the
  # echo die of SIGPIPE and the success check reports failure.
  grep -E 'SUCC|ERR[^ ]* |FAIL|Done' <<<"$out" | grep -v 'httpclient-dispatch' || true
  grep -q "Done with success" <<<"$out" || { echo "ERROR: $t not applied; restore with --restore $bk" >&2; exit 1; }
done
echo "==> applied. backup: $bk"
