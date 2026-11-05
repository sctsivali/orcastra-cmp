"""Deleting a cluster registration also forgets the tags and snapshot schedules of its instances.

Deregistration removed the Vault entry, the organization bindings, the dashboard's trust and the
key grants, but left every tag assignment and snapshot schedule naming the cluster. Found when the
migration lab was torn down on 2026-11-04: rows from a lab deleted on 2026-10-29 were still there,
and the scheduler kept trying a schedule whose cluster was gone. A cluster registered again under
the same id would have shown the old tags on new instances with the same names.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization, Tag, TagAssignment, TagCategory
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.services.cluster_records import forget_cluster_records
from tests._sqlite_session import AsyncSessionFacade, sqlite_session


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    for model in (Organization, TagCategory, Tag, TagAssignment, SnapshotSchedule):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
            if column.server_default is not None and "::" in str(getattr(column.server_default, "arg", "")):
                column.server_default = None
        table.indexes.clear()
    metadata.create_all(facade.sync.get_bind())
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


def _seed(db: AsyncSessionFacade) -> None:
    db.sync.execute(insert(Tag), [{"id": 1, "name": "final-qa", "created_by": "ops@example.test"}])
    db.sync.execute(insert(TagAssignment), [
        {"id": 1, "tag_id": 1, "resource_type": "instance", "resource_id": "web", "resource_host": "gone", "resource_project": "p"},
        {"id": 2, "tag_id": 1, "resource_type": "project", "resource_id": "p", "resource_host": "gone", "resource_project": ""},
        {"id": 3, "tag_id": 1, "resource_type": "instance", "resource_id": "web", "resource_host": "kept", "resource_project": "p"},
    ])
    columns = {column.name for column in SnapshotSchedule.__table__.columns}
    base = {"project": "p", "instance_name": "web", "label": "daily", "frequency": "daily", "hour_utc": 3,
            "minute_utc": 0, "keep_last": 2, "owner_email": "ops@example.test", "state": "active",
            "next_run_at": datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)}
    rows = [{**base, "id": 1, "cluster_id": "gone"}, {**base, "id": 2, "cluster_id": "kept"}]
    db.sync.execute(insert(SnapshotSchedule), [{k: v for k, v in row.items() if k in columns} for row in rows])
    db.sync.commit()


@pytest.mark.asyncio
async def test_the_deleted_clusters_tags_and_schedules_go_and_the_others_stay(db: AsyncSessionFacade) -> None:
    _seed(db)

    counts = await forget_cluster_records(db, "gone")  # type: ignore[arg-type]
    db.sync.commit()

    assert counts == {"tag_assignments": 2, "snapshot_schedules": 1}
    assert [row.resource_host for row in db.sync.execute(select(TagAssignment)).scalars()] == ["kept"]
    assert [row.cluster_id for row in db.sync.execute(select(SnapshotSchedule)).scalars()] == ["kept"]
    assert db.sync.execute(select(Tag)).scalars().first() is not None, "the tag itself is shared and stays"


def test_the_delete_route_forgets_them_with_the_binding() -> None:
    import inspect

    from app.routers import cluster_registration

    source = inspect.getsource(cluster_registration.delete_cluster)
    binding = source.index("sql_delete(OrganizationCluster)")
    assert "forget_cluster_records(db, cluster_id)" in source[binding:binding + 1500]


def test_the_delete_route_leaves_a_registration_made_meanwhile_alone() -> None:
    # Security review 2026-11-05: someone may register the same id between the Vault deletes and the
    # binding step; that new registration's binding, tags and schedules are not this delete's.
    import inspect

    from app.routers import cluster_registration

    source = inspect.getsource(cluster_registration.delete_cluster)
    binding = source.index("async def _delete_binding")
    assert "active_registration(db, cluster_id)" in source[binding:binding + 900]
