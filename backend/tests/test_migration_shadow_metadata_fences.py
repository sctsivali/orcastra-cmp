"""Shadow metadata writes share Migration Center's final admission boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.schemas import BulkTagAssignRequest
from app.routers import snapshot_schedules, tags, tenant_access
from app.services import migration_fence
from app.services.migration_fence import (
    MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL,
    MigrationFenceConflict,
)
from app.services.snapshots.schedule_runner import RunOutcome


@pytest.mark.asyncio
async def test_multi_target_admission_locks_clusters_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    held: set[str] = set()
    db = object()

    @asynccontextmanager
    async def serialize(observed_db: Any, cluster_id: str):
        assert observed_db is db
        events.append(f"enter:{cluster_id}")
        held.add(cluster_id)
        try:
            yield
        finally:
            held.remove(cluster_id)
            events.append(f"exit:{cluster_id}")

    async def project_fence(*, cluster_id: str, project: str, db: Any) -> None:
        assert held == {"cluster-a", "cluster-b"}
        events.append(f"project:{cluster_id}/{project}")

    async def instance_fence(
        *, cluster_id: str, project: str, instance_name: str, db: Any
    ) -> None:
        assert held == {"cluster-a", "cluster-b"}
        events.append(f"instance:{cluster_id}/{project}/{instance_name}")

    monkeypatch.setattr(
        "app.services.shared_cluster_audit.serialize_cluster_configuration",
        serialize,
    )
    monkeypatch.setattr(migration_fence, "assert_project_not_migration_fenced", project_fence)
    monkeypatch.setattr(migration_fence, "assert_instance_not_migration_fenced", instance_fence)

    async with migration_fence.guard_migration_mutations(
        db,
        projects=(("cluster-b", "project-b"),),
        instances=(("cluster-a", "project-a", "web-01"),),
    ):
        assert held == {"cluster-a", "cluster-b"}
        events.append("body")

    assert events == [
        "enter:cluster-a",
        "enter:cluster-b",
        "project:cluster-b/project-b",
        "instance:cluster-a/project-a/web-01",
        "body",
        "exit:cluster-b",
        "exit:cluster-a",
    ]


@pytest.mark.asyncio
async def test_admission_failure_is_sanitized_but_body_failure_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BodyFailure(RuntimeError):
        pass

    @asynccontextmanager
    async def unavailable(_db: Any, _cluster_id: str):
        raise OSError("database endpoint and credentials must stay private")
        yield  # pragma: no cover

    monkeypatch.setattr(
        "app.services.shared_cluster_audit.serialize_cluster_configuration",
        unavailable,
    )
    with pytest.raises(MigrationFenceConflict) as unavailable_exc:
        async with migration_fence.guard_migration_mutations(
            object(),
            clusters=("cluster-a",),
        ):
            pytest.fail("unavailable admission yielded")
    assert unavailable_exc.value.detail == MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL
    assert "credentials" not in str(unavailable_exc.value)

    @asynccontextmanager
    async def available(_db: Any, _cluster_id: str):
        yield

    async def clear(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "app.services.shared_cluster_audit.serialize_cluster_configuration",
        available,
    )
    monkeypatch.setattr(migration_fence, "assert_cluster_not_migration_fenced", clear)
    with pytest.raises(BodyFailure):
        async with migration_fence.guard_migration_mutations(
            object(),
            clusters=("cluster-a",),
        ):
            raise BodyFailure("preserve the real operation failure")


@pytest.mark.asyncio
async def test_tag_write_keeps_private_admission_through_service_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = False

    @asynccontextmanager
    async def guard(**kwargs: Any):
        nonlocal held
        assert kwargs == {
            "clusters": set(),
            "projects": set(),
            "instances": {("cluster-a", "tenant-a", "web-01")},
        }
        held = True
        try:
            yield
        finally:
            held = False

    class TagService:
        async def set_resource_tags(self, **_kwargs: Any) -> dict[str, bool]:
            assert held
            return {"success": True}

    monkeypatch.setattr(tags, "guard_migration_mutations", guard)
    monkeypatch.setattr(tags, "verify_cluster_access", lambda _cluster: None)
    monkeypatch.setattr(
        tags,
        "_authorize_resource_target",
        lambda *_args, **_kwargs: ("cluster-a", "tenant-a", "web-01"),
    )

    result = await tags.set_resource_tags(
        resource_type="instance",
        resource_id="web-01",
        request=BulkTagAssignRequest(
            tags=["env_prod"],
            resource_type="instance",
            resource_id="web-01",
            resource_host="cluster-a",
            resource_project="tenant-a",
        ),
        host="cluster-a",
        project="tenant-a",
        user_id="operator@example.test",
        tag_service=TagService(),
        _=None,
        _rbac=object(),
    )

    assert result == {"success": True}
    assert held is False


@pytest.mark.asyncio
async def test_run_now_holds_admission_across_both_schedule_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = False
    commits = 0
    schedule = SimpleNamespace(
        id=7,
        cluster_id="cluster-a",
        project="tenant-a",
        instance_name="web-01",
        state="active",
        due_slot=None,
    )

    @asynccontextmanager
    async def guard(**kwargs: Any):
        nonlocal held
        assert kwargs == {
            "cluster_id": "cluster-a",
            "project": "tenant-a",
            "instance_name": "web-01",
        }
        held = True
        try:
            yield
        finally:
            held = False

    class DB:
        async def commit(self) -> None:
            nonlocal commits
            assert held
            commits += 1

        async def refresh(self, _row: Any) -> None:
            assert held

    async def get_owned(*_args: Any, **_kwargs: Any) -> Any:
        return schedule

    async def run(*_args: Any, **_kwargs: Any) -> RunOutcome:
        assert held
        return RunOutcome(result="success", snapshot="ops-7-now")

    monkeypatch.setattr(snapshot_schedules, "guard_instance_mutation", guard)
    monkeypatch.setattr(snapshot_schedules.svc, "get_owned", get_owned)
    monkeypatch.setattr(snapshot_schedules.svc, "to_info", lambda _row: None)
    monkeypatch.setattr(snapshot_schedules, "verify_project_access", lambda _h, p: p)
    monkeypatch.setattr(snapshot_schedules, "_run_one_guarded", run)
    monkeypatch.setattr(
        "app.services.snapshots.schedule_scheduler.settle_row",
        lambda _schedule, _outcome, _now: None,
    )

    response = await snapshot_schedules.run_schedule_now(
        schedule_id=7,
        _=None,
        user=SimpleNamespace(email="operator@example.test"),
        db=DB(),
        lxd_service=object(),
    )

    assert response.success is True
    assert commits == 2
    assert held is False


@pytest.mark.asyncio
async def test_policy_guard_holds_through_the_callers_transaction_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = False
    events: list[str] = []
    current_rule = SimpleNamespace(cluster_id="cluster-b", project_name="*")

    class ScalarResult:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Scalars:
        def all(self) -> list[Any]:
            return [current_rule]

    class RulesResult:
        def scalars(self) -> Scalars:
            return Scalars()

    class DB:
        calls = 0

        async def execute(self, statement: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                assert statement._for_update_arg is not None
                events.append("policy-lock")
                return ScalarResult(17)
            events.append("fresh-rules")
            return RulesResult()

    db = DB()

    @asynccontextmanager
    async def guard(observed_db: Any, **kwargs: Any):
        nonlocal held
        assert observed_db is db
        assert kwargs == {
            "clusters": {"cluster-b"},
            "projects": {("cluster-a", "tenant-a")},
        }
        events.append("migration-admission")
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(tenant_access, "guard_migration_mutations", guard)
    proposed_rules = [SimpleNamespace(cluster_id="cluster-a", project_name="tenant-a")]

    async with tenant_access._guard_rules_mutation(
        proposed_rules,
        db,
        policy_id=17,
    ):
        assert held
        events.append("body")
    assert held is False
    assert events == ["policy-lock", "fresh-rules", "migration-admission", "body"]
