#!/bin/sh
# Backend entrypoint: apply DB migrations before serving, then exec the CMD.
#
# Fail-fast on a migration error (set -e): a silent schema mismatch is exactly what let a
# code-without-schema deploy ship a broken state. Set RUN_MIGRATIONS=false to skip (ex; a
# read replica, or a one-off where the DB is managed out of band).
set -e

if [ "${RUN_MIGRATIONS:-true}" != "false" ]; then
  echo "[entrypoint] alembic upgrade head ..."
  alembic upgrade head
  echo "[entrypoint] migrations up to date."
else
  echo "[entrypoint] RUN_MIGRATIONS=false - skipping migrations."
fi

exec "$@"
