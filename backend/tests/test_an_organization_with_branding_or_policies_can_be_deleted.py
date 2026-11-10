"""An organization with branding, tenant policies or a desktop control policy can be deleted.

Each of those rows points at its organization through a NOT NULL column whose foreign key cascades
the delete in the database. The ORM backrefs leave the children to that cascade; otherwise
SQLAlchemy sets their organization_id to NULL first, the database refuses it, and the route answers
409 "still referenced by other records" (ex; a seller organization whose branding its buyers follow
could not be removed).

SQLite enforces the foreign keys here (PRAGMA foreign_keys=ON), so the cascade is the database's,
as on Postgres.
"""

from __future__ import annotations

from sqlalchemy import func, select, text

from app.models.db_models import (
    DesktopControlPolicy,
    Organization,
    OrganizationPreferences,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.services.organization_service import OrganizationService
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables


def _database():
    db = with_json_tables(sqlite_session(*org_tables()), OrganizationPreferences, TenantAccessPolicy, TenantPolicyRule, DesktopControlPolicy)
    db.sync.commit()
    with db.sync.get_bind().connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return db


def _organization(db, slug: str) -> int:
    org = Organization(name=slug, slug=slug, created_by="admin@example.com", is_active=True, source="local")
    db.add(org)
    db.sync.flush()
    return org.id


def _count(db, model) -> int:
    return db.sync.execute(select(func.count()).select_from(model)).scalar_one()


async def test_a_seller_with_branding_is_deleted_with_its_branding() -> None:
    db = _database()
    org_id = _organization(db, "seller")
    db.add(OrganizationPreferences(organization_id=org_id, settings={"branding": {"appName": "Seller"}}, updated_by="p@example.com"))
    db.sync.commit()

    assert await OrganizationService(db).delete_organization(org_id) is True

    assert _count(db, Organization) == 0
    assert _count(db, OrganizationPreferences) == 0


async def test_an_organization_with_policies_is_deleted_with_them() -> None:
    db = _database()
    org_id = _organization(db, "buyer")
    db.add(TenantAccessPolicy(organization_id=org_id, name="auto:shop", created_by="orcahub"))
    db.add(DesktopControlPolicy(organization_id=org_id, enabled=True))
    db.sync.commit()

    assert await OrganizationService(db).delete_organization(org_id) is True

    assert _count(db, TenantAccessPolicy) == 0
    assert _count(db, DesktopControlPolicy) == 0


async def test_another_organization_keeps_its_rows() -> None:
    db = _database()
    gone = _organization(db, "gone")
    kept = _organization(db, "kept")
    for org_id in (gone, kept):
        db.add(OrganizationPreferences(organization_id=org_id, settings={"colors": {"accentColor": "#1d4ed8"}}))
        db.add(TenantAccessPolicy(organization_id=org_id, name="p", created_by="admin@example.com"))
    db.sync.commit()

    await OrganizationService(db).delete_organization(gone)

    left = db.sync.execute(text("SELECT organization_id FROM organization_preferences")).scalars().all()
    assert left == [kept]
    assert db.sync.execute(text("SELECT organization_id FROM tenant_access_policies")).scalars().all() == [kept]


async def test_the_delete_route_clears_what_the_removed_grants_shaped(monkeypatch) -> None:
    """Its tenant policies go with the organization, so the shared lists they shaped are cleared."""
    from contextlib import asynccontextmanager

    from app.routers import organizations

    cleared: list[str] = []

    class _Cache:
        def invalidate_after_access_change(self) -> None:
            cleared.append("access")

    class _Service:
        def __init__(self, _db) -> None:
            pass

        async def get_organization(self, _org_id: int) -> object:
            return object()

        async def migration_history(self, _org_id: int) -> tuple[int, int]:
            return (0, 0)

        async def delete_organization(self, _org_id: int) -> bool:
            return True

    @asynccontextmanager
    async def guard(_db, _org_id):
        yield

    monkeypatch.setattr(organizations, "_guard_organization_write", guard)
    monkeypatch.setattr(organizations, "OrganizationService", _Service)
    monkeypatch.setattr(organizations, "get_cache_manager", lambda: _Cache(), raising=False)

    await organizations.delete_organization(7, user=object(), db=object())

    assert cleared == ["access"]
