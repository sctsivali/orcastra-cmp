"""Migration 046 removes what deregistrations left behind before the cleanup existed.

Until 2026-11-05 deleting a cluster left its instances' tag assignments and snapshot schedules
(owner decision: sweep them once at upgrade). Only a cluster id whose every registration has ended
counts as deregistered: an id with an active registration, or with no registration row at all,
keeps its rows.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB

from app.models.cluster_registration_models import ClusterRegistration
from app.models.db_models import Organization, Tag, TagAssignment, TagCategory
from app.models.snapshot_schedule_models import SnapshotSchedule
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


def _migration():
    path = next(VERSIONS.glob("*_046_forget_deregistered_records.py"))
    spec = importlib.util.spec_from_file_location("m046", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    for model in (Organization, TagCategory, Tag, TagAssignment, SnapshotSchedule, ClusterRegistration):
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
    db.sync.execute(insert(ClusterRegistration), [
        {"id": 1, "cluster_id": "gone", "registration_id": "r1", "registered_at": NOW, "ended_at": NOW, "ended_reason": "deregistered"},
        {"id": 2, "cluster_id": "back", "registration_id": "r2", "registered_at": NOW, "ended_at": NOW, "ended_reason": "deregistered"},
        {"id": 3, "cluster_id": "back", "registration_id": "r3", "registered_at": NOW, "ended_at": None},
        {"id": 4, "cluster_id": "live", "registration_id": "r4", "registered_at": NOW, "ended_at": None},
    ])
    db.sync.execute(insert(Tag), [{"id": 1, "name": "qa", "created_by": "ops@example.test"}])
    hosts = ["gone", "back", "live", "never-recorded"]
    db.sync.execute(insert(TagAssignment), [
        {"id": i + 1, "tag_id": 1, "resource_type": "instance", "resource_id": "web", "resource_host": host, "resource_project": "p"}
        for i, host in enumerate(hosts)
    ])
    columns = {column.name for column in SnapshotSchedule.__table__.columns}
    base = {"project": "p", "instance_name": "web", "label": "daily", "frequency": "daily", "hour_utc": 3, "minute_utc": 0,
            "keep_last": 2, "owner_email": "ops@example.test", "state": "active", "next_run_at": NOW}
    db.sync.execute(insert(SnapshotSchedule), [
        {k: v for k, v in {**base, "id": i + 1, "cluster_id": host}.items() if k in columns} for i, host in enumerate(hosts)
    ])
    db.sync.commit()


def test_only_a_cluster_whose_every_registration_ended_loses_its_records(db: AsyncSessionFacade) -> None:
    _seed(db)
    module = _migration()

    for statement in module.FORGET:
        db.sync.execute(text(statement))
    db.sync.commit()

    kept_tags = sorted(row.resource_host for row in db.sync.execute(select(TagAssignment)).scalars())
    kept_schedules = sorted(row.cluster_id for row in db.sync.execute(select(SnapshotSchedule)).scalars())
    assert kept_tags == ["back", "live", "never-recorded"]
    assert kept_schedules == ["back", "live", "never-recorded"]


def test_the_revision_follows_045() -> None:
    module = _migration()
    assert module.down_revision == "045_branding_inheritance"
    assert module.revision == "046_forget_deregistered_records"
