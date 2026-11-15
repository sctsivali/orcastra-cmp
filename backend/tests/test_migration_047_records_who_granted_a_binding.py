"""Migration 047 adds organization_clusters.granted_by, nullable, and changes no existing row.

A binding made before it cannot be told apart from a sale after the fact, so existing rows keep a
NULL grantor and read exactly as before. The real upgrade and downgrade also run on a Postgres
copy before a deploy; this pins the revision chain and the upgrade's shape on SQLite.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _migration():
    path = next(VERSIONS.glob("*_047_org_cluster_granted_by.py"))
    spec = importlib.util.spec_from_file_location("m047", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_follows_046() -> None:
    module = _migration()
    assert module.revision == "047_org_cluster_granted_by"
    assert module.down_revision == "046_forget_deregistered_records"


def test_the_upgrade_adds_a_nullable_column_and_keeps_every_row(monkeypatch) -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE organization_clusters (id INTEGER PRIMARY KEY, organization_id INTEGER, "
            "cluster_id TEXT NOT NULL, registered_by TEXT, registered_at TEXT NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO organization_clusters (organization_id, cluster_id, registered_by, registered_at) "
            "VALUES (1, 'sold', NULL, '2026-11-01'), (2, 'own', 'p@example.test', '2026-11-01')"
        ))
        module = _migration()
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(conn)))
        module.upgrade()
        columns = {c["name"]: c for c in inspect(conn).get_columns("organization_clusters")}
        rows = conn.execute(text("SELECT cluster_id, registered_by, granted_by FROM organization_clusters ORDER BY id")).all()

    assert columns["granted_by"]["nullable"] is True
    assert [tuple(r) for r in rows] == [("sold", None, None), ("own", "p@example.test", None)]
