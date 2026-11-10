#!/bin/sh
# Backend entrypoint: apply DB migrations before serving, then exec the CMD.
#
# A migration error stops the container (set -e) instead of serving against the wrong schema.
# RUN_MIGRATIONS=false skips them (ex; a read replica, or a schema managed out of band).
set -e

if [ "${RUN_MIGRATIONS:-true}" != "false" ]; then
  # Stamp a legacy create_all database (up to v1.0.0-RC2, no Alembic stamp) so
  # `upgrade head` does not fail on the first CREATE TABLE. Idempotent; a no-op for
  # fresh or stamped databases. See app/core/db_bootstrap.py.
  echo "[entrypoint] reconciling database with Alembic (if needed) ..."
  python -m app.core.db_bootstrap
  echo "[entrypoint] alembic upgrade head ..."
  alembic upgrade head
  echo "[entrypoint] migrations up to date."
else
  echo "[entrypoint] RUN_MIGRATIONS=false - skipping migrations."
fi

exec "$@"
