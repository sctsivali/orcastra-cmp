"""Regression coverage for the latest fail-closed migration invariants."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models.migration_models import MigrationResourceLock
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.routers import instance_mobility
from app.services.migrations import (
    activation,
    executor,
    planning,
    preflight,
    preflight_source,
    reconcile_transfer,
    reconciliation,
    repository,
    transfer,
    worker,
)
from app.services.migrations.authority import ActorIdentityPin, MigrationAuthorityPin
from app.services.migrations.contracts import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationUnknown,
)


def _compiled(statement: Any) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _Result:
    def __init__(
        self,
        *,
        scalar_one: Any = None,
        scalar: Any = None,
        first: Any = None,
    ) -> None:
        self._scalar_one = scalar_one
        self._scalar = scalar
        self._first = first

    def scalar_one_or_none(self) -> Any:
        return self._scalar_one

    def scalar(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._first


@pytest.mark.asyncio
async def test_unresolved_jobs_reserve_every_concurrency_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = repository.utcnow()
    row = SimpleNamespace(
        job_id="candidate-job",
        source_organization_id=42,
        source_cluster_id="source-cluster",
        target_cluster_id="target-cluster",
    )
    results = iter(
        [
            _Result(),
            _Result(scalar_one=row),
            _Result(scalar=1),
            _Result(scalar=0),
            _Result(first=None),
        ]
    )

    class _Database:
        def __init__(self) -> None:
            self.statements: list[Any] = []
            self.rolled_back = False

        async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
            self.statements.append(statement)
            return next(results)

        async def rollback(self) -> None:
            self.rolled_back = True

    database = _Database()
    monkeypatch.setattr(
        repository,
        "get_settings",
        lambda: SimpleNamespace(migration_global_concurrency=1),
    )

    claimed = await repository.claim_job(database, row.job_id, "worker-a")  # type: ignore[arg-type]

    assert claimed is None
    assert database.rolled_back is True
    global_sql, organization_sql, cluster_sql = [
        _compiled(statement) for statement in database.statements[2:5]
    ]
    for sql in (global_sql, organization_sql, cluster_sql):
        assert "migration_jobs.status = 'running'" in sql
        assert "migration_jobs.lease_expires_at >" not in sql
        assert "migration_jobs.status IN ('unknown', 'manual_recovery')" in sql
        assert "migration_jobs.phase = 'reconciling'" in sql
        assert "'success'" not in sql
        assert "'failure'" not in sql
        assert "'cancelled'" not in sql
    assert "migration_jobs.source_organization_id = 42" in organization_sql
    assert "source-cluster" in cluster_sql
    assert "target-cluster" in cluster_sql

    predicate_sql = _compiled(repository._holds_concurrency_slot(now))
    assert "unknown" in predicate_sql
    assert "manual_recovery" in predicate_sql
    assert "reconciling" in predicate_sql


@pytest.mark.asyncio
async def test_resource_locks_are_inserted_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[Any] = []
    events: list[dict[str, Any]] = []

    class _Database:
        def add(self, value: Any) -> None:
            added.append(value)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, _value: Any) -> None:
            return None

        async def rollback(self) -> None:
            pytest.fail("deterministic lock insertion unexpectedly rolled back")

    async def no_existing(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def capture_event(*_args: Any, **kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(repository, "get_job_by_request", no_existing)
    monkeypatch.setattr(repository, "enqueue_event", capture_event)
    plan = SimpleNamespace(
        plan_id="plan-a",
        revision=1,
        scope="instance",
        intent="move",
        source_organization_id=7,
        target_organization_id=7,
        source_cluster_id="cluster-z",
        source_project="project-z",
        source_resource_name="workload-z",
        source_member=None,
        source_cluster_fingerprint="source-fingerprint",
        target_cluster_id="cluster-a",
        target_project="project-a",
        target_resource_name="workload-a",
        target_member=None,
        target_cluster_fingerprint="target-fingerprint",
        source_key="instance:cluster-z:project-z:workload-z",
        target_key="instance:cluster-a:project-a:workload-a",
        payload_hash="plan-digest",
        retention_hours=24,
        external_cutover_required=False,
        preflight_payload={
            "strategy": "cross_cluster_pull",
            "downtime_class": "cold",
            "resources": [],
        },
        status="ready",
        updated_at=None,
    )

    _row, created = await repository.create_job(
        _Database(),  # type: ignore[arg-type]
        plan=plan,
        requester="operator@example.test",
        requester_role="admin",
        actor_issuer="https://identity.example.test/",
        actor_subject="25bbd0bf-278a-477a-8c90-aac4543873d6",
        actor_email="operator@example.test",
        actor_provider_uuid="a8f936b1-9221-4101-b326-0eb23def243c",
        idempotency_key="request-a",
        payload_hash="request-digest",
        reason="Approved capacity move",
        acknowledgements=[
            "downtime-reviewed",
            "source-retention-understood",
            "billing-impact-reviewed",
        ],
    )

    locks = [value for value in added if isinstance(value, MigrationResourceLock)]
    lock_keys = [lock.lock_key for lock in locks]
    assert created is True
    assert len(lock_keys) == 4
    assert lock_keys == sorted(lock_keys)
    assert len(set(lock_keys)) == 4
    assert events == [
        {
            "job_id": _row.job_id,
            "event_type": "migration.accepted",
            "phase": "queued",
            "message": "Migration was accepted and queued.",
            "actor": {
                "issuer": "https://identity.example.test/",
                "subject": "25bbd0bf-278a-477a-8c90-aac4543873d6",
                "email": "operator@example.test",
                "provider_uuid": "a8f936b1-9221-4101-b326-0eb23def243c",
                "role": "admin",
            },
            "reason": "Approved capacity move",
        }
    ]


def test_migration_event_actor_requires_a_complete_current_authority_pin() -> None:
    with pytest.raises(repository.RepositoryConflict, match="identity is incomplete"):
        repository.event_actor(
            {
                "requester_role": "admin",
                "actor_issuer": "https://identity.example.test/",
                "actor_subject": "25bbd0bf-278a-477a-8c90-aac4543873d6",
                "actor_email": "operator@example.test",
            }
        )


@pytest.mark.asyncio
async def test_audit_delivery_uses_the_event_actor_not_the_latest_job_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = SimpleNamespace(
        event_id="event-a",
        job_id="job-a",
        channel="audit",
        event_type="migration.cutover",
        payload={
            "phase": "quiescing",
            "message": "Cutover was approved.",
            "reason": "Approved change request",
            "actor": {
                "issuer": "https://identity.example.test/",
                "subject": "25bbd0bf-278a-477a-8c90-aac4543873d6",
                "email": "approver@example.test",
                "provider_uuid": "a8f936b1-9221-4101-b326-0eb23def243c",
                "role": "partner",
            },
        },
    )
    latest_job = SimpleNamespace(
        requester="later-operator@example.test",
        target_cluster_id="cluster-a",
        target_project="project-a",
        target_organization_id=7,
    )
    delivered: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []

    class _SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: Any) -> None:
            return None

    async def pending(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [event]

    async def get_job(*_args: Any, **_kwargs: Any) -> Any:
        return latest_job

    async def finish(*_args: Any, **kwargs: Any) -> None:
        finished.append(kwargs)

    monkeypatch.setattr(worker, "async_session_maker", lambda: _SessionContext())
    monkeypatch.setattr(worker.repository, "pending_outbox_rows", pending)
    monkeypatch.setattr(worker.repository, "get_job", get_job)
    monkeypatch.setattr(worker.repository, "finish_outbox", finish)
    monkeypatch.setattr(
        worker,
        "activity_logger",
        SimpleNamespace(log_activity=lambda **kwargs: delivered.append(kwargs)),
    )

    assert await worker.drain_migration_audits() == 1
    assert delivered[0]["actor_user_id"] == "approver@example.test"
    assert delivered[0]["actor_user_id"] != latest_job.requester
    assert len(finished) == 1
    assert finished[0]["worker_id"]
    assert finished[0]["delivered"] is True


def _instance(
    identity: str,
    *,
    status: str = "Stopped",
    location: str = "member-a",
    autostart: bool = False,
) -> dict[str, Any]:
    config = {
        "limits.cpu": "2",
        "volatile.uuid": identity,
    }
    if autostart:
        config["boot.autostart"] = "true"
    return {
        "name": "workload",
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "status": status,
        "location": location,
        "created_at": "2026-09-22T00:00:00Z",
        "config": dict(config),
        "expanded_config": dict(config),
        "devices": {},
        "expanded_devices": {},
    }


async def _reconcile_original_running_but_stopped(
    monkeypatch: pytest.MonkeyPatch,
    *,
    strategy: str,
    marker: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Exception | None]:
    same_cluster = strategy == "member_relocation"
    source_cluster = "shared-cluster" if same_cluster else "source-cluster"
    target_cluster = source_cluster if same_cluster else "target-cluster"
    source_project = "shared-project" if same_cluster else "source-project"
    target_project = source_project if same_cluster else "target-project"
    source = _instance(
        "shared-identity" if same_cluster else "source-identity",
        status="Stopped",
        location="member-b" if same_cluster else "member-a",
    )
    target = source if same_cluster else _instance("target-identity", status="Stopped")
    if marker is not None and not same_cluster:
        target["config"]["user.orcastra.migration-job"] = marker
    operation_refs = [
        {
            "kind": "transfer",
            "cluster_id": target_cluster,
            "project": target_project,
            "operation_id": "target-operation",
        }
    ]
    safe_result: dict[str, Any] = {
        "transfer_submission_intent": True,
        "contract_hash": "contract-a",
        "original_status": "running",
        "source_identity_hash": executor.instance_identity_fingerprint(source),
        "source_definition_hash": executor.instance_fingerprint(source),
        "source_root_pool": None,
    }
    if same_cluster:
        safe_result["source_root_pool"] = "fast"
        source["expanded_devices"] = {"root": {"type": "disk", "path": "/", "pool": "fast"}}
        safe_result["source_definition_hash"] = executor.instance_fingerprint(source)
    else:
        operation_refs.insert(
            0,
            {
                "kind": "source_transfer",
                "cluster_id": source_cluster,
                "project": source_project,
                "operation_id": "source-operation",
            },
        )
        safe_result.update(
            {
                "source_submission_intent": True,
                "target_submission_intent": True,
            }
        )
    resource = SimpleNamespace(
        resource_id="resource-a",
        resource_type="instance",
        state="syncing",
        source_ref={
            "name": "workload",
            "identity_fingerprint": executor.instance_identity_fingerprint(source),
        },
        target_ref={"name": "workload"},
        operation_refs=operation_refs,
        safe_result=safe_result,
    )
    job = SimpleNamespace(
        job_id="job-a",
        scope="instance",
        intent="move",
        strategy=strategy,
        source_cluster_id=source_cluster,
        source_project=source_project,
        source_resource_name="workload",
        target_cluster_id=target_cluster,
        target_project=target_project,
        target_resource_name="workload",
        target_member="member-b" if same_cluster else None,
        safe_result={},
        external_cutover_required=False,
        external_cutover_confirmed_at=None,
        retention_hours=24,
    )
    source_client = object()
    target_client = source_client if same_cluster else object()
    resource_updates: list[dict[str, Any]] = []
    job_updates: list[dict[str, Any]] = []

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def resource_update(_resource_id: str, **values: Any) -> None:
        resource_updates.append(values)

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> bool:
        job_updates.append(values)
        return True

    async def blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        if function is reconciliation.executor.read_operation:
            return {"settled": True, "failed": False, "err": None}
        if function is reconciliation.executor.read_instance:
            return source if args[0] is source_client else target
        pytest.fail(f"unexpected reconciliation read: {function}")

    async def watched(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"settled": True, "failed": False}

    async def job_plan(_job: Any) -> Any:
        return SimpleNamespace(
            request_payload=_migration_request().model_dump(mode="json"),
            preflight_payload={},
        )

    for module in (reconciliation, reconcile_transfer):
        monkeypatch.setattr(module, "_resources", resources)
        monkeypatch.setattr(module, "_resource_update", resource_update)
        monkeypatch.setattr(module, "_fenced_job_update", job_update)
        monkeypatch.setattr(module, "_blocking", blocking)
    monkeypatch.setattr(reconcile_transfer, "await_operation", watched)
    monkeypatch.setattr(reconcile_transfer, "_job_plan", job_plan)

    error: Exception | None = None
    try:
        await reconciliation._reconcile(job, "worker-a", source_client, target_client)
    except Exception as exc:  # noqa: BLE001
        error = exc
    return resource_updates, job_updates, error


@pytest.mark.asyncio
async def test_reconciliation_resumes_activation_for_a_relocated_running_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_updates, job_updates, error = await _reconcile_original_running_but_stopped(
        monkeypatch, strategy="member_relocation"
    )

    assert error is None
    assert resource_updates[-1]["state"] == "ready"
    # Activation, not a premature success, decides the outcome: it starts the instance again.
    assert job_updates[-1]["phase"] == "target_validating"
    assert job_updates[-1]["status"] == "running"


@pytest.mark.asyncio
async def test_reconciliation_never_adopts_a_staged_target_without_the_job_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _updates, job_updates, error = await _reconcile_original_running_but_stopped(
        monkeypatch, strategy="cross_cluster_pull"
    )

    assert isinstance(error, MigrationAuthorityError)
    assert getattr(error, "code", None) == "target_not_owned"
    assert not any(update.get("phase") == "target_validating" for update in job_updates)


def _cutover_resource(name: str) -> Any:
    return SimpleNamespace(
        resource_id=f"resource-{name}",
        resource_type="instance",
        state="ready",
        source_ref={"name": name},
        target_ref={"name": name},
        safe_result={},
    )


async def _run_activation_until_unknown(
    monkeypatch: pytest.MonkeyPatch,
    *,
    names: list[str],
    target_stays_stopped: bool = False,
    source_restarts_at_final_barrier: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    resources = [_cutover_resource(name) for name in names]
    job = SimpleNamespace(
        job_id="job-cutover",
        intent="move",
        strategy="same_cluster_copy",
        source_cluster_id="shared-cluster",
        target_cluster_id="shared-cluster",
        source_project="source-project",
        target_project="target-project",
        target_member=None,
        safe_result={},
    )
    source_client = object()
    target_client = object()
    transferred: list[tuple[Any, dict[str, Any], Any]] = []
    source_by_name: dict[str, dict[str, Any]] = {}
    target_by_name: dict[str, dict[str, Any]] = {}
    for resource in resources:
        name = str(resource.source_ref["name"])
        source = _instance(f"source-{name}", status="Stopped")
        source["name"] = name
        target = _instance(f"target-{name}", status="Stopped")
        target["name"] = name
        target["config"]["user.orcastra.migration-job"] = job.job_id
        source_by_name[name] = source
        target_by_name[name] = target
        transferred.append(
            (
                resource,
                {
                    "original_status": "running",
                    "source_identity_hash": executor.instance_identity_fingerprint(source),
                    "target_identity_hash": executor.instance_identity_fingerprint(target),
                },
                None,
            )
        )
    resource_updates: list[tuple[str, dict[str, Any]]] = []
    started: set[str] = set()

    async def update(resource_id: str, **values: Any) -> None:
        resource_updates.append((resource_id, values))

    async def lease() -> None:
        return None

    async def job_update(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def settled(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"settled": True, "failed": False}

    async def retention(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("an unverified cutover must never enter retention")

    async def blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        label = str(kwargs.pop("label", ""))
        if function is executor.submit_instance_state:
            started.add(str(kwargs["name"]))
            return f"start-{kwargs['name']}"
        if function is executor.read_instance:
            name = str(kwargs["name"])
            if args[0] is target_client:
                target = dict(target_by_name[name])
                if name in started:
                    target["status"] = "Stopped" if target_stays_stopped else "Running"
                return target
            source = dict(source_by_name[name])
            if (
                source_restarts_at_final_barrier
                and name == names[0]
                and "final_barrier" in label
            ):
                source["status"] = "Running"
            return source
        pytest.fail(f"unexpected activation operation: {function}")

    monkeypatch.setattr(activation, "_resource_update", update)
    monkeypatch.setattr(activation, "_assert_mutation_lease", lease)
    monkeypatch.setattr(activation, "_fenced_job_update", job_update)
    monkeypatch.setattr(activation, "_blocking", blocking)
    monkeypatch.setattr(activation, "await_operation", settled)
    monkeypatch.setattr(activation, "enter_source_retention", retention)

    async def unfenced(_job: Any, _resource: Any, state: dict[str, Any], _client: Any) -> dict[str, Any]:
        # The NIC fence is covered in test_migration_project_runners.py; these tests pin barriers.
        return state

    monkeypatch.setattr(activation, "fence_source_nics", unfenced)

    with pytest.raises(MigrationUnknown):
        await activation.finish_transfer(
            job, "worker-a", source_client, target_client, transferred
        )
    return resource_updates


@pytest.mark.asyncio
async def test_settled_start_is_unknown_when_target_readback_is_still_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = await _run_activation_until_unknown(
        monkeypatch,
        names=["workload"],
        target_stays_stopped=True,
    )

    assert not any(values.get("state") == "retained" for _, values in updates)


@pytest.mark.asyncio
async def test_source_restart_after_target_activation_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = await _run_activation_until_unknown(
        monkeypatch,
        names=["workload"],
        source_restarts_at_final_barrier=True,
    )

    assert not any(values.get("state") == "retained" for _, values in updates)


@pytest.mark.asyncio
async def test_final_multi_resource_barrier_prevents_terminal_promotion_on_source_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = await _run_activation_until_unknown(
        monkeypatch,
        names=["workload-a", "workload-b"],
        source_restarts_at_final_barrier=True,
    )

    assert not any(values.get("state") == "retained" for _, values in updates)
    assert {
        resource_id
        for resource_id, values in updates
        if values.get("state") == "cutting_over"
    } == {"resource-workload-a", "resource-workload-b"}


def test_post_submission_wait_transport_failure_is_migration_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def transport_failure(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("connection reset after accepted operation")

    monkeypatch.setattr(
        executor.snapshot_service,
        "wait_for_operation",
        transport_failure,
    )

    with pytest.raises(MigrationUnknown) as exc:
        executor.wait_operation(
            object(),
            operation_id="operation-a",
            project="source-project",
        )

    assert isinstance(exc.value, executor.MigrationOperationUnknown)
    assert isinstance(exc.value.__cause__, OSError)


def _preflight_identity() -> dict[str, Any]:
    return {
        "engine": "lxd",
        "version": "6.10",
        "extensions": ["cluster_internal_copy"],
        "architectures": ["x86_64"],
    }


def _empty_project_resources(instance: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "instances": [instance] if instance is not None else [],
        "profiles": [{"name": "default", "config": {}, "devices": {}}],
        "networks": [],
        "images": [],
        "custom_volumes": [],
        "storage_buckets": [],
        "network_acls": [],
        "network_zones": [],
        "network_address_sets": [],
        "placement_groups": [],
        "network_forwards": [],
        "network_load_balancers": [],
    }


def test_boot_autostart_never_blocks_a_copy_or_a_member_relocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    source = _instance("source-identity", autostart=True)
    project = {"name": "source-project", "config": {"restricted": "true"}}
    monkeypatch.setattr(preflight, "daemon_identity", lambda _client: _preflight_identity())
    monkeypatch.setattr(preflight, "approved_build", lambda *_args: True)
    monkeypatch.setattr(preflight, "_get_project", lambda *_args: project)
    monkeypatch.setattr(
        preflight,
        "_instance",
        lambda _client, _project, name: source if name == "workload" else None,
    )
    monkeypatch.setattr(
        preflight,
        "_known_project_resources",
        lambda *_args: _empty_project_resources(source),
    )
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight.mobility, "instance_footprint", lambda *_args: 100)
    monkeypatch.setattr(
        preflight.mobility,
        "pool_headroom",
        lambda *_args: {"total": 1_000, "used": 100, "free": 900},
    )

    instance_report = preflight.inspect(
        scope="instance",
        intent="copy",
        source_client=client,
        target_client=client,
        source_cluster="cluster-a",
        source_project="source-project",
        source_name="workload",
        target_cluster="cluster-a",
        target_project="source-project",
        target_name="workload-copy",
        target_member=None,
        target_pool="fast",
        settings=SimpleNamespace(),
        mappings={},
    )
    project_report = preflight.inspect(
        scope="project",
        intent="move",
        source_client=client,
        target_client=client,
        source_cluster="cluster-a",
        source_project="source-project",
        source_name=None,
        target_cluster="cluster-a",
        target_project="source-project",
        target_name=None,
        target_member="member-b",
        target_pool=None,
        settings=SimpleNamespace(),
        mappings={},
    )

    # A copy leaves the source alone, so autostart is not a concern for it.
    assert "source_autostart_not_fenced" not in {
        blocker["code"] for blocker in instance_report.blockers
    }
    # A project member relocation keeps every identity, so autostart stays as it is.
    project_blockers = {blocker["code"]: blocker for blocker in project_report.blockers}
    assert "source_autostart_not_fenced" not in project_blockers
    assert "source_autostart_fenced" not in {
        warning["code"] for warning in project_report.warnings
    }


def test_an_instance_move_fences_autostart_instead_of_refusing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(api=SimpleNamespace(networks=object(), operations=object()))
    source = _instance("source-identity", autostart=True)
    project = {"name": "source-project", "config": {}}
    monkeypatch.setattr(preflight, "daemon_identity", lambda _client: _preflight_identity())
    monkeypatch.setattr(preflight, "approved_build", lambda *_args: True)
    monkeypatch.setattr(preflight, "_get_project", lambda *_args: project)
    monkeypatch.setattr(
        preflight,
        "_instance",
        lambda _client, project_name, name: (
            source if name == "workload" and project_name == "source-project" else None
        ),
    )
    monkeypatch.setattr(preflight, "_profile", lambda *_args: {"config": {}, "devices": {}})
    monkeypatch.setattr(preflight, "_get_collection", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_args: 100)
    monkeypatch.setattr(
        preflight_source, "active_instance_operations", lambda _client, _wanted: set()
    )
    monkeypatch.setattr(
        preflight.mobility,
        "pool_headroom",
        lambda *_args: {"total": 1_000, "used": 100, "free": 900},
    )

    report = preflight.inspect(
        scope="instance",
        intent="move",
        source_client=client,
        target_client=client,
        source_cluster="cluster-a",
        source_project="source-project",
        source_name="workload",
        target_cluster="cluster-a",
        target_project="other-project",
        target_name="workload",
        target_member=None,
        target_pool="fast",
        settings=SimpleNamespace(),
        mappings={},
    )

    assert "source_autostart_not_fenced" not in {issue["code"] for issue in report.blockers}
    assert "source_autostart_fenced" in {issue["code"] for issue in report.warnings}
    # The target keeps the source's autostart setting. Only the retained source loses it.
    assert report.contracts["workload"].expected_config.get("boot.autostart") == "true"


def _migration_request() -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {
                "cluster_id": "source-cluster",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "target-cluster",
                "project": "target-project",
                "resource_name": "workload",
            },
            "mappings": {"storage_pools": {"root": "fast"}},
        }
    )


def _authority() -> Any:
    return SimpleNamespace(
        pin=MigrationAuthorityPin(
            identity=ActorIdentityPin(
                email="operator@example.test",
                issuer="https://identity.example.test/",
                subject="25bbd0bf-278a-477a-8c90-aac4543873d6",
            ),
            provider_uuid="a8f936b1-9221-4101-b326-0eb23def243c",
        ),
        role=SimpleNamespace(value="admin"),
    )


def _actor_fields() -> dict[str, str]:
    authority = _authority()
    return {
        "actor_issuer": authority.pin.identity.issuer,
        "actor_subject": authority.pin.identity.subject,
        "actor_email": authority.pin.identity.email,
        "actor_provider_uuid": authority.pin.provider_uuid,
    }


async def _execute_until_billing_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acknowledgements: list[str],
    billing_gate: Any,
) -> None:
    request = _migration_request()
    approved_report = {
        "blockers": [],
        "resources": [
            {
                "resource_type": "instance",
                "source": {"name": "workload"},
            }
        ],
    }
    plan = SimpleNamespace(
        plan_id="plan-a",
        revision=1,
        **_actor_fields(),
        expires_at=repository.utcnow() + timedelta(minutes=15),
        status="ready",
        request_payload=request.model_dump(mode="json"),
        source_cluster_fingerprint="source-fingerprint",
        target_cluster_fingerprint="target-fingerprint",
        preflight_payload=approved_report,
        updated_at=repository.utcnow(),
    )
    service = planning.MigrationPlanningMixin(
        db=SimpleNamespace(commit=lambda: None),
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    async def no_replay(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_plan(*_args: Any, **_kwargs: Any) -> Any:
        return plan

    async def organizations(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        return 7, 7

    async def fingerprints(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        return "source-fingerprint", "target-fingerprint"

    async def fresh_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return approved_report

    async def clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return object(), object()

    async def blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return billing_gate(function, *args, **kwargs)

    async def must_not_create(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("billing refusal still created a migration job")

    monkeypatch.setattr(planning.repository, "get_job_by_request", no_replay)
    monkeypatch.setattr(planning.repository, "get_plan", get_plan)
    monkeypatch.setattr(planning.repository, "create_job", must_not_create)
    monkeypatch.setattr(service, "_organizations", organizations)
    monkeypatch.setattr(service, "_fingerprints", fingerprints)
    monkeypatch.setattr(service, "_preflight", fresh_preflight)
    monkeypatch.setattr(service, "_require_execution_stage", lambda *_args: None)
    monkeypatch.setattr(service, "_clients", clients)
    monkeypatch.setattr(planning, "_blocking", blocking)

    await service._execute_plan_with_digest(
        "plan-a",
        expected_revision=1,
        idempotency_key="request-a",
        digest="request-digest",
        reason="Approved capacity move",
        acknowledgements=acknowledgements,
        user=object(),
        authority=_authority(),
    )


@pytest.mark.asyncio
async def test_execute_requires_billing_acknowledgement_before_policy_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_policy(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("billing policy was read before acknowledgements were complete")

    with pytest.raises(MigrationConflict, match="billing-impact-reviewed"):
        await _execute_until_billing_gate(
            monkeypatch,
            acknowledgements=["downtime-reviewed", "source-retention-understood"],
            billing_gate=unexpected_policy,
        )


@pytest.mark.asyncio
async def test_execute_enforces_current_billing_policy_before_job_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def deny_policy(function: Any, *_args: Any, **_kwargs: Any) -> None:
        assert function is planning.enforce_migration_billing_policy
        calls.append(function)
        raise MigrationConflict("current billing policy refused the migration")

    with pytest.raises(MigrationConflict, match="current billing policy refused"):
        await _execute_until_billing_gate(
            monkeypatch,
            acknowledgements=[
                "downtime-reviewed",
                "source-retention-understood",
                "billing-impact-reviewed",
            ],
            billing_gate=deny_policy,
        )

    assert calls == [planning.enforce_migration_billing_policy]


class _StopAfterBilling(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_worker_rechecks_billing_and_emits_one_stable_override_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _migration_request()
    acknowledgements = [
        "downtime-reviewed",
        "source-retention-understood",
        "billing-impact-reviewed",
    ]
    job = SimpleNamespace(
        job_id="job-a",
        phase="cutover_pending",
        intent="move",
        strategy="cross_cluster_pull",
        source_project="source-project",
        external_cutover_required=False,
        safe_result={
            "reason": "Approved capacity move",
            "acknowledgements": acknowledgements,
        },
    )
    updates: list[dict[str, Any]] = []
    billing_calls: list[dict[str, Any]] = []

    async def job_plan(_job: Any) -> Any:
        return SimpleNamespace(request_payload=request.model_dump(mode="json"))

    async def resources(_job_id: str) -> list[Any]:
        return []

    async def fresh_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "blockers": [],
            "resources": [
                {
                    "resource_type": "instance",
                    "source": {"name": "workload"},
                }
            ],
        }

    async def blocking(function: Any, *_args: Any, **kwargs: Any) -> int:
        assert function is transfer.enforce_migration_billing_policy
        kwargs.pop("label", None)
        billing_calls.append(kwargs)
        return 1

    async def fenced_update(_job: Any, _worker_id: str, **values: Any) -> bool:
        updates.append(values)
        return True

    async def stop_after_billing(*_args: Any, **_kwargs: Any) -> bool:
        raise _StopAfterBilling

    monkeypatch.setattr(transfer, "_job_plan", job_plan)
    monkeypatch.setattr(transfer, "_resources", resources)
    monkeypatch.setattr(transfer, "_fresh_worker_preflight", fresh_preflight)
    monkeypatch.setattr(transfer, "_blocking", blocking)
    monkeypatch.setattr(transfer, "_fenced_job_update", fenced_update)
    monkeypatch.setattr(transfer, "_ensure_project_target", stop_after_billing)

    with pytest.raises(_StopAfterBilling):
        await transfer._perform_copy_or_cutover(job, "worker-a", object(), object())

    assert len(billing_calls) == 1
    audit_update = updates[0]
    assert audit_update["safe_result"]["billing_override_audited"] is True
    assert audit_update["safe_result"]["billing_impact_workload_count"] == 1
    assert len(audit_update["outbox_events"]) == 1
    event = audit_update["outbox_events"][0]
    assert event["event_type"] == "migration.billing_override"
    assert event == transfer.billing_override_event(
        "job-a",
        workload_count=1,
        reason="Approved capacity move",
        phase="cutover_pending",
    )
    assert event["event_id"] == transfer.billing_override_event(
        "job-a",
        workload_count=99,
        reason="another retry reason",
        phase="reconciling",
    )["event_id"]


@pytest.mark.asyncio
async def test_worker_requires_billing_acknowledgement_before_any_cutover_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _migration_request()
    job = SimpleNamespace(
        job_id="job-a",
        phase="cutover_pending",
        intent="move",
        strategy="cross_cluster_pull",
        source_project="source-project",
        external_cutover_required=False,
        safe_result={
            "reason": "Approved capacity move",
            "acknowledgements": ["downtime-reviewed", "source-retention-understood"],
        },
    )

    async def job_plan(_job: Any) -> Any:
        return SimpleNamespace(request_payload=request.model_dump(mode="json"))

    async def resources(_job_id: str) -> list[Any]:
        return []

    async def fresh_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"blockers": [], "resources": []}

    async def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("worker reached billing read or mutation without durable acknowledgement")

    monkeypatch.setattr(transfer, "_job_plan", job_plan)
    monkeypatch.setattr(transfer, "_resources", resources)
    monkeypatch.setattr(transfer, "_fresh_worker_preflight", fresh_preflight)
    monkeypatch.setattr(transfer, "_blocking", must_not_run)
    monkeypatch.setattr(transfer, "_fenced_job_update", must_not_run)

    with pytest.raises(MigrationConflict, match="billing-impact-reviewed"):
        await transfer._perform_copy_or_cutover(job, "worker-a", object(), object())


@pytest.mark.asyncio
async def test_legacy_copy_adapter_refuses_disabled_admission_before_daemon_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = instance_mobility.get_settings()
    old_center = settings.migration_center_enabled
    old_copy = settings.migration_durable_copy_enabled
    object.__setattr__(settings, "migration_center_enabled", False)
    object.__setattr__(settings, "migration_durable_copy_enabled", False)
    opened_hosts: list[str] = []

    async def client(_service: Any, host: str) -> Any:
        opened_hosts.append(host)
        pytest.fail(f"disabled durable-copy admission opened daemon {host}")

    monkeypatch.setattr(instance_mobility, "_client", client)
    monkeypatch.setattr(instance_mobility, "verify_project_access", lambda _host, project: project)
    body = instance_mobility.MoveRequest(
        host="source-cluster",
        project="source-project",
        target_host="target-cluster",
        target_project="target-project",
        target_name="workload-copy",
        reason="Approved compatibility copy",
    )
    try:
        with pytest.raises(HTTPException) as exc:
            await instance_mobility.copy_instance(
                instance_name="workload",
                body=body,
                lxd_service=object(),
                idempotency_key=None,
                db=object(),  # type: ignore[arg-type]
                _=None,
                user=SimpleNamespace(),
            )
    finally:
        object.__setattr__(settings, "migration_center_enabled", old_center)
        object.__setattr__(settings, "migration_durable_copy_enabled", old_copy)

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "durable_copy_admission_disabled"
    assert opened_hosts == []
