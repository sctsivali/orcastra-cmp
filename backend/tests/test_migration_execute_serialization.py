"""Final migration acceptance shares the cluster serialization boundary with creates."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services import migration_fence
from app.services.migrations import planning, repository
from app.services.migrations import preflight as migration_preflight
from app.services.migrations.authority import ActorIdentityPin, MigrationAuthorityPin
from app.services.migrations.contracts import MigrationConflict
from app.services.migrations.plan_contracts import approval_report_hash


def _request(
    *,
    source_cluster: str = "cluster-z",
    target_cluster: str = "cluster-a",
) -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {
                "cluster_id": source_cluster,
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": target_cluster,
                "project": "target-project",
                "resource_name": "workload-copy",
            },
            "mappings": {"storage_pools": {"root": "fast"}},
        }
    )


def _report(*, include_concurrent_workload: bool = False) -> dict[str, Any]:
    resources = [
        {
            "resource_key": "instance:workload",
            "resource_type": "instance",
            "source": {"name": "workload", "identity_fingerprint": "source-id"},
            "target": {"name": "workload-copy"},
            "dependency_keys": [],
        }
    ]
    if include_concurrent_workload:
        resources.append(
            {
                "resource_key": "instance:concurrent-create",
                "resource_type": "instance",
                "source": {
                    "name": "concurrent-create",
                    "identity_fingerprint": "new-id",
                },
                "target": {"name": "concurrent-create"},
                "dependency_keys": [],
            }
        )
    return {
        "strategy": "staged_cold",
        "downtime_class": "required",
        "capabilities": [],
        "mappings": {"storage_pools": {"root": "fast"}},
        "resources": resources,
        "blockers": [],
        "warnings": [],
        "rollback_contract": {"source_retained": True},
    }


def _plan(request: MigrationPlanCreateRequest, report: dict[str, Any]) -> Any:
    return SimpleNamespace(
        plan_id="plan-a",
        revision=1,
        expires_at=repository.utcnow() + timedelta(minutes=15),
        status="ready",
        request_payload=request.model_dump(mode="json"),
        actor_issuer="https://identity.example.test/",
        actor_subject="fa6385b5-bebf-4704-8dcb-b92768559aca",
        actor_email="operator@example.test",
        actor_provider_uuid="d09ce115-b122-49e2-a46b-4299f63704ce",
        source_cluster_fingerprint="source-fingerprint",
        target_cluster_fingerprint="target-fingerprint",
        preflight_payload=report,
    )


def _authority() -> Any:
    return SimpleNamespace(
        pin=MigrationAuthorityPin(
            identity=ActorIdentityPin(
                email="operator@example.test",
                issuer="https://identity.example.test/",
                subject="fa6385b5-bebf-4704-8dcb-b92768559aca",
            ),
            provider_uuid="d09ce115-b122-49e2-a46b-4299f63704ce",
        ),
        role=SimpleNamespace(value="admin"),
    )


class _Database:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def commit(self) -> None:
        self.events.append("commit")


class _SQLiteDatabase:
    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class _FirstResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value


class _ActiveOperationDatabase:
    def __init__(self, *results: Any) -> None:
        self.results = list(results) or [("instance-operation-job",)]
        self.statement: Any = None
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _FirstResult:
        self.statement = statement
        self.statements.append(statement)
        return _FirstResult(self.results.pop(0))


@pytest.mark.asyncio
async def test_migration_cluster_locks_are_sorted_and_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def observed_lock(_db: Any, cluster_id: str):
        events.append(f"enter:{cluster_id}")
        try:
            yield
        finally:
            events.append(f"exit:{cluster_id}")

    monkeypatch.setattr(planning, "serialize_cluster_configuration", observed_lock)

    async with planning._serialize_migration_clusters(
        object(),
        ("cluster-z", "cluster-a", "cluster-z", "cluster-a"),
    ):
        events.append("body")

    assert events == [
        "enter:cluster-a",
        "enter:cluster-z",
        "body",
        "exit:cluster-z",
        "exit:cluster-a",
    ]


@pytest.mark.asyncio
async def test_ordinary_mutation_finishes_before_migration_can_commit_its_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutation-first ordering gives migration a fresh post-write preflight boundary."""
    db = _SQLiteDatabase()
    fence = {"active": False}
    events: list[str] = []
    mutation_entered = asyncio.Event()
    release_mutation = asyncio.Event()
    migration_attempted = asyncio.Event()

    async def check_fence(**_kwargs: Any) -> None:
        events.append("fence-check")
        if fence["active"]:
            raise migration_fence.MigrationFenceConflict()

    monkeypatch.setattr(
        migration_fence,
        "assert_instance_not_migration_fenced",
        check_fence,
    )

    async def ordinary_mutation() -> None:
        async with migration_fence.guard_instance_mutation(
            db,
            cluster_id="cluster-a",
            project="project-a",
            instance_name="web-1",
        ):
            events.append("mutation-admitted")
            mutation_entered.set()
            await release_mutation.wait()
            assert fence["active"] is False
            events.append("daemon-write")

    async def migration_acceptance() -> None:
        await mutation_entered.wait()
        migration_attempted.set()
        events.append("migration-waiting")
        async with planning._serialize_migration_clusters(db, ("cluster-a",)):
            fence["active"] = True
            events.append("migration-fence-committed")

    mutation_task = asyncio.create_task(ordinary_mutation())
    await mutation_entered.wait()
    migration_task = asyncio.create_task(migration_acceptance())
    await migration_attempted.wait()
    await asyncio.sleep(0)

    assert "migration-fence-committed" not in events
    release_mutation.set()
    await asyncio.gather(mutation_task, migration_task)

    assert events.index("daemon-write") < events.index("migration-fence-committed")


@pytest.mark.asyncio
async def test_migration_fence_wins_before_ordinary_mutation_and_no_daemon_write_occurs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration-first ordering makes the later mutation fail under the shared lock."""
    db = _SQLiteDatabase()
    fence = {"active": False}
    events: list[str] = []
    migration_entered = asyncio.Event()
    release_migration = asyncio.Event()

    async def check_fence(**_kwargs: Any) -> None:
        events.append("fence-check")
        if fence["active"]:
            raise migration_fence.MigrationFenceConflict()

    monkeypatch.setattr(
        migration_fence,
        "assert_instance_not_migration_fenced",
        check_fence,
    )

    async def migration_acceptance() -> None:
        async with planning._serialize_migration_clusters(db, ("cluster-a",)):
            fence["active"] = True
            events.append("migration-fence-committed")
            migration_entered.set()
            await release_migration.wait()

    async def ordinary_mutation() -> None:
        await migration_entered.wait()
        async with migration_fence.guard_instance_mutation(
            db,
            cluster_id="cluster-a",
            project="project-a",
            instance_name="web-1",
        ):
            events.append("daemon-write")

    migration_task = asyncio.create_task(migration_acceptance())
    await migration_entered.wait()
    mutation_task = asyncio.create_task(ordinary_mutation())
    await asyncio.sleep(0)
    assert "fence-check" not in events

    release_migration.set()
    await migration_task
    with pytest.raises(migration_fence.MigrationFenceConflict):
        await mutation_task

    assert events == ["migration-fence-committed", "fence-check"]


@pytest.mark.asyncio
async def test_preflight_blocks_an_active_durable_instance_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _ActiveOperationDatabase(("instance-operation-job",), None, None)
    service = planning.MigrationPlanningMixin(
        db=database,
        lxd_service=object(),
        settings=SimpleNamespace(),
    )
    result = migration_preflight.PreflightResult(
        strategy="staged_cold",
        downtime="required",
        capabilities={
            "source": {"engine": "lxd"},
            "target": {"engine": "lxd"},
            "checkpoint": True,
            "rollback": True,
            "live": False,
            "incremental_refresh": False,
        },
    )

    async def clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return object(), object()

    async def inspect(*_args: Any, **_kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(service, "_clients", clients)
    monkeypatch.setattr(planning, "_blocking", inspect)

    report = await service._preflight(
        _request(),
        source_fingerprint="source-fingerprint",
        target_fingerprint="target-fingerprint",
    )

    assert [blocker["code"] for blocker in report["blockers"]] == [
        "active_instance_operation"
    ]
    assert report["approval_hash"] == approval_report_hash(report)
    instance_statement = next(
        statement
        for statement in database.statements
        if "instance_operation_jobs" in str(statement)
    )
    params = instance_statement.compile().params
    bound_values = {
        item
        for value in params.values()
        for item in (value if isinstance(value, list) else [value])
    }
    assert {"cluster-z", "source-project", "workload"}.issubset(
        bound_values
    )


@pytest.mark.asyncio
async def test_member_relocation_rejects_an_automatic_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _ActiveOperationDatabase(None, None, None, None)
    service = planning.MigrationPlanningMixin(
        db=database,
        lxd_service=object(),
        settings=SimpleNamespace(),
    )
    result = migration_preflight.PreflightResult(
        strategy="member_relocation",
        downtime="required",
        capabilities={
            "source": {"engine": "lxd"},
            "target": {"engine": "lxd"},
            "checkpoint": True,
            "rollback": False,
            "live": False,
            "incremental_refresh": False,
        },
    )
    request = MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {
                "cluster_id": "cluster-z",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "cluster-z",
                "project": "source-project",
                "resource_name": "workload",
                "member": "member-b",
            },
            "checkpoint_requested": True,
        }
    )

    async def clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return object(), object()

    async def inspect(*_args: Any, **_kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(service, "_clients", clients)
    monkeypatch.setattr(planning, "_blocking", inspect)

    report = await service._preflight(
        request,
        source_fingerprint="source-fingerprint",
        target_fingerprint="source-fingerprint",
    )

    assert [blocker["code"] for blocker in report["blockers"]] == [
        "member_relocation_checkpoint_unsupported"
    ]
    checkpoint = next(
        capability
        for capability in report["capabilities"]
        if capability["code"] == "checkpoint"
    )
    assert checkpoint["supported"] is False
    assert report["daemon_capabilities"]["checkpoint"] is False
    assert report["approval_hash"] == approval_report_hash(report)


@pytest.mark.asyncio
async def test_durable_copy_rejects_an_automatic_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _ActiveOperationDatabase(None, None, None, None)
    service = planning.MigrationPlanningMixin(
        db=database,
        lxd_service=object(),
        settings=SimpleNamespace(),
    )
    result = migration_preflight.PreflightResult(
        strategy="cross_cluster_pull",
        downtime="none",
        capabilities={
            "source": {"engine": "lxd"},
            "target": {"engine": "lxd"},
            "checkpoint": True,
            "rollback": False,
            "live": False,
            "incremental_refresh": False,
        },
    )
    request = _request()
    request.intent = "copy"
    request.checkpoint_requested = True

    async def clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return object(), object()

    async def inspect(*_args: Any, **_kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(service, "_clients", clients)
    monkeypatch.setattr(planning, "_blocking", inspect)

    report = await service._preflight(
        request,
        source_fingerprint="source-fingerprint",
        target_fingerprint="target-fingerprint",
    )

    assert [blocker["code"] for blocker in report["blockers"]] == [
        "copy_checkpoint_unsupported"
    ]
    checkpoint = next(
        capability
        for capability in report["capabilities"]
        if capability["code"] == "checkpoint"
    )
    assert checkpoint["supported"] is False
    assert report["approval_hash"] == approval_report_hash(report)


def test_every_instance_mutation_boundary_uses_the_shared_guard() -> None:
    from app.routers import instance_rebuild, instance_specifications, instances
    from app.services import instance_specifications as specification_service

    handlers = {
        "start": instances.start_instance,
        "stop": instances.stop_instance,
        "restart": instances.restart_instance,
        "delete": instances.delete_instance,
        "rebuild admission": instance_rebuild.rebuild_instance,
        "rebuild execution": instance_rebuild._run_rebuild,
        "specification admission": instance_specifications.update_instance_specification,
        "specification execution": specification_service.run_specification_job,
    }

    for label, handler in handlers.items():
        assert "async with guard_instance_mutation(" in inspect.getsource(handler), label


@pytest.mark.asyncio
async def test_preflight_treats_an_in_flight_rebuild_as_active_mutation() -> None:
    database = _ActiveOperationDatabase(None, ("rebuild-job",))

    assert await planning._has_active_instance_operation(database, _request()) is True
    assert len(database.statements) == 2
    assert "rebuild_jobs" in str(database.statements[1])


@pytest.mark.asyncio
async def test_execute_commits_fences_before_releasing_locks_and_scheduling_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    held: list[str] = []
    request = _request()
    report = _report()
    plan = _plan(request, report)
    db = _Database(events)
    service = planning.MigrationPlanningMixin(
        db=db,
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    @asynccontextmanager
    async def observed_lock(_db: Any, cluster_id: str):
        events.append(f"enter:{cluster_id}")
        held.append(cluster_id)
        try:
            yield
        finally:
            events.append(f"exit:{cluster_id}")
            held.remove(cluster_id)

    async def no_replay(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_plan(*_args: Any, **_kwargs: Any) -> Any:
        return plan

    async def organizations(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        events.append("organizations")
        assert held == ["cluster-a", "cluster-z"]
        return 7, 7

    async def fingerprints(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        events.append("fingerprints")
        return "source-fingerprint", "target-fingerprint"

    async def fresh_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append("preflight")
        assert held == ["cluster-a", "cluster-z"]
        return report

    def require_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("stage")

    def acknowledgements_gate(**_kwargs: Any) -> None:
        events.append("acknowledgements")

    async def clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        events.append("clients")
        return object(), object()

    async def billing(*_args: Any, **_kwargs: Any) -> None:
        events.append("billing")

    async def create_job(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        events.append("create-job")
        assert held == ["cluster-a", "cluster-z"]
        await db.commit()
        return SimpleNamespace(job_id="job-a"), True

    def worker(job_id: str, *, allow_mutation: bool) -> object:
        assert (job_id, allow_mutation) == ("job-a", True)
        assert held == []
        events.append("worker-created")
        return object()

    def schedule(_worker: object) -> None:
        assert held == []
        events.append("schedule")

    monkeypatch.setattr(planning, "serialize_cluster_configuration", observed_lock)
    monkeypatch.setattr(planning.repository, "get_job_by_request", no_replay)
    monkeypatch.setattr(planning.repository, "get_plan", get_plan)
    monkeypatch.setattr(planning.repository, "create_job", create_job)
    monkeypatch.setattr(planning, "validate_acknowledgements", acknowledgements_gate)
    monkeypatch.setattr(planning, "_blocking", billing)
    monkeypatch.setattr(planning, "run_migration_job", worker)
    monkeypatch.setattr(planning, "_schedule", schedule)
    monkeypatch.setattr(service, "_organizations", organizations)
    monkeypatch.setattr(service, "_fingerprints", fingerprints)
    monkeypatch.setattr(service, "_preflight", fresh_preflight)
    monkeypatch.setattr(service, "_require_execution_stage", require_stage)
    monkeypatch.setattr(service, "_clients", clients)

    row, created = await service._execute_plan_with_digest(
        "plan-a",
        expected_revision=1,
        idempotency_key="request-a",
        digest="digest-a",
        reason="Approved relocation",
        acknowledgements=["approved"],
        user=object(),
        authority=_authority(),
    )

    assert row.job_id == "job-a"
    assert created is True
    assert events == [
        "enter:cluster-a",
        "enter:cluster-z",
        "organizations",
        "fingerprints",
        "preflight",
        "stage",
        "acknowledgements",
        "clients",
        "billing",
        "create-job",
        "commit",
        "exit:cluster-z",
        "exit:cluster-a",
        "worker-created",
        "schedule",
    ]


@pytest.mark.asyncio
async def test_concurrent_hierarchy_drift_revises_plan_without_creating_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    held: list[str] = []
    request = _request(source_cluster="cluster-b", target_cluster="cluster-a")
    approved_report = _report()
    drifted_report = _report(include_concurrent_workload=True)
    plan = _plan(request, approved_report)
    db = _Database(events)
    service = planning.MigrationPlanningMixin(
        db=db,
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    @asynccontextmanager
    async def observed_lock(_db: Any, cluster_id: str):
        events.append(f"enter:{cluster_id}")
        held.append(cluster_id)
        try:
            yield
        finally:
            events.append(f"exit:{cluster_id}")
            held.remove(cluster_id)

    async def no_replay(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_plan(*_args: Any, **_kwargs: Any) -> Any:
        return plan

    async def organizations(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        return 7, 7

    async def fingerprints(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        return "source-fingerprint", "target-fingerprint"

    async def fresh_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert held == ["cluster-a", "cluster-b"]
        events.append("preflight-detected-drift")
        return drifted_report

    async def revise(_db: Any, row: Any, *, report: dict[str, Any]) -> Any:
        assert held == ["cluster-a", "cluster-b"]
        assert report is drifted_report
        events.append("revise-plan")
        row.revision += 1
        await db.commit()
        return row

    async def unexpected_create(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("hierarchy drift created a migration job")

    def unexpected_schedule(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("hierarchy drift scheduled a migration worker")

    monkeypatch.setattr(planning, "serialize_cluster_configuration", observed_lock)
    monkeypatch.setattr(planning.repository, "get_job_by_request", no_replay)
    monkeypatch.setattr(planning.repository, "get_plan", get_plan)
    monkeypatch.setattr(planning.repository, "revise_plan_preflight", revise)
    monkeypatch.setattr(planning.repository, "create_job", unexpected_create)
    monkeypatch.setattr(planning, "_schedule", unexpected_schedule)
    monkeypatch.setattr(service, "_organizations", organizations)
    monkeypatch.setattr(service, "_fingerprints", fingerprints)
    monkeypatch.setattr(service, "_preflight", fresh_preflight)
    monkeypatch.setattr(service, "_require_execution_stage", lambda *_args: None)

    with pytest.raises(MigrationConflict, match="plan revision 2"):
        await service._execute_plan_with_digest(
            "plan-a",
            expected_revision=1,
            idempotency_key="request-a",
            digest="digest-a",
            reason="Approved relocation",
            acknowledgements=[],
            user=object(),
            authority=_authority(),
        )

    assert events == [
        "enter:cluster-a",
        "enter:cluster-b",
        "preflight-detected-drift",
        "revise-plan",
        "commit",
        "exit:cluster-b",
        "exit:cluster-a",
    ]
