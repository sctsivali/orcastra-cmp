# Shared helpers for scripts that run ON the logging host.
# Sourced, not executed. Requires: bash, curl, python3.

set -euo pipefail

ORCA_LOGGING_DIR="${ORCA_LOGGING_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ORCA_LOGGING_ENV="${ORCA_LOGGING_ENV:-$ORCA_LOGGING_DIR/compose/.env}"
OS_URL="${OS_URL:-https://localhost:9200}"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }

[[ -r "$ORCA_LOGGING_ENV" ]] || die "env file not readable: $ORCA_LOGGING_ENV"
# note: read only the keys we need instead of sourcing, so a stray shell construct in .env is never executed
_env_get() { grep -E "^$1=" "$ORCA_LOGGING_ENV" | tail -1 | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/'; }
OS_ADMIN_USER="${OS_ADMIN_USER:-admin}"
OS_ADMIN_PASSWORD="${OS_ADMIN_PASSWORD:-$(_env_get OPENSEARCH_ADMIN_PASSWORD)}"
[[ -n "$OS_ADMIN_PASSWORD" ]] || die "OPENSEARCH_ADMIN_PASSWORD missing in $ORCA_LOGGING_ENV"

# Verify the node certificate when our CA is present; fall back to -k only before the CA rotation.
OS_CACERT="${OS_CACERT:-$ORCA_LOGGING_DIR/certs/root-ca.pem}"
if [[ -r "$OS_CACERT" ]]; then _TLS=(--cacert "$OS_CACERT"); else _TLS=(-k); fi

# os METHOD PATH [curl args...]  -> prints body, fails on HTTP >= 400
os() {
  local method="$1" path="$2"; shift 2
  curl -sS --fail-with-body -m "${OS_TIMEOUT:-60}" "${_TLS[@]}" \
    -u "$OS_ADMIN_USER:$OS_ADMIN_PASSWORD" -X "$method" "$OS_URL$path" "$@"
}
os_exists() {
  local code; code="$(curl -sS -o /dev/null -w '%{http_code}' -I -m 30 "${_TLS[@]}" -u "$OS_ADMIN_USER:$OS_ADMIN_PASSWORD" "$OS_URL$1")"
  [[ "$code" == 200 ]]
}
os_json() { local m="$1" p="$2" f="$3"; os "$m" "$p" -H 'content-type: application/json' --data-binary "@$f"; }

# jq is not installed on the host; python3 is.
pyj() { python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }
