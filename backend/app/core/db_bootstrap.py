"""Reconcile a legacy ``create_all`` database with Alembic before migrating.

Releases up to and including v1.0.0-RC2 materialized the schema with SQLAlchemy
``Base.metadata.create_all`` and shipped no migrations, so the database ends up
with all the tables but **no** ``alembic_version`` stamp. When a newer build's
entrypoint then runs ``alembic upgrade head``, Alembic starts from base and the
first ``CREATE TABLE`` fails with ``relation "..." already exists``.

This module runs *before* ``alembic upgrade head`` and repairs that state
automatically: if it finds a populated-but-unstamped database that matches a
known baseline, it stamps it to the matching revision(s) so the subsequent
upgrade applies only the genuinely missing migrations.

It is deliberately conservative:

* An already-stamped database is left untouched (normal upgrade path).
* A truly empty database is left untouched (Alembic builds it from base).
* It stamps **only** when the live schema unambiguously matches a known
  baseline. If it sees a populated, unstamped database whose schema is newer
  than the last known baseline, it refuses to guess and exits non-zero with
  actionable instructions, so it can never corrupt an unexpected schema.

Idempotent: re-running it after a successful stamp is a no-op.
"""
from __future__ import annotations

import asyncio
import os
from typing import List, Set, Tuple, Union

# Tables that a v1.0.0-RC2 create_all database is guaranteed to have. Presence of
# any of these (with no alembic stamp) marks a legacy populated database.
_RC2_CORE_TABLES: Tuple[str, ...] = (
    "organizations",
    "tag_categories",
    "metrics_snapshots",
    "session_audit_logs",
    "tenant_access_policies",
)

# Tables introduced by migrations *after* the RC2 baseline (008-013). If any of
# these already exist in an unstamped database, the schema is past RC2 and we do
# not know exactly where it sits, so we refuse to auto-stamp.
_POST_RC2_TABLES: Tuple[str, ...] = (
    "image_upload_sessions",   # 008
    "break_glass_grants",      # 011
    "iso_upload_sessions",     # 012
    "rebuild_jobs",            # 013
)

# The two Alembic heads that describe the RC2 schema exactly (its history forked
# at 001 and only re-merges at 009_merge_org_heads).
_RC2_STAMP: List[str] = ["007_metrics_snapshots", "002_org_external_source"]


def _asyncpg_dsn(url: str) -> str:
    """Normalise a SQLAlchemy URL to a plain DSN that ``asyncpg.connect`` accepts."""
    url = url.replace("+asyncpg", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


async def _live_tables_and_stamp(dsn: str) -> Tuple[Set[str], bool]:
    """Return (public table names, is_stamped) for the target database."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        tables = {r["tablename"] for r in rows}
        stamped = False
        if "alembic_version" in tables:
            stamped = (await conn.fetchval("SELECT count(*) FROM alembic_version")) > 0
        return tables, stamped
    finally:
        await conn.close()


def _plan(tables: Set[str], stamped: bool) -> Union[str, Tuple[str, List[str]]]:
    """Decide what to do. Returns "managed" | "fresh" | "rc2" | ("unknown", found)."""
    if stamped:
        return "managed"
    if not any(t in tables for t in _RC2_CORE_TABLES):
        return "fresh"
    found_post = [t for t in _POST_RC2_TABLES if t in tables]
    if not found_post:
        return "rc2"
    return ("unknown", found_post)


def _alembic_config():
    from alembic.config import Config

    app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = Config(os.path.join(app_dir, "alembic.ini"))
    # Resolve script_location absolutely so this works regardless of cwd.
    cfg.set_main_option("script_location", os.path.join(app_dir, "alembic"))
    return cfg


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("[db-bootstrap] DATABASE_URL not set; skipping legacy reconciliation.")
        return

    tables, stamped = asyncio.run(_live_tables_and_stamp(_asyncpg_dsn(url)))
    plan = _plan(tables, stamped)

    if plan == "managed":
        print("[db-bootstrap] database is already Alembic-managed; nothing to do.")
        return
    if plan == "fresh":
        print("[db-bootstrap] empty database; migrations will create the schema from base.")
        return
    if plan == "rc2":
        # Import + stamp only in this branch; runs a fresh asyncio loop internally
        # (the detection loop above has already finished, so there is no nesting).
        from alembic import command

        print(
            "[db-bootstrap] legacy unstamped (RC2) database detected; stamping to "
            f"{_RC2_STAMP} so the upgrade applies only the missing migrations."
        )
        command.stamp(_alembic_config(), _RC2_STAMP)
        print("[db-bootstrap] stamp complete; proceeding to upgrade.")
        return

    # ("unknown", found_post): populated + unstamped + newer than the RC2 baseline.
    _, found = plan
    raise SystemExit(
        "[db-bootstrap] REFUSING TO AUTO-STAMP.\n"
        "  The database has application tables but no Alembic version stamp, and its\n"
        f"  schema is newer than the known RC2 baseline (found: {', '.join(found)}).\n"
        "  Auto-stamping could corrupt it, so migrations were not run. Stamp the\n"
        "  database manually to the revision matching its real schema, then redeploy.\n"
        "  (Set RUN_MIGRATIONS=false to start the backend without migrating.)"
    )


if __name__ == "__main__":
    main()
