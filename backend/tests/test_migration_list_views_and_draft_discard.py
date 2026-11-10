"""The migration list filters by view on the server, and an operator can discard their own draft.

Computed in the browser from the newest page of jobs, the Active and Needs Attention tabs would
drop an older retained or blocked job and stop polling it. The server answers one view at a time
with exact counts for all three. Only a draft's author can discard it, and only before it started.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization
from app.models.migration_models import MigrationJob, MigrationPlan
from app.services.migrations import repository
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
ADMIN = repository.MigrationListVisibility(
    role="admin", actor_issuer="i", actor_subject="s", actor_email="ops@example.test",
    actor_provider_uuid="u", allowed_clusters=None, visible_projects=None,
)


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


def _plan(index: int, status: str = "ready") -> dict:
    return {
        "id": index, "plan_id": f"plan-{index}", "scope": "instance", "intent": "copy", "status": status,
        "requester": "ops@example.test", "requester_role": "partner", "source_organization_id": 1,
        "target_organization_id": 1, "source_cluster_id": "a", "source_project": "p", "source_resource_name": "web",
        "source_cluster_fingerprint": "fp", "target_cluster_id": "a", "target_project": "q",
        "target_cluster_fingerprint": "fp", "source_key": f"s{index}", "target_key": f"t{index}", "payload_hash": "h",
        "request_payload": {}, "preflight_payload": {}, "expires_at": NOW + timedelta(minutes=15),
        "created_at": NOW, "updated_at": NOW,
    }


def _job(index: int, phase: str, status: str, plan: int) -> dict:
    return {
        "id": index, "job_id": f"job-{index}", "plan_id": f"plan-{plan}", "plan_revision": 1, "scope": "instance",
        "intent": "copy", "requester": "ops@example.test", "requester_role": "partner", "source_organization_id": 1,
        "target_organization_id": 1, "source_cluster_id": "a", "source_project": f"p{index}",
        "source_resource_name": "web", "source_cluster_fingerprint": "fp", "target_cluster_id": "a",
        "target_project": f"q{index}", "target_cluster_fingerprint": "fp", "source_key": f"s{index}",
        "target_key": f"t{index}", "idempotency_key": f"k{index}", "payload_hash": "h", "strategy": "same_cluster_copy",
        "downtime_class": "none", "phase": phase, "status": status, "safe_result": {}, "operation_refs": [],
        "created_at": NOW + timedelta(seconds=index), "updated_at": NOW,
    }


def _seed_jobs(db: AsyncSessionFacade) -> None:
    db.sync.execute(insert(Organization), [{"id": 1, "name": "Org", "slug": "org", "created_by": "ops@example.test"}])
    db.sync.execute(insert(MigrationPlan), [_plan(i, "executed") for i in range(1, 7)])
    db.sync.execute(insert(MigrationJob), [
        _job(1, "source_retained", "waiting", 1),
        _job(2, "initial_sync", "running", 2),
        _job(3, "manual_recovery", "manual_recovery", 3),
        _job(4, "completed", "success", 4),
        _job(5, "failed", "failure", 5),
        _job(6, "cleanup_blocked", "waiting", 6),
    ])
    db.sync.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(("view", "expected"), [
    ("active", ["job-6", "job-3", "job-2", "job-1"]),
    ("attention", ["job-6", "job-5", "job-3"]),
    ("history", ["job-5", "job-4"]),
    (None, ["job-6", "job-5", "job-4", "job-3", "job-2", "job-1"]),
])
async def test_each_view_matches_the_console_tabs(db: AsyncSessionFacade, view, expected) -> None:
    _seed_jobs(db)

    rows, total = await repository.list_jobs(db, visibility=ADMIN, limit=50, offset=0, view=view)  # type: ignore[arg-type]

    assert [row.job_id for row in rows] == expected
    assert total == len(expected)


@pytest.mark.asyncio
async def test_the_counts_cover_every_view_whatever_the_page(db: AsyncSessionFacade) -> None:
    _seed_jobs(db)

    counts = await repository.count_jobs_by_view(db, visibility=ADMIN)  # type: ignore[arg-type]

    assert counts == {"active": 4, "attention": 3, "history": 2}


@pytest.mark.asyncio
async def test_a_draft_that_never_started_is_discarded(db: AsyncSessionFacade) -> None:
    db.sync.execute(insert(Organization), [{"id": 1, "name": "Org", "slug": "org", "created_by": "ops@example.test"}])
    db.sync.execute(insert(MigrationPlan), [_plan(1, "ready"), _plan(2, "expired"), _plan(3, "executed")])
    db.sync.execute(insert(MigrationJob), [_job(1, "completed", "success", 3)])
    db.sync.commit()

    assert await repository.discard_plan(db, "plan-1") is True  # type: ignore[arg-type]
    assert await repository.discard_plan(db, "plan-2") is True  # type: ignore[arg-type]
    assert await repository.discard_plan(db, "plan-3") is False, "a plan a migration ran from stays"  # type: ignore[arg-type]

    kept = [row.plan_id for row in db.sync.execute(select(MigrationPlan)).scalars()]
    assert kept == ["plan-3"]


def test_the_discard_route_is_kept_to_the_plans_author() -> None:
    import inspect

    from app.routers import migrations as router

    route = next(r for r in router.router.routes
                 if getattr(r, "path", "").endswith("/plans/{plan_id}") and "DELETE" in getattr(r, "methods", set()))
    source = inspect.getsource(route.endpoint)
    assert "_authorize_plan_current(" in source
    assert route.status_code == 204
