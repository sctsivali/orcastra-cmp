"""Recognise a Mini 1.0.0-RC1 database and bring it under Alembic before the upgrade runs.

The RC1 image built its schema with create_all and never stamped alembic_version, so the CMP
bootstrap sees post-RC2 tables with no stamp and refuses to touch it. This reconciler runs first
(app/editions seam in core/db_bootstrap.py): it recognises exactly the RC1 shape, repairs what
create_all built differently from the migrations, and stamps the revisions that shape matches, so
`alembic upgrade head` applies only what RC1 lacks.

important: recognition is strict. A database with the Mini tables but missing RC1's core tables
(half a restore), or with tables from later releases, is refused rather than stamped: a wrong stamp
makes every later migration fail the same way and the operator loses the upgrade path.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Set

from app.core.db_bootstrap import _RC2_CORE_TABLES, _alembic_config, _asyncpg_dsn

MINI_TABLES = frozenset({"local_identities", "audit_log"})
RC1_TABLES = frozenset(_RC2_CORE_TABLES) | {"image_upload_sessions"} | MINI_TABLES
# Created by CMP migrations after the revisions RC1's schema matches; none can exist in RC1.
LATER_TABLES = frozenset({"break_glass_grants", "iso_upload_sessions", "rebuild_jobs", "local_identity_certificates"})
RC1_STAMP = ["011_audit_log_anchor", "002_org_external_source"]
ORG_SOURCE_COLUMNS = frozenset({"source", "external_id", "api_key_id"})


def recognise(tables: Set[str], stamped: bool) -> Optional[str]:
    """"rc1" for an unstamped RC1 database, None for a shape this does not own; raises to refuse."""
    if stamped or not (tables & MINI_TABLES):
        return None
    missing = sorted(RC1_TABLES - tables)
    if missing:
        raise SystemExit(
            "[db-bootstrap] REFUSING TO AUTO-STAMP.\n"
            "  The database has Orcastra Mini tables but no Alembic stamp, and it is missing\n"
            f"  tables a Mini 1.0.0-RC1 database always has (missing: {', '.join(missing)}).\n"
            "  Restore a complete backup of the RC1 database, then run the upgrade again."
        )
    later = sorted(tables & LATER_TABLES)
    if later:
        raise SystemExit(
            "[db-bootstrap] REFUSING TO AUTO-STAMP.\n"
            "  The database has Orcastra Mini tables, no Alembic stamp, and tables from a later\n"
            f"  release ({', '.join(later)}). That is not a shape this can recognise safely.\n"
            "  Restore the pre-upgrade backup, or stamp the database to its real revision."
        )
    return "rc1"


async def _repair(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            columns = {
                r["column_name"] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'organizations'"
                )
            }
            if not ORG_SOURCE_COLUMNS <= columns:
                raise SystemExit(
                    "[db-bootstrap] REFUSING TO AUTO-STAMP.\n"
                    "  The organizations table lacks columns every Mini 1.0.0-RC1 database has "
                    f"({', '.join(sorted(ORG_SOURCE_COLUMNS - columns))})."
                )
            width = await conn.fetchval(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'audit_log' AND column_name = 'event_id'"
            )
            if width is not None and width < 64:
                await conn.execute("ALTER TABLE audit_log ALTER COLUMN event_id TYPE varchar(64)")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS audit_log_anchor ("
                "id integer PRIMARY KEY, max_id bigint NOT NULL DEFAULT 0, row_count bigint NOT NULL DEFAULT 0, "
                "head_hash varchar(64), updated_at timestamptz)"
            )
    finally:
        await conn.close()


def reconcile(tables: Set[str], stamped: bool, url: str) -> bool:
    if recognise(tables, stamped) != "rc1":
        return False
    asyncio.run(_repair(_asyncpg_dsn(url)))
    from alembic import command

    print(
        "[db-bootstrap] unstamped Orcastra Mini 1.0.0-RC1 database detected; repaired its create_all "
        f"differences and stamping to {RC1_STAMP} so the upgrade applies only what it lacks."
    )
    command.stamp(_alembic_config(), RC1_STAMP)
    return True
