#!/bin/bash
# Orcastra Dashboard - production upgrade preflight
#
# Read-only. Run this ON the production VM, from the deployment directory (the one holding
# docker-compose.prod.yml and .env), BEFORE pulling new images. It changes nothing.
#
#   ./upgrade_preflight.sh --to 1.0.0-RC4 --new-compose ./docker-compose.prod.yml.new
#
# Exit codes:
#   0 = no blocking finding (advisories may still be present, read them)
#   1 = at least one blocking finding, do not upgrade until it is resolved
#   3 = invalid usage or a missing dependency
#
# note: `set -e` is deliberately absent. A preflight has to run every check and then report;
# aborting at the first probe that returns non-zero would hide every finding after it.
set -uo pipefail

RED='\033[0;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; BLU='\033[0;34m'; NC='\033[0m'
BLOCKERS=0
ADVISORIES=0

ok()      { echo -e "${GRN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YEL}[WARN]${NC} $*"; ADVISORIES=$((ADVISORIES + 1)); }
fail()    { echo -e "${RED}[FAIL]${NC} $*"; BLOCKERS=$((BLOCKERS + 1)); }
info()    { echo -e "${BLU}[INFO]${NC} $*"; }
section() { echo ""; echo -e "${BLU}=== $* ===${NC}"; }
need()    { command -v "$1" >/dev/null 2>&1 || { echo "missing dep: $1" >&2; exit 3; }; }

# important: bump this with each release that adds a migration. When the target image is
# already pulled the real head is read out of it instead, which is why the runbook has you
# pull and then run this a second time.
EXPECTED_HEAD_FALLBACK="015_session_audit_bigint"

CLUSTER_ID_RE='^[a-zA-Z0-9][a-zA-Z0-9._-]*$'
MIN_FREE_GB=10

TARGET_VERSION=""
NEW_COMPOSE=""
ENV_FILE=".env"
CUR_COMPOSE="docker-compose.prod.yml"
REGISTRY_REPO="svlct/orcastra-dashboard"

usage() {
    cat >&2 <<'EOF'
usage: upgrade_preflight.sh --to <version> [options]

  --to <version>          target APP_VERSION, for example 1.0.0-RC4   (required)
  --new-compose <path>    the compose file shipped with that release
  --env <path>            default .env
  --compose <path>        the compose file currently in use, default docker-compose.prod.yml
  --repo <namespace>      default svlct/orcastra-dashboard
EOF
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --to)          TARGET_VERSION="${2:-}"; shift 2 ;;
        --new-compose) NEW_COMPOSE="${2:-}";    shift 2 ;;
        --env)         ENV_FILE="${2:-}";       shift 2 ;;
        --compose)     CUR_COMPOSE="${2:-}";    shift 2 ;;
        --repo)        REGISTRY_REPO="${2:-}";  shift 2 ;;
        -h|--help)     usage ;;
        *)             echo "unknown option: $1" >&2; usage ;;
    esac
done
[ -n "$TARGET_VERSION" ] || usage

need docker
[ -f "$ENV_FILE" ] || { echo "no such env file: $ENV_FILE" >&2; exit 3; }

# note: greps rather than sourcing. Sourcing a .env executes whatever is inside it, and a
# read-only preflight must never run an operator's config file.
env_get() {
    sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n1 \
        | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

PREFIX="$(env_get CONTAINER_PREFIX)"; PREFIX="${PREFIX:-orcastra-dashboard}"
PG_CONTAINER="${PREFIX}-postgres"
BACKEND_CONTAINER="${PREFIX}-backend"
PG_USER="$(env_get POSTGRES_USER)"
PG_DB="$(env_get POSTGRES_DB)"
BACKEND_IMAGE="${REGISTRY_REPO}:backend-${TARGET_VERSION}"
FRONTEND_IMAGE="${REGISTRY_REPO}:frontend-${TARGET_VERSION}"

psql_q() { docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null; }
running() { docker ps --format '{{.Names}}' | grep -qx "$1"; }

echo "Orcastra Dashboard upgrade preflight"
echo "  target:  $TARGET_VERSION"
echo "  env:     $ENV_FILE"
echo "  compose: $CUR_COMPOSE"

# -----------------------------------------------------------------------------
section "Currently deployed"

if running "$BACKEND_CONTAINER"; then
    running_image="$(docker inspect --format '{{.Config.Image}}' "$BACKEND_CONTAINER" 2>/dev/null)"
    running_rev="$(docker image inspect "$running_image" \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null)"
    info "backend image:    ${running_image:-unknown}"
    if [ -n "$running_rev" ]; then
        info "backend revision: $running_rev"
    else
        warn "the running backend image carries no org.opencontainers.image.revision label"
        warn "  it predates provenance stamping, so which commit is live cannot be established"
    fi
else
    warn "container '$BACKEND_CONTAINER' is not running; treating this as a cold deployment"
fi

# -----------------------------------------------------------------------------
section "Database schema and the migration lock window"

if ! running "$PG_CONTAINER"; then
    fail "container '$PG_CONTAINER' is not running, cannot inspect the schema"
else
    stamp="$(psql_q 'SELECT version_num FROM alembic_version')"
    if [ -z "$stamp" ]; then
        has_tables="$(psql_q "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
        if [ "${has_tables:-0}" -gt 0 ]; then
            warn "database holds ${has_tables} tables and carries NO alembic stamp"
            warn "  db_bootstrap will try to recognise it as an RC2 baseline and stamp it."
            warn "  If it does not match exactly it exits non-zero and the container will not start."
            warn "  Take the backup before you find out which."
        else
            info "database is empty; migrations will build it from base"
        fi
    else
        info "current alembic_version: $stamp"
    fi

    expected="$EXPECTED_HEAD_FALLBACK"
    image_history=""
    if docker image inspect "$BACKEND_IMAGE" >/dev/null 2>&1; then
        from_image="$(docker run --rm --entrypoint alembic "$BACKEND_IMAGE" heads 2>/dev/null | awk 'NR==1 {print $1}')"
        [ -n "$from_image" ] && expected="$from_image"
        image_history="$(docker run --rm --entrypoint alembic "$BACKEND_IMAGE" history 2>/dev/null)"
        info "expected head, read from $BACKEND_IMAGE: $expected"
    else
        info "expected head, from this script's constant (target image not pulled yet): $expected"
        info "  pull the target image and run this again to read the head from the image itself"
    fi

    if [ -z "$stamp" ]; then
        : # already reported above
    elif [ "$stamp" = "$expected" ]; then
        ok "schema is already at the target head, no migration will run"
    elif [ -n "$image_history" ] && ! printf '%s' "$image_history" | grep -q "$stamp"; then
        # important: this is the crash-loop case, and it is the normal shape of a rollback.
        # alembic raises "Can't locate revision identified by '<stamp>'" when the database is
        # stamped with a revision the image does not ship, the entrypoint runs under set -e,
        # and the container dies before uvicorn ever starts. db_bootstrap does not rescue it:
        # it leaves an already-stamped database alone by design.
        fail "the database is stamped '$stamp', which does not exist in $BACKEND_IMAGE"
        fail "  This image is OLDER than the schema. Starting it produces a crash loop, not a"
        fail "  downgrade. Either deploy an image that contains $stamp, or downgrade the schema"
        fail "  first from an image that has it, or start this one with RUN_MIGRATIONS=false."
    else
        info "the entrypoint will advance $stamp -> $expected on first start"
    fi

    rows="$(psql_q 'SELECT count(*) FROM session_audit_logs')"
    size="$(psql_q "SELECT pg_size_pretty(pg_total_relation_size('session_audit_logs'))")"
    if [ -n "$rows" ]; then
        info "session_audit_logs: ${rows} rows, ${size}"
        # 015 widens three int columns to bigint, which rewrites the table under an
        # ACCESS EXCLUSIVE lock. Reads and writes to this table block for the rewrite.
        if [ "$rows" -gt 5000000 ]; then
            fail "the table is large enough that the 015 rewrite is a real outage window"
            fail "  plan a maintenance window, do not run this during business hours"
        elif [ "$rows" -gt 500000 ]; then
            warn "the 015 rewrite will take noticeable time on a table this size"
        else
            ok "the 015 rewrite window is negligible at this size"
        fi
    else
        info "session_audit_logs does not exist yet"
    fi
fi

# -----------------------------------------------------------------------------
section "Authentication configuration"

auth_enabled="$(env_get AUTH_ENABLED)"
issuer="$(env_get AUTHENTIK_ISSUER)"
auth_lc="$(printf '%s' "${auth_enabled:-true}" | tr 'A-Z' 'a-z')"

if [ -z "$auth_enabled" ]; then
    info "AUTH_ENABLED is not set in $ENV_FILE; compose defaults it to true"
fi

if [ "$auth_lc" != "false" ] && [ -z "$issuer" ]; then
    fail "AUTH_ENABLED is '${auth_enabled:-true}' and AUTHENTIK_ISSUER is empty"
    fail "  The build you are running now treats this as development mode and hands every"
    fail "  unauthenticated caller a full Admin. The new build answers 503 to every request"
    fail "  instead. Upgrading without setting AUTHENTIK_ISSUER takes the dashboard down."
elif [ "$auth_lc" = "false" ]; then
    warn "AUTH_ENABLED=false, so this deployment serves everyone as Admin by design"
else
    ok "auth is enabled (${auth_enabled:-unset, defaults true}) and AUTHENTIK_ISSUER is set"
fi

if [ -z "$(env_get AUTHENTIK_CLIENT_ID)" ]; then
    fail "AUTHENTIK_CLIENT_ID is empty; compose derives AUTHENTIK_AUDIENCE from it, so every"
    fail "  token will fail audience validation"
fi

# -----------------------------------------------------------------------------
section "Environment coverage for the new compose file"

if [ -z "$NEW_COMPOSE" ] || [ ! -f "$NEW_COMPOSE" ]; then
    warn "no --new-compose given, skipping the env coverage and compose delta checks"
    warn "  these are the two checks that catch a silently ignored .env, do not skip them"
else
    missing=""; hard=""
    for v in $(grep -oE '\$\{[A-Z_][A-Z0-9_]*' "$NEW_COMPOSE" | sed 's/\${//' | sort -u); do
        grep -qE "^[[:space:]]*${v}=" "$ENV_FILE" && continue
        if grep -qE "\\\$\\{${v}:\\?" "$NEW_COMPOSE"; then
            hard="$hard $v"
        elif grep -qE "\\\$\\{${v}\\}" "$NEW_COMPOSE"; then
            missing="$missing $v"
        fi
    done
    [ -n "$hard" ] && fail "the new compose refuses to start without:$hard"
    if [ -n "$missing" ]; then
        warn "the new compose reads these with no default and .env does not set them:$missing"
        warn "  each becomes an empty string, which fails later and further from the cause"
    fi
    [ -z "$hard" ] && [ -z "$missing" ] && ok "every required variable is present in $ENV_FILE"
fi

# -----------------------------------------------------------------------------
section "Compose file delta"

if [ -n "$NEW_COMPOSE" ] && [ -f "$NEW_COMPOSE" ] && [ -f "$CUR_COMPOSE" ]; then
    if diff -q "$CUR_COMPOSE" "$NEW_COMPOSE" >/dev/null 2>&1; then
        ok "compose files are identical, nothing to replace"
    else
        added="$(diff "$CUR_COMPOSE" "$NEW_COMPOSE" | grep -c '^>')"
        removed="$(diff "$CUR_COMPOSE" "$NEW_COMPOSE" | grep -c '^<')"
        warn "compose files differ: ${added} lines added, ${removed} removed"
        echo ""
        diff -u "$CUR_COMPOSE" "$NEW_COMPOSE" | sed 's/^/    /'
        echo ""
    fi
    # Named checks for the deltas that lose data or reliability rather than features. These
    # run whether or not the files differ, because a compose file copied from an old guide
    # can be missing all of them while matching nothing in particular.
    grep -q 'autoheal' "$CUR_COMPOSE" || \
        warn "no autoheal sidecar: a wedged-but-alive backend is never restarted"
    grep -q '/var/orcastra/uploads' "$CUR_COMPOSE" || \
        fail "no /var/orcastra/uploads volume: in-flight image and ISO uploads are lost on every recreate"
    grep -q 'mem_limit' "$CUR_COMPOSE" || \
        warn "no mem_limit: workers can be OOM-killed with the restart count staying at 0"
    grep -q 'stop_grace_period' "$CUR_COMPOSE" || \
        warn "no stop_grace_period: Docker's 10s default cuts live console sessions on every deploy"
    grep -q 'timeout=3' "$CUR_COMPOSE" || \
        warn "the backend healthcheck probe has no internal timeout, so a hung probe reads as a hung app"
else
    warn "cannot diff compose files, one of them is missing"
fi

# -----------------------------------------------------------------------------
section "Registered cluster ids"

if running "$PG_CONTAINER"; then
    bad=0; seen=0
    while IFS= read -r cid; do
        [ -n "$cid" ] || continue
        seen=$((seen + 1))
        printf '%s' "$cid" | grep -qE "$CLUSTER_ID_RE" || {
            fail "cluster id '$cid' does not match $CLUSTER_ID_RE"
            bad=$((bad + 1))
        }
    done <<EOF
$(psql_q 'SELECT DISTINCT cluster_id FROM organization_clusters')
EOF
    if [ "$seen" -eq 0 ]; then
        warn "no registered cluster ids found; the check inspected nothing"
    elif [ "$bad" -eq 0 ]; then
        ok "all ${seen} registered cluster ids match the new validation pattern"
    else
        fail "${bad} cluster id(s) will be rejected by the new build; rename before upgrading"
    fi
    # note: organization_clusters is the mirror. Vault is the registry of record, so a cluster
    # whose org binding was removed can exist there and not here.
    if [ -n "$(env_get VAULT_ADDR)" ]; then
        info "Vault is configured; cross-check the authoritative list with: vault kv list secret/clusters"
    fi
fi

# -----------------------------------------------------------------------------
section "Host capacity and image availability"

free_gb="$(df -BG --output=avail . 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "$free_gb" ]; then
    if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
        fail "only ${free_gb}GB free; a pull needs room for the new images alongside the old"
    else
        ok "${free_gb}GB free"
    fi
else
    warn "could not determine free disk space"
fi

pullable() {
    docker buildx imagetools inspect "$1" >/dev/null 2>&1 && return 0
    docker manifest inspect "$1" >/dev/null 2>&1 && return 0
    return 1
}
rev_of() {
    docker buildx imagetools inspect "$1" \
        --format '{{ index .Image.Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null
}

for ref in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
    if pullable "$ref"; then
        ok "$ref is available"
    else
        fail "$ref cannot be reached; run 'docker login' or check that the tag exists"
    fi
done

brev="$(rev_of "$BACKEND_IMAGE")"
frev="$(rev_of "$FRONTEND_IMAGE")"
if [ -n "$brev" ] && [ -n "$frev" ]; then
    info "backend  revision ${brev}"
    info "frontend revision ${frev}"
    if [ "$brev" != "$frev" ]; then
        fail "the two halves of ${TARGET_VERSION} were built from DIFFERENT commits"
        fail "  this happened to 1.0.0-RC4 once. Do not deploy it; ask for a rebuilt release."
    else
        ok "${TARGET_VERSION} is one commit on both services"
    fi
else
    warn "could not read provenance labels; the images may predate revision stamping"
fi

# -----------------------------------------------------------------------------
echo ""
echo "======================================================================"
if [ "$BLOCKERS" -gt 0 ]; then
    echo -e "${RED}FAIL: ${BLOCKERS} blocking finding(s), ${ADVISORIES} advisory. Do not upgrade yet.${NC}"
    exit 1
elif [ "$ADVISORIES" -gt 0 ]; then
    echo -e "${YEL}WARN: no blockers, ${ADVISORIES} advisory finding(s). Read them, then proceed.${NC}"
    exit 0
else
    echo -e "${GRN}PASS: clear to upgrade to ${TARGET_VERSION}.${NC}"
    exit 0
fi
