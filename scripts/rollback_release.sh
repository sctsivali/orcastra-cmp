#!/bin/bash
# Orcastra Dashboard - roll back to a previously published release.
#
#   ./rollback_release.sh --to 1.0.0-RC3
#
# Two things about rolling this product back are not obvious, and both have teeth.
#
# 1. The schema, and why this script does not touch it.
#    The backend entrypoint runs `alembic upgrade head` under `set -e` on every start. If the
#    database is stamped with a revision the older image does not ship, alembic raises
#    "Can't locate revision identified by '<stamp>'" and the container dies before uvicorn
#    starts. db_bootstrap does not rescue it; it leaves an already-stamped database alone by
#    design. So a naive rollback across a migration is a crash loop, not a downgrade.
#    This script detects that and stops rather than guessing, because the right answer
#    depends on the migration. Migration 015 only widens three session_audit_logs counters
#    from int to bigint, and older code reads a bigint column without complaint, so for that
#    one the schema can simply stay where it is and RUN_MIGRATIONS=false is safe. Migration
#    015a (organization owner sets) is NOT: older code claims an organization without its
#    owner row and the foreign key it adds refuses that commit, so it is always downgraded
#    first: `alembic downgrade 015_session_audit_bigint` from an image that ships 015a.
#
# 2. The Redis flush, and why it is not optional.
#    Cached bodies written by the newer build carry fields the older models reject, and the
#    hardware models are declared extra="forbid". An older process inflating one of those
#    cached bodies raises a validation error and answers 500, on endpoints an operator hits
#    immediately, for as long as the entries live.
set -uo pipefail

RED='\033[0;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; BLU='\033[0;34m'; NC='\033[0m'
COMPOSE="docker-compose.prod.yml"
ENV_FILE=".env"
REGISTRY_REPO="svlct/orcastra-dashboard"
TO=""
ASSUME_YES=0
SKIP_MIGRATIONS=0

usage() {
    cat >&2 <<'EOF'
usage: rollback_release.sh --to <version> [options]

  --to <version>       the release to roll back to, for example 1.0.0-RC3   (required)
  --compose <path>     default docker-compose.prod.yml
  --env <path>         default .env
  --repo <namespace>   default svlct/orcastra-dashboard
  --skip-migrations    start the older image with RUN_MIGRATIONS=false. Use this when the
                       schema is ahead of the target image and the migrations in between are
                       forward-compatible, which is true of 015.
  -y, --yes            do not prompt
EOF
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --to)              TO="${2:-}";             shift 2 ;;
        --compose)         COMPOSE="${2:-}";        shift 2 ;;
        --env)             ENV_FILE="${2:-}";       shift 2 ;;
        --repo)            REGISTRY_REPO="${2:-}";  shift 2 ;;
        --skip-migrations) SKIP_MIGRATIONS=1;       shift ;;
        -y|--yes)          ASSUME_YES=1;            shift ;;
        -h|--help)         usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done
[ -n "$TO" ] || usage
[ -f "$ENV_FILE" ] || { echo "no such env file: $ENV_FILE" >&2; exit 3; }
[ -f "$COMPOSE" ]  || { echo "no such compose file: $COMPOSE" >&2; exit 3; }
command -v docker >/dev/null 2>&1 || { echo "missing dep: docker" >&2; exit 3; }

env_get() {
    sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n1 \
        | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

PREFIX="$(env_get CONTAINER_PREFIX)"; PREFIX="${PREFIX:-orcastra-dashboard}"
CURRENT="$(env_get APP_VERSION)"
TARGET_BACKEND="${REGISTRY_REPO}:backend-${TO}"
TARGET_FRONTEND="${REGISTRY_REPO}:frontend-${TO}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# note: caught here rather than later because the rest of this script would happily
# recreate both containers to land on the version already running, which during an incident
# reads as "the rollback did nothing" instead of "you asked for the version you have".
if [ -n "$CURRENT" ] && [ "$CURRENT" = "$TO" ]; then
    echo -e "${YEL}APP_VERSION is already ${TO}; there is nothing to roll back to.${NC}"
    echo "  Pass the version you want to return to, for example --to 1.0.0-RC3."
    exit 1
fi

echo -e "${YEL}Rolling back ${CURRENT:-unknown} -> ${TO}${NC}"
echo "  compose: $COMPOSE"
echo "  env:     $ENV_FILE"

# --- the target has to exist before anything is stopped ----------------------
for ref in "$TARGET_BACKEND" "$TARGET_FRONTEND"; do
    docker buildx imagetools inspect "$ref" >/dev/null 2>&1 \
        || docker manifest inspect "$ref" >/dev/null 2>&1 \
        || { echo -e "${RED}$ref is not published; nothing was changed${NC}"; exit 1; }
done
echo -e "${GRN}both target images are reachable${NC}"

# --- schema compatibility, checked before committing to anything -------------
PG_CONTAINER="${PREFIX}-postgres"
if docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    stamp_now="$(docker exec -i "$PG_CONTAINER" psql -U "$(env_get POSTGRES_USER)" \
        -d "$(env_get POSTGRES_DB)" -tAc 'SELECT version_num FROM alembic_version' 2>/dev/null)"
    if [ -n "$stamp_now" ] && docker image inspect "$TARGET_BACKEND" >/dev/null 2>&1; then
        hist="$(docker run --rm --entrypoint alembic "$TARGET_BACKEND" history 2>/dev/null)"
        if [ -n "$hist" ] && ! printf '%s' "$hist" | grep -q "$stamp_now"; then
            if [ "$stamp_now" = "015a_organization_owner_keys" ] && [ "$SKIP_MIGRATIONS" -eq 1 ]; then
                echo -e "${RED}--skip-migrations cannot cross 015a_organization_owner_keys: $TARGET_BACKEND claims${NC}"
                echo -e "${RED}organizations without their owner rows, which that revision's foreign key refuses.${NC}"
                echo "  Downgrade the schema first, from an image that ships 015a:"
                echo "    docker exec ${PREFIX}-backend alembic downgrade 015_session_audit_bigint"
                echo "  then run this again without --skip-migrations. Nothing was changed."
                exit 1
            fi
            if [ "$SKIP_MIGRATIONS" -eq 0 ]; then
                echo -e "${RED}The database is stamped '$stamp_now', which $TARGET_BACKEND does not ship.${NC}"
                echo "  Starting it as-is gives a crash loop, because the entrypoint runs"
                echo "  'alembic upgrade head' under set -e and cannot resolve that revision."
                echo ""
                echo "  Either downgrade the schema first, from an image that HAS the revision:"
                echo "    docker exec ${PREFIX}-backend alembic downgrade <older-revision>"
                echo "  or, when the migrations in between are forward-compatible (015 is,"
                echo "  015a is not), re-run this with --skip-migrations."
                exit 1
            fi
            echo -e "${YEL}schema is ahead of the target image; proceeding with RUN_MIGRATIONS=false${NC}"
        fi
    fi
else
    echo -e "${YEL}postgres container not running, skipping the schema compatibility check${NC}"
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Proceed? [y/N] '
    read -r reply
    case "$reply" in [Yy]*) ;; *) echo "aborted, nothing was changed"; exit 0 ;; esac
fi

# --- from here on the deployment is being modified ---------------------------
cp -a "$ENV_FILE" "${ENV_FILE}.before-rollback-${STAMP}"
echo -e "${GRN}saved ${ENV_FILE}.before-rollback-${STAMP}${NC}"

sed -i "s/^APP_VERSION=.*/APP_VERSION=${TO}/" "$ENV_FILE"
grep -q '^API_VERSION=' "$ENV_FILE" && sed -i "s/^API_VERSION=.*/API_VERSION=${TO}/" "$ENV_FILE"
if [ "$SKIP_MIGRATIONS" -eq 1 ]; then
    if grep -q '^RUN_MIGRATIONS=' "$ENV_FILE"; then
        sed -i "s/^RUN_MIGRATIONS=.*/RUN_MIGRATIONS=false/" "$ENV_FILE"
    else
        printf '\n# set by rollback_release.sh on %s, remove once rolled forward again\nRUN_MIGRATIONS=false\n' "$STAMP" >> "$ENV_FILE"
    fi
    echo -e "${YEL}RUN_MIGRATIONS=false written; remember to remove it when you roll forward${NC}"
fi
echo -e "${GRN}pinned APP_VERSION=${TO}${NC}"

# Pull before stopping anything, so a registry problem is found while the current build is
# still serving traffic.
docker compose -f "$COMPOSE" pull backend frontend || {
    echo -e "${RED}pull failed; nothing was stopped and the current build is still running${NC}"
    echo "  restore with: cp ${ENV_FILE}.before-rollback-${STAMP} ${ENV_FILE}"
    exit 1
}

# Scoped, not FLUSHALL: the vault namespace is unchanged in shape and refetching it costs
# round trips to Vault for no benefit.
for pattern in 'orcastra:api_responses:*' 'orcastra:monitoring_history:*' 'orcastra:sessions:*'; do
    docker exec "${PREFIX}-redis" sh -c \
        "redis-cli --scan --pattern '$pattern' | xargs -r -n 100 redis-cli DEL" >/dev/null 2>&1 \
        && echo -e "${GRN}flushed ${pattern}${NC}" \
        || echo -e "${YEL}could not flush ${pattern}; do it by hand before trusting the dashboard${NC}"
done

# --force-recreate because when only the tag moved the container spec can hash identically
# and compose would leave the old container running.
docker compose -f "$COMPOSE" up -d --force-recreate backend frontend

echo ""
echo -e "${BLU}Verify what is now running:${NC}"
echo "  docker inspect --format '{{.Config.Image}}' ${PREFIX}-backend"
echo "  docker image inspect ${TARGET_BACKEND} \\"
echo "    --format '{{ index .Config.Labels \"org.opencontainers.image.revision\" }}'"
echo "  docker inspect --format '{{.RestartCount}}' ${PREFIX}-backend    # must stay 0"
echo "  curl -s http://127.0.0.1:\$(sed -n 's/^BACKEND_PORT=//p' ${ENV_FILE} | tail -1)/health"
