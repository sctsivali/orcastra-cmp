"""Reconcile a legacy ``create_all`` database with Alembic before migrating.

Releases up to and including v1.0.0-RC2 built the schema with
``Base.metadata.create_all`` and no migrations, so such a database has every
table but no ``alembic_version`` stamp, and ``alembic upgrade head`` fails on
the first ``CREATE TABLE`` with ``relation "..." already exists``.

This runs before ``alembic upgrade head``. A populated, unstamped database that
matches the RC2 baseline exactly is stamped to the matching revisions, so the
upgrade applies only the missing migrations. A stamped or empty database is
left alone. A populated, unstamped database that is partial or newer than the
baseline is refused with instructions, never guessed at. Re-running after a
successful stamp is a no-op.
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

# Tables introduced by migrations after the RC2 baseline (008-013). If any exists
# in an unstamped database, the schema is past RC2 at an unknown revision, so it
# is not auto-stamped.
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
    """Decide what to do.

    Returns "managed" | "fresh" | "rc2" | ("unknown", found) | ("partial", missing).

    important: a database counts as RC2 only with all of the core tables. Stamping
    a partial one (half a restored dump, an interrupted install) would record
    001-007 as applied, the upgrade would fail at 010 on a table that was never
    created, and the wrong stamp would make every retry fail the same way.
    """
    if stamped:
        return "managed"
    present = [t for t in _RC2_CORE_TABLES if t in tables]
    if not present:
        return "fresh"
    found_post = [t for t in _POST_RC2_TABLES if t in tables]
    if found_post:
        return ("unknown", found_post)
    missing = [t for t in _RC2_CORE_TABLES if t not in tables]
    if missing:
        return ("partial", missing)
    return "rc2"


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

    # An edition recognises its own legacy shapes first; one that returns True has reconciled the
    # database (stamped it, or found nothing to do) and the upgrade proceeds.
    from app.editions import current_edition

    for reconcile in current_edition().db_reconcilers:
        if reconcile(tables, stamped, url):
            return

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

    kind, offending = plan
    if kind == "partial":
        raise SystemExit(
            "[db-bootstrap] REFUSING TO AUTO-STAMP.\n"
            "  The database has application tables but no Alembic version stamp, and it is\n"
            f"  missing tables an RC2 database always has (missing: {', '.join(offending)}).\n"
            "  That is not a shape this can safely recognise, so migrations were not run.\n"
            "  Restore a complete backup, or stamp the database manually to the revision\n"
            "  matching its real schema, then redeploy.\n"
            "  (Set RUN_MIGRATIONS=false to start the backend without migrating.)"
        )

    # ("unknown", found_post): populated + unstamped + newer than the RC2 baseline.
    raise SystemExit(
        "[db-bootstrap] REFUSING TO AUTO-STAMP.\n"
        "  The database has application tables but no Alembic version stamp, and its\n"
        f"  schema is newer than the known RC2 baseline (found: {', '.join(offending)}).\n"
        "  Auto-stamping could corrupt it, so migrations were not run. Stamp the\n"
        "  database manually to the revision matching its real schema, then redeploy.\n"
        "  (Set RUN_MIGRATIONS=false to start the backend without migrating.)"
    )


if __name__ == "__main__":
    main()
