"""Fences and the retention sweep while a retained move is recovered, against real SQL rows.

A move in retention holds only the exact lock on its stopped source, so the rest of the cluster
stays writable for the whole window. Once a rollback or cleanup starts mutating daemons again,
its locks must fence the project and the cluster like any running migration. The sweep honours
the rollback contract the operator approved as well as the current global flag.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization
from app.models.migration_models import (
    MigrationAuditOutbox,
    MigrationJob,
    MigrationPlan,
    MigrationResourceLock,
)
from app.services import migration_fence
from app.services.migrations.retention_store import schedule_due_cleanups
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

from types import SimpleNamespace

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations.planning_checks import project_lock_blockers
from app.services.migrations.project_retention_store import fence_project_for_recovery
from app.services.migrations.repository import RepositoryConflict
from app.services.migrations.retention_store import refence_target_for_rollback
from app.services.migrations.security import canonical_resource_key

PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _default_to_no_active_migration_fence() -> None:
    """Override the suite default so these tests exercise the real fence queries."""


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    for model in (Organization, MigrationPlan, MigrationJob, MigrationResourceLock, MigrationAuditOutbox):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()  # SQLite autoincrements INTEGER PRIMARY KEY only.
            # Postgres casts such as '{}'::jsonb are not SQLite syntax; every insert here is explicit.
            if column.server_default is not None and "::" in str(getattr(column.server_default, "arg", "")):
                column.server_default = None
        table.indexes.clear()
    metadata.create_all(facade.sync.get_bind())
    facade.sync.execute(
        insert(Organization), [{"id": 1, "name": "Org", "slug": "org", "created_by": "ops@example.test"}]
    )
    facade.sync.commit()
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


def _endpoints(job_id: str) -> dict[str, Any]:
    return {
        "scope": "instance",
        "intent": "move",
        "requester": "ops@example.test",
        "requester_role": "partner",
        "source_organization_id": 1,
        "target_organization_id": 1,
        "source_cluster_id": "cluster-a",
        "source_resource_name": "web",
        "source_cluster_fingerprint": "fp-a",
        "target_cluster_id": "cluster-b",
        "target_cluster_fingerprint": "fp-b",
        "source_key": f"src-{job_id}",
        "target_key": f"dst-{job_id}",
        "payload_hash": "h",
    }


def _seed(
    db: AsyncSessionFacade,
    job_id: str,
    *,
    phase: str,
    status: str = "waiting",
    automatic_cleanup: Optional[bool] = True,
    deadline: Optional[datetime] = None,
) -> None:
    contract = {} if automatic_cleanup is None else {"automatic_cleanup": automatic_cleanup}
    db.sync.execute(
        insert(MigrationPlan),
        [{
            **_endpoints(job_id),
            "id": 1,
            "plan_id": f"plan-{job_id}",
            "source_project": "tenant-a",
            "target_project": "tenant-b",
            "expires_at": PAST + timedelta(days=365),
            "preflight_payload": {"rollback_contract": contract},
        }],
    )
    db.sync.execute(
        insert(MigrationJob),
        [{
            **_endpoints(job_id),
            "id": 1,
            "job_id": job_id,
            "plan_id": f"plan-{job_id}",
            "plan_revision": 1,
            "source_project": "tenant-a",
            "target_project": "tenant-b",
            "idempotency_key": f"key-{job_id}",
            "strategy": "cross_cluster_pull",
            "downtime_class": "cold",
            "phase": phase,
            "status": status,
            "active_source_key": f"src-{job_id}",
            "retention_deadline": deadline,
        }],
    )
    db.sync.execute(
        insert(MigrationResourceLock),
        [{
            "id": 1,
            "job_id": job_id,
            "lock_key": f"lock-{job_id}",
            "lock_scope": "instance",
            "cluster_id": "cluster-a",
            "project": "tenant-a",
            "resource_name": "web",
        }],
    )
    db.sync.commit()


@pytest.mark.parametrize(
    ("phase", "fenced"),
    [("source_retained", False), ("cleanup_due", False), ("rolling_back", True), ("cleaning_up", True)],
)
async def test_a_retained_move_fences_its_cluster_only_while_recovery_runs(
    db: AsyncSessionFacade, phase: str, fenced: bool
) -> None:
    _seed(db, "job-1", phase=phase)

    cluster = await migration_fence._active_cluster_lock_job_id(db, cluster_id="cluster-a")
    project = await migration_fence._active_project_lock_job_id(
        db, cluster_id="cluster-a", project="tenant-a"
    )

    assert cluster == ("job-1" if fenced else None)
    assert project == ("job-1" if fenced else None)
    assert await migration_fence._active_cluster_lock_job_id(db, cluster_id="cluster-z") is None


@pytest.mark.parametrize(
    ("approved", "automatic", "phase"),
    [
        (True, True, "cleaning_up"),
        (False, True, "cleanup_due"),
        (None, True, "cleanup_due"),
        (True, False, "cleanup_due"),
    ],
)
async def test_expired_retention_deletes_automatically_only_when_the_plan_approved_it(
    db: AsyncSessionFacade, approved: Optional[bool], automatic: bool, phase: str
) -> None:
    _seed(db, "job-1", phase="source_retained", automatic_cleanup=approved, deadline=PAST)

    await schedule_due_cleanups(db, automatic=automatic)

    row = db.sync.execute(select(MigrationJob.phase, MigrationJob.status)).one()
    assert row.phase == phase


async def test_a_project_plan_waits_for_a_retained_move_into_that_project(db: AsyncSessionFacade) -> None:
    """A retained instance move locks only its source, yet its live target in the other project
    is still its rollback object."""
    _seed(db, "job-1", phase="source_retained")
    request = MigrationPlanCreateRequest.model_validate(
        {
            "scope": "project",
            "source": {"cluster_id": "cluster-b", "project": "tenant-b"},
            "target": {"cluster_id": "cluster-c", "project": "tenant-b"},
        }
    )

    [blocker] = await project_lock_blockers(db, request)

    assert blocker["code"] == "project_in_active_migration"
    assert await project_lock_blockers(
        db,
        MigrationPlanCreateRequest.model_validate(
            {
                "scope": "project",
                "source": {"cluster_id": "cluster-c", "project": "elsewhere"},
                "target": {"cluster_id": "cluster-d", "project": "elsewhere"},
            }
        ),
    ) == []


async def test_a_teardown_fence_refuses_a_project_another_job_moved_into(db: AsyncSessionFacade) -> None:
    _seed(db, "job-1", phase="source_retained")
    relocation = SimpleNamespace(
        job_id="job-2", target_cluster_id="cluster-b", target_project="tenant-b",
        source_cluster_id="cluster-z", source_project="old",
    )

    with pytest.raises(RepositoryConflict, match="job-1"[:8]):
        await fence_project_for_recovery(db, relocation, side="target")


async def test_an_instance_rollback_refuses_a_target_another_job_now_holds(db: AsyncSessionFacade) -> None:
    _seed(db, "job-1", phase="source_retained")
    key = canonical_resource_key("instance", "cluster-b", "tenant-b", "web")
    db.sync.execute(
        insert(MigrationResourceLock),
        [{"id": 2, "job_id": "job-other", "lock_key": key, "lock_scope": "instance",
          "cluster_id": "cluster-b", "project": "tenant-b", "resource_name": "web"}],
    )
    db.sync.commit()
    job = SimpleNamespace(
        job_id="job-1", target_cluster_id="cluster-b", target_project="tenant-b",
        target_resource_name=None, source_resource_name="web", target_cluster_fingerprint="fp-b",
    )

    with pytest.raises(RepositoryConflict):
        await refence_target_for_rollback(db, job)
