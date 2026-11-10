"""Plans cannot pile up without bound.

Each plan stores its request and preflight report, so without a limit a script could grow the
database at will. An operator holds at most OPEN_PLAN_LIMIT open plans, and expired plans no job
refers to are removed after PLAN_RETENTION.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization
from app.models.migration_models import MigrationJob, MigrationPlan
from app.services.migrations import planning, repository
from app.services.migrations.contracts import MigrationConflict
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
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


def _plan(index: int, *, requester: str, status: str, age: timedelta, ttl: timedelta = timedelta(minutes=15)) -> dict:
    now = repository.utcnow()
    return {
        "id": index,
        "plan_id": f"plan-{index:03d}",
        "scope": "instance",
        "intent": "copy",
        "status": status,
        "requester": requester,
        "requester_role": "partner",
        "source_organization_id": 1,
        "target_organization_id": 1,
        "source_cluster_id": "a",
        "source_project": "p",
        "source_resource_name": "web",
        "source_cluster_fingerprint": "fp",
        "target_cluster_id": "a",
        "target_project": "q",
        "target_cluster_fingerprint": "fp",
        "source_key": f"s{index}",
        "target_key": f"t{index}",
        "payload_hash": "h",
        "request_payload": {},
        "preflight_payload": {},
        "expires_at": now - age + ttl,
        "created_at": now - age,
        "updated_at": now - age,
    }


@pytest.mark.asyncio
async def test_only_unexpired_open_plans_of_that_operator_count(db: AsyncSessionFacade) -> None:
    rows = [_plan(i, requester="a@example.test", status="ready", age=timedelta(minutes=1)) for i in range(3)]
    rows += [_plan(10, requester="a@example.test", status="ready", age=timedelta(hours=2))]  # Past its TTL
    rows += [_plan(11, requester="a@example.test", status="executed", age=timedelta(minutes=1))]
    rows += [_plan(12, requester="b@example.test", status="blocked", age=timedelta(minutes=1))]
    db.sync.execute(insert(MigrationPlan), rows)
    db.sync.commit()

    assert await repository.count_open_plans(db, "a@example.test") == 3  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_expired_plans_no_job_refers_to_are_purged_after_the_retention(db: AsyncSessionFacade) -> None:
    old = repository.PLAN_RETENTION + timedelta(days=1)
    db.sync.execute(insert(MigrationPlan), [
        _plan(1, requester="a@example.test", status="expired", age=old),
        _plan(2, requester="a@example.test", status="expired", age=timedelta(days=2)),
        _plan(3, requester="a@example.test", status="executed", age=old),
    ])
    db.sync.commit()

    removed = await repository.purge_stale_plans(db)  # type: ignore[arg-type]

    kept = set(db.sync.execute(select(MigrationPlan.plan_id)).scalars())
    assert removed == 1
    assert kept == {"plan-002", "plan-003"}


@pytest.mark.asyncio
async def test_a_new_plan_is_refused_once_the_operator_holds_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Verifier:
        def __init__(self, _settings: Any) -> None:
            pass

        async def pin_user(self, _user: Any) -> Any:
            return SimpleNamespace(
                role=SimpleNamespace(value="partner"),
                pin=SimpleNamespace(identity=SimpleNamespace(email="a@example.test")),
            )

    async def count(_db: Any, requester: str) -> int:
        assert requester == "a@example.test"
        return repository.OPEN_PLAN_LIMIT

    async def must_not_plan(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("planning went on past the open-plan limit")

    planner = planning.MigrationPlanningMixin.__new__(planning.MigrationPlanningMixin)
    planner.db = object()
    planner.settings = SimpleNamespace()
    monkeypatch.setattr(planning, "AuthentikAuthorityVerifier", Verifier)
    monkeypatch.setattr(planning.repository, "count_open_plans", count)
    monkeypatch.setattr(planner, "_organizations", must_not_plan, raising=False)

    with pytest.raises(MigrationConflict, match=str(repository.OPEN_PLAN_LIMIT)):
        await planner.create_plan(SimpleNamespace(), user=object(), requester_role="partner")
