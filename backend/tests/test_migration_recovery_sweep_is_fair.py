"""The recovery sweep reaches every organization's oldest waiting job.

A claim refused for want of a slot changes nothing, so a sweep ordered by age alone would let
one organization with more queued jobs than the limit hold every place and starve the rest. The
sweep takes each organization's oldest job first, then each one's next.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert

from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization
from app.models.migration_models import MigrationJob, MigrationPlan
from app.services.migrations import repository
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

START = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    for model in (Organization, MigrationPlan, MigrationJob):
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


def _queued(index: int, organization_id: int, updated_at: datetime) -> dict:
    job_id = f"job-{organization_id}-{index:03d}"
    return {
        "id": index + organization_id * 1000,
        "job_id": job_id,
        "plan_id": f"plan-{job_id}",
        "plan_revision": 1,
        "scope": "instance",
        "intent": "copy",
        "requester": "ops@example.test",
        "requester_role": "partner",
        "source_organization_id": organization_id,
        "target_organization_id": organization_id,
        "source_cluster_id": f"cluster-{organization_id}",
        "source_project": f"p{index}",
        "source_resource_name": "web",
        "source_cluster_fingerprint": "fp",
        "target_cluster_id": f"cluster-{organization_id}",
        "target_project": f"q{index}",
        "target_cluster_fingerprint": "fp",
        "source_key": f"src-{job_id}",
        "target_key": f"dst-{job_id}",
        "idempotency_key": f"key-{job_id}",
        "payload_hash": "h",
        "strategy": "same_cluster_copy",
        "downtime_class": "none",
        "phase": "queued",
        "status": "pending",
        "safe_result": {},
        "operation_refs": [],
        "created_at": updated_at,
        "updated_at": updated_at,
    }


@pytest.mark.asyncio
async def test_a_newer_job_of_another_organization_is_offered_despite_a_large_backlog(
    db: AsyncSessionFacade,
) -> None:
    backlog = [_queued(i, 1, START + timedelta(seconds=i)) for i in range(60)]
    newcomer = _queued(0, 2, START + timedelta(hours=1))
    db.sync.execute(insert(MigrationJob), [*backlog, newcomer])
    db.sync.commit()

    offered = await repository.recoverable_job_ids(db, limit=50)  # type: ignore[arg-type]

    assert len(offered) == 50
    assert newcomer["job_id"] in offered
    assert offered[0] == backlog[0]["job_id"]
