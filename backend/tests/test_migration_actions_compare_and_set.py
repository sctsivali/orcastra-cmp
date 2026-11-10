"""Two operator actions on one revision: exactly one is applied.

An admin Rollback and a partner Finalize sent together on the same revision must not both land.
The router loads the job before the action, so the locked re-read must bypass the session's
identity map, and the update must carry a revision predicate.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert, text
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization
from app.models.migration_models import MigrationJob, MigrationPlan
from app.services.migrations import repository
from tests._sqlite_session import AsyncSessionFacade, sqlite_session


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
    facade.sync.execute(insert(MigrationJob), [{
        "id": 1, "job_id": "job-1", "plan_id": "plan-1", "plan_revision": 1, "revision": 4,
        "scope": "instance", "intent": "move", "requester": "ops@example.test", "requester_role": "partner",
        "source_organization_id": 1, "target_organization_id": 1, "source_cluster_id": "a",
        "source_project": "src", "source_resource_name": "web", "source_cluster_fingerprint": "fp",
        "target_cluster_id": "b", "target_project": "dst", "target_cluster_fingerprint": "fp",
        "source_key": "s", "target_key": "t", "idempotency_key": "k", "payload_hash": "h",
        "strategy": "cross_cluster_pull", "downtime_class": "cold", "phase": "source_retained",
        "status": "waiting", "safe_result": {}, "operation_refs": [],
    }])
    facade.sync.commit()
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


@pytest.mark.asyncio
async def test_the_locked_read_sees_a_revision_another_request_committed(db: AsyncSessionFacade) -> None:
    stale = await repository.get_job(db, "job-1")  # type: ignore[arg-type]
    assert stale is not None and stale.revision == 4
    # Another request's action lands: a plain UPDATE the ORM objects of this session never saw.
    db.sync.execute(text("UPDATE migration_jobs SET revision = 5, phase = 'rolling_back' WHERE job_id = 'job-1'"))
    db.sync.commit()

    locked = await repository.get_job(db, "job-1", lock=True)  # type: ignore[arg-type]

    assert locked is not None and locked.revision == 5 and locked.phase == "rolling_back"


@pytest.mark.asyncio
async def test_an_update_for_an_old_revision_is_not_applied(db: AsyncSessionFacade) -> None:
    applied = await repository.set_job(db, "job-1", expected_revision=3, phase="cleaning_up")  # type: ignore[arg-type]

    assert applied is False
    db.sync.expire_all()
    row = await repository.get_job(db, "job-1")  # type: ignore[arg-type]
    assert row is not None and row.phase == "source_retained" and row.revision == 4
