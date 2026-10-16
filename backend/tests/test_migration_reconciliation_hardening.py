from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.migration_schemas import MigrationActionName
from app.services.migrations import (
    actions,
    executor,
    preflight,
    preparation,
    presenters,
    reconciliation,
    transfer,
    worker,
)
from app.services.migrations import target_contract
from app.services.migrations.contracts import MigrationAuthorityError, MigrationUnknown


def _job(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "job_id": "migration-job",
        "scope": "instance",
        "source_cluster_id": "source-cluster",
        "target_cluster_id": "target-cluster",
        "source_project": "source-project",
        "target_project": "target-project",
        "target_member": None,
        "strategy": "cross_cluster_pull",
        "intent": "copy",
        "safe_result": {},
        "external_cutover_required": False,
        "external_cutover_confirmed_at": None,
        "retention_hours": 24,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _instance(*, identity: str, location: str = "member-a") -> dict[str, Any]:
    return {
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "config": {"volatile.uuid": identity},
        "expanded_config": {"volatile.uuid": identity, "limits.cpu": "2"},
        "devices": {"root": {"type": "disk", "path": "/", "pool": "fast"}},
        "expanded_devices": {
            "root": {"type": "disk", "path": "/", "pool": "fast"}
        },
        "status": "Stopped",
        "location": location,
        "created_at": "2026-09-22T10:00:00Z",
    }


def _resource(
    *,
    operation_refs: list[dict[str, Any]],
    safe_result: dict[str, Any] | None = None,
    state: str = "syncing",
    source_identity: str = "source-identity",
) -> SimpleNamespace:
    source = _instance(identity=source_identity)
    state = {
        "original_status": "stopped",
        "source_definition_hash": executor.instance_fingerprint(source),
        "source_snapshot_inventory_hash": "snapshot-hash",
        **(safe_result or {}),
    }
    return SimpleNamespace(
        resource_id="resource-1",
        resource_type="instance",
        source_ref={
            "name": "workload",
            "identity_fingerprint": executor.instance_identity_fingerprint(source),
        },
        target_ref={"name": "workload"},
        operation_refs=operation_refs,
        safe_result=state,
        state=state,
    )


def _install_reconciliation_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resource: SimpleNamespace,
    source: dict[str, Any],
    target: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], object, object]:
    source_client = object()
    target_client = object()
    resource_updates: list[dict[str, Any]] = []
    job_updates: list[dict[str, Any]] = []

    async def resources(_job_id: str) -> list[SimpleNamespace]:
        return [resource]

    async def resource_update(_resource_id: str, **values: Any) -> None:
        resource_updates.append(values)

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> None:
        job_updates.append(values)

    def read_instance(client: Any, *, project: str, name: str) -> dict[str, Any]:
        del project, name
        return source if client is source_client else target

    monkeypatch.setattr(reconciliation, "_resources", resources)
    monkeypatch.setattr(reconciliation, "_resource_update", resource_update)
    monkeypatch.setattr(reconciliation, "_fenced_job_update", job_update)
    monkeypatch.setattr(reconciliation, "target_completion_events", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(executor, "read_instance", read_instance)
    monkeypatch.setattr(
        executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: "snapshot-hash",
    )
    return resource_updates, job_updates, source_client, target_client


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_kind", ["source_transfer", "transfer"])
async def test_cross_cluster_reconciliation_requires_both_transfer_operations(
    monkeypatch: pytest.MonkeyPatch,
    missing_kind: str,
) -> None:
    operation_refs = [
        {
            "kind": "source_transfer",
            "cluster_id": "source-cluster",
            "project": "source-project",
            "operation_id": "source-operation",
        },
        {
            "kind": "transfer",
            "cluster_id": "target-cluster",
            "project": "target-project",
            "operation_id": "target-operation",
        },
    ]
    for operation_ref in operation_refs:
        if operation_ref["kind"] == missing_kind:
            operation_ref["operation_id"] = None
    resource = _resource(
        operation_refs=operation_refs,
        safe_result={
            "transfer_submission_intent": True,
            "source_submission_intent": True,
            "target_submission_intent": True,
        },
    )
    source = _instance(identity="source-identity")
    target = _instance(identity="target-identity")
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=target,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    await reconciliation._reconcile(
        _job(), "worker-1", source_client, target_client
    )

    assert any(update.get("state") == "unknown" for update in resource_updates)
    assert job_updates[-1]["phase"] == "unknown"
    assert job_updates[-1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_cross_cluster_reconciliation_accepts_independent_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _resource(
        operation_refs=[
            {
                "kind": "source_transfer",
                "cluster_id": "source-cluster",
                "project": "source-project",
                "operation_id": "source-operation",
            },
            {
                "kind": "transfer",
                "cluster_id": "target-cluster",
                "project": "target-project",
                "operation_id": "target-operation",
            },
        ],
        safe_result={
            "transfer_submission_intent": True,
            "source_submission_intent": True,
            "target_submission_intent": True,
        },
    )
    source = _instance(identity="source-identity")
    target = _instance(identity="target-identity")
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=target,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    await reconciliation._reconcile(
        _job(), "worker-1", source_client, target_client
    )

    completed = next(update for update in resource_updates if update.get("state") == "completed")
    assert completed["safe_result"]["target_identity_hash"] == (
        executor.instance_identity_fingerprint(target)
    )
    assert job_updates[-1]["phase"] == "completed"
    assert job_updates[-1]["status"] == "success"


@pytest.mark.asyncio
async def test_reconciliation_does_not_adopt_a_same_name_target_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_target = _instance(identity="original-target")
    replacement_target = _instance(identity="replacement-target")
    expected_identity_hash = executor.instance_identity_fingerprint(original_target)
    resource = _resource(
        operation_refs=[
            {
                "kind": "transfer",
                "cluster_id": "target-cluster",
                "project": "target-project",
                "operation_id": "target-operation",
            }
        ],
        safe_result={
            "transfer_submission_intent": True,
            "target_identity_hash": expected_identity_hash,
        },
        state="ready",
    )
    source = _instance(identity="source-identity")
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=replacement_target,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    await reconciliation._reconcile(
        _job(), "worker-1", source_client, target_client
    )

    unknown = next(update for update in resource_updates if update.get("state") == "unknown")
    assert unknown["safe_result"]["target_identity_hash"] == expected_identity_hash
    assert unknown["safe_result"]["target_identity_hash"] != (
        executor.instance_identity_fingerprint(replacement_target)
    )
    assert job_updates[-1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_reconciliation_does_not_adopt_target_after_source_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_source = _instance(identity="original-source")
    replacement_source = _instance(identity="replacement-source")
    target = _instance(identity="target-identity")
    resource = _resource(
        operation_refs=[
            {
                "kind": "transfer",
                "cluster_id": "target-cluster",
                "project": "target-project",
                "operation_id": "target-operation",
            }
        ],
        safe_result={"transfer_submission_intent": True},
        source_identity="original-source",
        state="ready",
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=replacement_source,
            target=target,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    await reconciliation._reconcile(
        _job(), "worker-1", source_client, target_client
    )

    assert executor.instance_identity_fingerprint(original_source) == (
        resource.source_ref["identity_fingerprint"]
    )
    assert not any(update.get("state") == "completed" for update in resource_updates)
    assert resource_updates[-1]["state"] == "unknown"
    assert job_updates[-1]["phase"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["same_cluster_copy", "member_relocation"])
async def test_single_transfer_evidence_remains_valid_for_same_cluster_strategies(
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
) -> None:
    target_member = "member-b" if strategy == "member_relocation" else None
    resource = _resource(
        operation_refs=[
            {
                "kind": "transfer",
                "cluster_id": "shared-cluster",
                "project": "target-project",
                "operation_id": "transfer-operation",
            }
        ],
        safe_result={
            "transfer_submission_intent": True,
            "source_definition_hash": executor.instance_fingerprint(
                _instance(
                    identity="shared-identity",
                    location=target_member or "member-a",
                )
            ),
            "original_status": "stopped",
            "source_root_pool": "fast",
        },
        source_identity="shared-identity",
    )
    source = _instance(identity="shared-identity", location=target_member or "member-a")
    target = _instance(identity="shared-identity", location=target_member or "member-a")
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=target,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    if strategy == "member_relocation":
        resource.operation_refs[0]["project"] = "source-project"

    await reconciliation._reconcile(
        _job(
            source_cluster_id="shared-cluster",
            target_cluster_id="shared-cluster",
            strategy=strategy,
            target_member=target_member,
        ),
        "worker-1",
        source_client,
        target_client,
    )

    if strategy == "member_relocation":
        # Adoption proves the relocation. Activation and its final barrier complete the job.
        assert any(update.get("state") == "ready" for update in resource_updates)
        assert job_updates[-1]["phase"] == "target_validating"
        assert job_updates[-1]["status"] == "running"
    else:
        # A copy accepted before target contracts keeps the operation-evidence rule.
        assert any(update.get("state") == "completed" for update in resource_updates)
        assert job_updates[-1]["phase"] == "completed"
        assert job_updates[-1]["status"] == "success"


@pytest.mark.asyncio
async def test_member_relocation_wrong_member_never_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _instance(identity="shared-identity", location="member-c")
    resource = _resource(
        operation_refs=[
            {
                "kind": "transfer",
                "cluster_id": "shared-cluster",
                "project": "source-project",
                "operation_id": "transfer-operation",
            }
        ],
        safe_result={
            "transfer_submission_intent": True,
            "source_definition_hash": executor.instance_fingerprint(source),
            "original_status": "stopped",
            "source_root_pool": "fast",
        },
        source_identity="shared-identity",
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=source,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    await reconciliation._reconcile(
        _job(
            source_cluster_id="shared-cluster",
            target_cluster_id="shared-cluster",
            strategy="member_relocation",
            target_member="member-b",
        ),
        "worker-1",
        source_client,
        target_client,
    )

    assert not any(update.get("state") == "completed" for update in resource_updates)
    assert resource_updates[-1]["state"] == "unknown"
    assert job_updates[-1]["phase"] == "unknown"
    assert "terminal" not in job_updates[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("current_status", ["Error", "Frozen", "Starting", ""])
async def test_member_relocation_requires_exact_stopped_status_before_success(
    monkeypatch: pytest.MonkeyPatch,
    current_status: str,
) -> None:
    source = _instance(identity="shared-identity", location="member-b")
    source["status"] = current_status
    resource = _resource(
        operation_refs=[
            {
                "kind": "transfer",
                "cluster_id": "shared-cluster",
                "project": "source-project",
                "operation_id": "transfer-operation",
            }
        ],
        safe_result={
            "transfer_submission_intent": True,
            "source_definition_hash": executor.instance_fingerprint(source),
            "original_status": "stopped",
            "source_root_pool": "fast",
        },
        source_identity="shared-identity",
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=source,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )

    await reconciliation._reconcile(
        _job(
            source_cluster_id="shared-cluster",
            target_cluster_id="shared-cluster",
            strategy="member_relocation",
            intent="move",
            target_member="member-b",
        ),
        "worker-1",
        source_client,
        target_client,
    )

    assert not any(update.get("state") == "completed" for update in resource_updates)
    assert resource_updates[-1]["state"] == "unknown"
    assert job_updates[-1]["phase"] == "manual_recovery"
    assert job_updates[-1]["status"] == "manual_recovery"
    assert job_updates[-1]["error_code"] == "target_activation_unverified"


@pytest.mark.asyncio
async def test_member_relocation_can_adopt_exact_identity_and_member_without_operation_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _instance(identity="shared-identity", location="member-b")
    resource = _resource(
        operation_refs=[],
        safe_result={
            "transfer_submission_intent": True,
            "transfer_submission_at": "2026-09-23T12:00:00+00:00",
            "source_definition_hash": executor.instance_fingerprint(source),
            "original_status": "stopped",
            "source_root_pool": "fast",
        },
        source_identity="shared-identity",
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=source,
        )
    )
    monkeypatch.setattr(
        reconciliation,
        "_discover_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            reconciliation.OperationInventoryError("expired")
        ),
    )

    await reconciliation._reconcile(
        _job(
            source_cluster_id="shared-cluster",
            target_cluster_id="shared-cluster",
            strategy="member_relocation",
            target_member="member-b",
        ),
        "worker-1",
        source_client,
        target_client,
    )

    assert any(update.get("state") == "ready" for update in resource_updates)
    assert job_updates[-1]["phase"] == "target_validating"


@pytest.mark.asyncio
async def test_primary_copy_inventory_match_without_correlation_token_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _instance(identity="source-identity")
    target = _instance(identity="target-identity")
    resource = _resource(
        operation_refs=[],
        safe_result={
            "transfer_submission_intent": True,
            "transfer_submission_at": "2026-09-23T12:00:00+00:00",
        },
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=target,
        )
    )
    monkeypatch.setattr(
        reconciliation,
        "_discover_operation",
        lambda *_args, **_kwargs: (
            "matching-operation",
            {"settled": True, "failed": False},
        ),
    )

    await reconciliation._reconcile(
        _job(
            source_cluster_id="shared-cluster",
            target_cluster_id="shared-cluster",
            strategy="same_cluster_copy",
        ),
        "worker-1",
        source_client,
        target_client,
    )

    assert not any(update.get("state") == "completed" for update in resource_updates)
    assert job_updates[-1]["phase"] == "unknown"
    assert "terminal" not in job_updates[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent_key",
    [
        "cleanup_submission_intent",
        "rollback_delete_submission_intent",
        "restore_submission_intent",
    ],
)
async def test_persisted_recovery_intent_resumes_its_runner_without_releasing_locks(
    monkeypatch: pytest.MonkeyPatch,
    intent_key: str,
) -> None:
    source = _instance(identity="source-identity")
    target = _instance(identity="target-identity")
    resource = _resource(
        operation_refs=[],
        safe_result={intent_key: True},
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=target,
        )
    )

    await reconciliation._reconcile(
        _job(phase="unknown"), "worker-1", source_client, target_client
    )

    assert resource_updates == []
    assert "terminal" not in job_updates[-1]
    if intent_key == "restore_submission_intent":
        # Checkpoint restore is not offered, so a persisted restore intent needs an operator.
        assert job_updates[-1]["phase"] == "manual_recovery"
        assert job_updates[-1]["status"] == "manual_recovery"
    else:
        # The convergent runner resumes. Locks are only released by its terminal transaction.
        expected = (
            "cleaning_up" if intent_key == "cleanup_submission_intent" else "rolling_back"
        )
        assert job_updates[-1]["phase"] == expected
        assert job_updates[-1]["status"] == "running"


class _InventoryResponse:
    status_code = 200

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def json(self) -> dict[str, Any]:
        return {"metadata": {"success": self._rows}}


class _InventoryOperations:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get(self, **_kwargs: Any) -> _InventoryResponse:
        return _InventoryResponse(self._rows)


def _inventory_client(rows: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(
        api=SimpleNamespace(
            _api_endpoint="https://cluster.example:8443/1.0",
            operations=_InventoryOperations(rows),
        )
    )


def _operation_row(*, resource_url: str, created_at: str) -> dict[str, Any]:
    return {
        "id": "operation-1",
        "class": "task",
        "description": "Creating instance",
        "created_at": created_at,
        "status_code": 200,
        "project": "target-project",
        "resources": {"instances": [resource_url]},
    }


@pytest.mark.parametrize(
    ("resource_url", "created_at"),
    [
        (
            "https://evil.example:8443/1.0/instances/workload?project=target-project",
            "2026-09-23T12:00:01+00:00",
        ),
        (
            "/1.0/instances/workload?project=target-project",
            "2026-09-23T12:10:00+00:00",
        ),
    ],
)
def test_operation_discovery_rejects_wrong_origin_and_late_operations(
    resource_url: str,
    created_at: str,
) -> None:
    client = _inventory_client(
        [_operation_row(resource_url=resource_url, created_at=created_at)]
    )

    with pytest.raises(reconciliation.OperationInventoryError):
        reconciliation._discover_operation(
            client,
            project="target-project",
            name="workload",
            kind="transfer",
            intent_at="2026-09-23T12:00:00+00:00",
            expected_resources={("target-project", "workload")},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_reconciled", [False, True])
async def test_reconciled_source_preparation_never_replays_a_post(
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_reconciled: bool,
) -> None:
    source = _instance(identity="source-identity")
    source["name"] = "workload"
    state: dict[str, Any] = {
        "original_status": "running" if not checkpoint_reconciled else "stopped",
        "checkpoint_name": None,
        "target_started": False,
        "target_start_attempted": False,
        "source_definition_hash": executor.instance_fingerprint(source),
        "source_identity_hash": executor.instance_identity_fingerprint(source),
        "source_root_pool": "fast",
        "stop_submission_intent": not checkpoint_reconciled,
        "stop_reconciled": not checkpoint_reconciled,
    }
    job = SimpleNamespace(
        job_id="job-reconcile",
        source_project="source-project",
        strategy="member_relocation" if not checkpoint_reconciled else "same_cluster_copy",
    )
    if checkpoint_reconciled:
        state.update(
            {
                "checkpoint_name": "orcastra-migrate-job-reconcile",
                "checkpoint_submission_intent": True,
                "checkpoint_reconciled": True,
                "source_snapshot_inventory_hash": "snapshot-hash",
            }
        )
    resource = SimpleNamespace(
        resource_id="resource-1",
        resource_type="instance",
        source_ref={
            "name": "workload",
            "identity_fingerprint": executor.instance_identity_fingerprint(source),
        },
        safe_result=state,
    )
    updates: list[dict[str, Any]] = []

    async def direct(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    async def update(_resource_id: str, **values: Any) -> None:
        updates.append(values)

    async def job_update(*_args: Any, **_kwargs: Any) -> None:
        return None

    def read_instance(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return source

    def unexpected_post(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("reconciled source preparation replayed a daemon mutation")

    monkeypatch.setattr(preparation, "_blocking", direct)
    monkeypatch.setattr(preparation, "_resource_update", update)
    monkeypatch.setattr(preparation, "_fenced_job_update", job_update)
    monkeypatch.setattr(preparation.executor, "read_instance", read_instance)
    monkeypatch.setattr(
        preparation.executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: "snapshot-hash",
    )
    monkeypatch.setattr(
        preparation.executor, "submit_instance_state", unexpected_post
    )
    monkeypatch.setattr(preparation.executor, "submit_checkpoint", unexpected_post)

    prepared = await preparation._prepare_sources(
        job,
        "worker-1",
        object(),
        [resource],
        checkpoint_requested=checkpoint_reconciled,
    )

    assert prepared["resource-1"]["original_status"] == state["original_status"]
    assert updates


@pytest.mark.asyncio
async def test_member_relocation_checkpoint_gate_runs_before_any_daemon_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        job_id="job-member-checkpoint",
        phase="queued",
        strategy="member_relocation",
        scope="instance",
        intent="move",
        safe_result={},
        locked_by="worker-1",
        lease_expires_at=None,
    )
    updates: list[dict[str, Any]] = []

    async def plan(_job: Any) -> Any:
        return SimpleNamespace(checkpoint_requested=True)

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> None:
        updates.append(values)

    async def resources(_job_id: str) -> list[Any]:
        return [SimpleNamespace(operation_refs=[], safe_result={})]

    async def unexpected_clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        pytest.fail("checkpoint gate accessed a daemon before refusing execution")

    monkeypatch.setattr(worker, "_job_plan", plan)
    monkeypatch.setattr(worker, "_resources", resources)
    monkeypatch.setattr(worker, "_fenced_job_update", job_update)
    monkeypatch.setattr(worker, "_authoritative_clients", unexpected_clients)

    await worker._run_claimed(job, "worker-1", allow_mutation=True)

    assert updates[-1]["phase"] == "failed"
    assert updates[-1]["terminal"] is True
    assert updates[-1]["error_code"] == (
        "member_relocation_checkpoint_unrecoverable"
    )


@pytest.mark.asyncio
async def test_claimed_copy_rejects_a_stale_checkpoint_plan_before_daemon_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        job_id="job-copy-checkpoint",
        phase="queued",
        intent="copy",
        strategy="same_cluster_copy",
        scope="instance",
        safe_result={},
        locked_by="worker-1",
        lease_expires_at=None,
    )
    updates: list[dict[str, Any]] = []

    async def plan(_job: Any) -> Any:
        return SimpleNamespace(checkpoint_requested=True)

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> None:
        updates.append(values)

    async def resources(_job_id: str) -> list[Any]:
        return [SimpleNamespace(operation_refs=[], safe_result={})]

    async def unexpected_clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        pytest.fail("stale copy checkpoint plan reached daemon client acquisition")

    monkeypatch.setattr(worker, "_job_plan", plan)
    monkeypatch.setattr(worker, "_resources", resources)
    monkeypatch.setattr(worker, "_fenced_job_update", job_update)
    monkeypatch.setattr(worker, "_authoritative_clients", unexpected_clients)

    await worker._run_claimed(job, "worker-1", allow_mutation=True)

    assert updates[-1]["phase"] == "failed"
    assert updates[-1]["status"] == "failure"
    assert updates[-1]["terminal"] is True
    assert updates[-1]["error_code"] == "copy_checkpoint_unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "strategy", "scope", "checkpoint_requested"),
    [
        ("copy", "same_cluster_copy", "instance", True),
        ("move", "member_relocation", "instance", True),
        ("move", "project_relocation", "project", True),
    ],
)
async def test_invalid_contract_after_submission_keeps_resource_locks(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    strategy: str,
    scope: str,
    checkpoint_requested: bool,
) -> None:
    job = SimpleNamespace(
        job_id="job-partially-submitted",
        phase="initial_sync",
        intent=intent,
        strategy=strategy,
        scope=scope,
        safe_result={},
        locked_by="worker-1",
        lease_expires_at=None,
    )
    resource = SimpleNamespace(
        operation_refs=[{"kind": "transfer", "operation_id": "operation-1"}],
        safe_result={"transfer_submission_intent": True},
    )
    updates: list[dict[str, Any]] = []

    async def plan(_job: Any) -> Any:
        return SimpleNamespace(checkpoint_requested=checkpoint_requested)

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> None:
        updates.append(values)

    async def unexpected_clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        pytest.fail("invalid persisted contract reached daemon client acquisition")

    monkeypatch.setattr(worker, "_job_plan", plan)
    monkeypatch.setattr(worker, "_resources", resources)
    monkeypatch.setattr(worker, "_fenced_job_update", job_update)
    monkeypatch.setattr(worker, "_authoritative_clients", unexpected_clients)

    await worker._run_claimed(job, "worker-1", allow_mutation=True)

    assert updates[-1]["phase"] == "manual_recovery"
    assert updates[-1]["status"] == "manual_recovery"
    assert updates[-1]["terminal"] is False
    assert updates[-1]["error_code"].endswith("_mutation_state_ambiguous")


@pytest.mark.parametrize(
    "phase",
    ["source_retained", "cleanup_due", "rollback_available", "cleanup_blocked"],
)
def test_retention_actions_follow_the_retained_phase(phase: str) -> None:
    job = SimpleNamespace(
        phase=phase,
        status="waiting",
        lease_expires_at=None,
        scope="instance",
        intent="move",
        strategy="same_cluster_copy",
    )
    resource = SimpleNamespace(
        resource_type="instance",
        state="retained",
        operation_refs=[],
        safe_result={
            "checkpoint_name": "checkpoint-a",
            "target_started": False,
            "target_start_attempted": False,
        },
    )

    actions = presenters._available_actions(job, [resource])

    retained = phase in {"source_retained", "cleanup_due", "cleanup_blocked"}
    assert (MigrationActionName.ROLLBACK in actions) is retained
    assert (MigrationActionName.CLEANUP in actions) is retained
    assert MigrationActionName.RESTORE_CHECKPOINT not in actions
    assert (MigrationActionName.RELEASE in actions) is (phase == "cleanup_blocked")


def test_member_relocation_and_copy_never_offer_retention_actions() -> None:
    for intent, strategy in (("move", "member_relocation"), ("copy", "same_cluster_copy")):
        job = SimpleNamespace(
            phase="source_retained",
            status="waiting",
            lease_expires_at=None,
            scope="instance",
            intent=intent,
            strategy=strategy,
        )
        actions = presenters._available_actions(job, [])
        assert MigrationActionName.ROLLBACK not in actions
        assert MigrationActionName.CLEANUP not in actions


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["stop_source", "checkpoint"])
async def test_pretransfer_reconciliation_returns_only_a_proven_continuation(
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    source = _instance(identity="source-identity")
    state: dict[str, Any] = {
        "original_status": "running" if step == "stop_source" else "stopped",
        "source_identity_hash": executor.instance_identity_fingerprint(source),
        "source_definition_hash": executor.instance_fingerprint(source),
        f"{'stop' if step == 'stop_source' else 'checkpoint'}_submission_intent": True,
    }
    if step == "checkpoint":
        state["checkpoint_name"] = "checkpoint-a"
    resource = SimpleNamespace(
        resource_id="resource-1",
        resource_type="instance",
        state="preparing",
        source_ref={"name": "workload"},
        target_ref={"name": "workload-copy"},
        operation_refs=[
            {
                "kind": step,
                "cluster_id": "source-cluster",
                "project": "source-project",
                "operation_id": "operation-1",
            }
        ],
        safe_result=state,
    )
    job = _job()
    updates: list[dict[str, Any]] = []
    job_updates: list[dict[str, Any]] = []

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def resource_update(_resource_id: str, **values: Any) -> None:
        updates.append(values)

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> None:
        job_updates.append(values)

    monkeypatch.setattr(reconciliation, "_resources", resources)
    monkeypatch.setattr(reconciliation, "_resource_update", resource_update)
    monkeypatch.setattr(reconciliation, "_fenced_job_update", job_update)
    monkeypatch.setattr(
        reconciliation.executor,
        "read_instance",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        reconciliation.executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )
    monkeypatch.setattr(
        reconciliation.executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: "snapshot-hash",
    )
    monkeypatch.setattr(
        reconciliation,
        "_snapshot_names",
        lambda *_args, **_kwargs: {"checkpoint-a"},
    )

    await reconciliation._reconcile(job, "worker-1", object(), object())

    proof_key = "stop_reconciled" if step == "stop_source" else "checkpoint_reconciled"
    assert any(update.get("safe_result", {}).get(proof_key) for update in updates)
    assert job_updates[-1]["phase"] == (
        "quiescing" if step == "stop_source" else "checkpointing"
    )
    assert job_updates[-1]["status"] == "running"
    assert "terminal" not in job_updates[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_status", "target_snapshot_hash", "target_identity_available"),
    [
        ("Running", "snapshot-hash", True),
        ("Stopped", "different-snapshots", True),
        ("Stopped", "snapshot-hash", False),
    ],
)
async def test_recovered_copy_requires_stopped_target_and_exact_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    target_status: str,
    target_snapshot_hash: str,
    target_identity_available: bool,
) -> None:
    source = _instance(identity="shared-identity")
    target = _instance(identity="target-identity")
    target["status"] = target_status
    if not target_identity_available:
        target.pop("created_at", None)
        target["config"] = {}
        target["expanded_config"] = {"limits.cpu": "2"}
    resource = _resource(
        operation_refs=[
            {
                "kind": "transfer",
                "cluster_id": "shared-cluster",
                "project": "target-project",
                "operation_id": "transfer-operation",
            }
        ],
        safe_result={"transfer_submission_intent": True},
        source_identity="shared-identity",
    )
    resource_updates, job_updates, source_client, target_client = (
        _install_reconciliation_fakes(
            monkeypatch,
            resource=resource,
            source=source,
            target=target,
        )
    )
    monkeypatch.setattr(
        executor,
        "read_operation",
        lambda *_args, **_kwargs: {"settled": True, "failed": False},
    )
    monkeypatch.setattr(
        executor,
        "snapshot_inventory_fingerprint",
        lambda client, **_kwargs: (
            "snapshot-hash" if client is source_client else target_snapshot_hash
        ),
    )

    await reconciliation._reconcile(
        _job(
            source_cluster_id="shared-cluster",
            target_cluster_id="shared-cluster",
            strategy="same_cluster_copy",
        ),
        "worker-1",
        source_client,
        target_client,
    )

    assert not any(update.get("state") == "completed" for update in resource_updates)
    assert job_updates[-1]["phase"] == "unknown"


async def _run_copy_transfer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_status: str = "Stopped",
    target_status: str = "Stopped",
    target_snapshot_hash: str = "snapshot-hash",
) -> tuple[list[dict[str, Any]], int, Exception | None]:
    source = _instance(identity="source-identity")
    source["status"] = source_status
    target = _instance(identity="target-identity")
    target["status"] = target_status
    target["config"]["user.orcastra.migration-job"] = "migration-job"
    source_client = object()
    target_client = object()
    contract = target_contract.build_contract(
        source=source,
        target_profiles={"default": {"config": {}, "devices": {}}},
        profile_mapping={},
        network_mapping={},
        usable_networks=[],
        root_pool="fast",
        intent="copy",
    )
    resource = SimpleNamespace(
        resource_id="resource-1",
        resource_type="instance",
        source_ref={
            "name": "workload",
            "identity_fingerprint": executor.instance_identity_fingerprint(source),
        },
        target_ref={"name": "workload-copy", "contract": contract.summary()},
        operation_refs=[],
        safe_result={},
        state="pending",
    )
    prepared = {
        "resource-1": {
            "original_status": "stopped",
            "source_identity_hash": executor.instance_identity_fingerprint(source),
            "source_definition_hash": executor.instance_fingerprint(source),
            "source_snapshot_inventory_hash": "snapshot-hash",
        }
    }
    job = _job(
        source_cluster_id="shared-cluster",
        target_cluster_id="shared-cluster",
        strategy="same_cluster_copy",
    )
    updates: list[dict[str, Any]] = []
    submissions = 0

    async def update(_resource_id: str, **values: Any) -> None:
        updates.append(values)

    async def lease() -> None:
        return None

    async def settled(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"settled": True, "failed": False}

    async def blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal submissions
        kwargs.pop("label", None)
        if function is executor.read_instance:
            return source if args[0] is source_client else target
        if function is transfer.build_live_contract:
            return contract, source
        if function is executor.snapshot_inventory_fingerprint:
            return (
                "snapshot-hash"
                if args[0] is source_client
                else target_snapshot_hash
            )
        if function is executor.submit_copy_instance:
            submissions += 1
            assert kwargs["definition"]["config"]["user.orcastra.migration-job"] == job.job_id
            return "copy", "transfer-operation", target_client
        pytest.fail(f"unexpected copy transfer call: {function}")

    monkeypatch.setattr(transfer, "_resource_update", update)
    monkeypatch.setattr(transfer, "_assert_mutation_lease", lease)
    monkeypatch.setattr(transfer, "_blocking", blocking)
    monkeypatch.setattr(transfer, "await_operation", settled)
    error: Exception | None = None
    try:
        await transfer._transfer_resources(
            job,
            "worker-1",
            source_client,
            target_client,
            [resource],
            prepared,
            request=SimpleNamespace(
                mappings=SimpleNamespace(
                    model_dump=lambda **_kwargs: {},
                    storage_pools={"root": "fast"},
                )
            ),
            root_pool_name="fast",
        )
    except Exception as exc:  # noqa: BLE001 - returned for focused assertions
        error = exc
    return updates, submissions, error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_status", "target_snapshot_hash", "succeeds"),
    [
        ("Stopped", "snapshot-hash", True),
        ("Running", "snapshot-hash", False),
        ("Frozen", "snapshot-hash", False),
        ("Stopped", "different-snapshots", False),
    ],
)
async def test_copy_transfer_pins_and_verifies_status_and_snapshot_inventory(
    monkeypatch: pytest.MonkeyPatch,
    target_status: str,
    target_snapshot_hash: str,
    succeeds: bool,
) -> None:
    updates, submissions, error = await _run_copy_transfer(
        monkeypatch,
        target_status=target_status,
        target_snapshot_hash=target_snapshot_hash,
    )

    assert submissions == 1
    assert (error is None) is succeeds
    assert any(
        update.get("safe_result", {}).get("source_snapshot_inventory_hash")
        == "snapshot-hash"
        for update in updates
    )
    # Transfer ends at "ready". Activation and its barriers decide completion.
    assert any(update.get("state") == "ready" for update in updates) is succeeds
    if not succeeds:
        assert isinstance(error, MigrationUnknown)


@pytest.mark.asyncio
async def test_copy_source_state_race_is_rejected_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates, submissions, error = await _run_copy_transfer(
        monkeypatch,
        source_status="Running",
    )

    assert submissions == 0
    assert updates == []
    assert isinstance(error, MigrationAuthorityError)


async def _run_failed_copy_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    operation_outcome: dict[str, bool],
    target: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    approved_source: dict[str, Any] | None = None,
    operation_refs: list[dict[str, Any]] | None = None,
    inventory_rows: list[dict[str, Any]] | None = None,
    cross_cluster_source_only: bool = False,
    same_project: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    authoritative_source = source or _instance(identity="source-identity")
    pinned_source = approved_source or authoritative_source
    source_identity = executor.instance_identity_fingerprint(pinned_source)
    source_definition = executor.instance_fingerprint(pinned_source)
    kind = "source_transfer" if cross_cluster_source_only else "transfer"
    target_project = "source-project" if same_project else "target-project"
    refs = operation_refs
    if refs is None:
        refs = [
            {
                "kind": kind,
                "cluster_id": (
                    "source-cluster" if cross_cluster_source_only else "shared-cluster"
                ),
                "project": (
                    "source-project" if cross_cluster_source_only else target_project
                ),
                "operation_id": "failed-operation",
            }
        ]
    state = {
        "original_status": "stopped",
        "source_definition_hash": source_definition,
        "source_identity_hash": source_identity,
        "source_snapshot_inventory_hash": "snapshot-hash",
        "transfer_submission_intent": True,
        "transfer_submission_at": "2026-09-23T12:00:00+00:00",
    }
    if cross_cluster_source_only:
        state.update(
            {
                "source_submission_intent": True,
                "source_submission_at": "2026-09-23T12:00:00+00:00",
            }
        )
    resource = SimpleNamespace(
        resource_id="resource-1",
        resource_type="instance",
        source_ref={
            "name": "workload",
            "identity_fingerprint": source_identity,
            "definition_fingerprint": preflight._instance_preflight_fingerprint(
                pinned_source
            ),
        },
        target_ref={"name": "workload-copy"},
        operation_refs=refs,
        safe_result=state,
        state="unknown",
    )
    source_client = _inventory_client(inventory_rows or [])
    target_client = (
        _inventory_client([]) if cross_cluster_source_only else source_client
    )
    resource_updates: list[dict[str, Any]] = []
    job_updates: list[dict[str, Any]] = []

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def resource_update(_resource_id: str, **values: Any) -> None:
        resource_updates.append(values)

    async def job_update(_job: Any, _worker_id: str, **values: Any) -> None:
        job_updates.append(values)

    def read_instance(_client: Any, *, project: str, name: str) -> Any:
        if project == "source-project" and name == "workload":
            return authoritative_source
        return target

    monkeypatch.setattr(reconciliation, "_resources", resources)
    monkeypatch.setattr(reconciliation, "_resource_update", resource_update)
    monkeypatch.setattr(reconciliation, "_fenced_job_update", job_update)
    monkeypatch.setattr(reconciliation.executor, "read_instance", read_instance)
    monkeypatch.setattr(
        reconciliation.executor,
        "read_operation",
        lambda *_args, **_kwargs: operation_outcome,
    )
    monkeypatch.setattr(
        reconciliation.executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: "snapshot-hash",
    )
    monkeypatch.setattr(
        reconciliation,
        "target_completion_events",
        lambda *_args, **_kwargs: [],
    )

    job = _job(
        source_cluster_id=(
            "source-cluster" if cross_cluster_source_only else "shared-cluster"
        ),
        target_cluster_id=(
            "target-cluster" if cross_cluster_source_only else "shared-cluster"
        ),
        strategy=(
            "cross_cluster_pull"
            if cross_cluster_source_only
            else "same_cluster_copy"
        ),
        target_project=target_project,
    )
    await reconciliation._reconcile(job, "worker-1", source_client, target_client)
    return resource_updates, job_updates


@pytest.mark.asyncio
async def test_failed_copy_with_absent_target_releases_locks_after_exact_failed_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_updates, job_updates = await _run_failed_copy_reconciliation(
        monkeypatch,
        operation_outcome={"settled": True, "failed": True},
    )

    assert resource_updates[-1]["state"] == "failed"
    assert job_updates[-1]["phase"] == "failed"
    assert job_updates[-1]["status"] == "failure"
    assert job_updates[-1]["terminal"] is True
    assert job_updates[-1]["safe_result"]["outcome"] == (
        "copy_failed_without_target"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_outcome", "target_present", "source_drift"),
    [
        ({"settled": False, "failed": False}, False, False),
        ({"settled": True, "failed": False}, False, False),
        ({"settled": True, "failed": True}, True, False),
        ({"settled": True, "failed": True}, False, True),
    ],
)
async def test_failed_copy_refuses_release_when_authoritative_proof_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    operation_outcome: dict[str, bool],
    target_present: bool,
    source_drift: bool,
) -> None:
    approved_source = _instance(identity="source-identity")
    current_source = (
        _instance(identity="replacement-source") if source_drift else approved_source
    )
    target = _instance(identity="target-identity") if target_present else None
    resource_updates, job_updates = await _run_failed_copy_reconciliation(
        monkeypatch,
        operation_outcome=operation_outcome,
        target=target,
        source=current_source,
        approved_source=approved_source,
    )

    assert not any(update.get("terminal") is True for update in job_updates)
    assert job_updates[-1]["phase"] == "unknown"
    assert any(update.get("state") == "unknown" for update in resource_updates)


@pytest.mark.asyncio
async def test_failed_copy_never_adopts_an_unreferenced_history_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _operation_row(
        resource_url="/1.0/instances/workload-copy?project=target-project",
        created_at="2026-09-23T12:00:01+00:00",
    )
    row["status_code"] = 400
    resource_updates, job_updates = await _run_failed_copy_reconciliation(
        monkeypatch,
        operation_outcome={"settled": True, "failed": True},
        operation_refs=[],
        inventory_rows=[row],
    )

    assert not any(update.get("terminal") is True for update in job_updates)
    assert job_updates[-1]["phase"] == "unknown"
    assert any(update.get("state") == "unknown" for update in resource_updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("active_name", ["workload", "workload-copy"])
async def test_same_project_failed_copy_keeps_locks_for_active_source_or_target_operation(
    monkeypatch: pytest.MonkeyPatch,
    active_name: str,
) -> None:
    row = _operation_row(
        resource_url=(
            f"/1.0/instances/{active_name}?project=source-project"
        ),
        created_at="2026-09-23T12:00:01+00:00",
    )
    row["status_code"] = 103
    row["project"] = "source-project"
    resource_updates, job_updates = await _run_failed_copy_reconciliation(
        monkeypatch,
        operation_outcome={"settled": True, "failed": True},
        inventory_rows=[row],
        same_project=True,
    )

    assert not any(update.get("terminal") is True for update in job_updates)
    assert job_updates[-1]["phase"] == "unknown"
    assert any(update.get("state") == "unknown" for update in resource_updates)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_outcome", "terminal"),
    [
        ({"settled": True, "failed": True}, True),
        ({"settled": False, "failed": False}, False),
        ({"settled": True, "failed": False}, False),
    ],
)
async def test_lost_cross_cluster_source_handoff_releases_only_after_exact_failure(
    monkeypatch: pytest.MonkeyPatch,
    operation_outcome: dict[str, bool],
    terminal: bool,
) -> None:
    _resource_updates, job_updates = await _run_failed_copy_reconciliation(
        monkeypatch,
        operation_outcome=operation_outcome,
        cross_cluster_source_only=True,
    )

    assert any(update.get("terminal") is True for update in job_updates) is terminal
    assert job_updates[-1]["phase"] == ("failed" if terminal else "unknown")


def test_reconciliation_backoff_is_durable_deterministic_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reconciliation.repository, "utcnow", lambda: now)
    job = _job(job_id="job-backoff", safe_result={})

    first = reconciliation._reconciliation_backoff_values(
        job,
        error_code="ambiguous_daemon_state",
        error_message="Authoritative readback is inconclusive.",
    )
    repeated = reconciliation._reconciliation_backoff_values(
        job,
        error_code="ambiguous_daemon_state",
        error_message="Authoritative readback is inconclusive.",
    )

    assert first["status"] == "unknown"
    assert first["safe_result"]["reconciliation_streak"] == 1
    assert first["lease_expires_at"] > now
    assert first["lease_expires_at"] == repeated["lease_expires_at"]
    assert first["safe_result"]["reconciliation_retry_delay_seconds"] == (
        repeated["safe_result"]["reconciliation_retry_delay_seconds"]
    )

    exhausted = reconciliation._reconciliation_backoff_values(
        _job(
            job_id="job-backoff",
            safe_result={
                "reconciliation_streak": (
                    reconciliation._MAX_AUTOMATIC_RECONCILIATIONS - 1
                )
            },
        ),
        error_code="ambiguous_daemon_state",
        error_message="Authoritative readback is inconclusive.",
    )

    assert exhausted["phase"] == "manual_recovery"
    assert exhausted["status"] == "manual_recovery"
    assert exhausted["lease_expires_at"] is None
    assert exhausted["error_code"] == "reconciliation_attempts_exhausted"
    assert "terminal" not in exhausted


def test_explicit_recheck_resets_delay_and_starts_a_new_bounded_cycle() -> None:
    reset = actions._reset_reconciliation_cycle(
        {
            "verified": False,
            "reconciliation_cycle": 2,
            "reconciliation_streak": 5,
            "reconciliation_retry_delay_seconds": 900,
        }
    )

    assert reset["verified"] is False
    assert reset["reconciliation_cycle"] == 3
    assert reset["reconciliation_streak"] == 0
    assert "reconciliation_retry_delay_seconds" not in reset

    next_attempt = reconciliation._reconciliation_backoff_values(
        _job(job_id="job-backoff", safe_result=reset),
        error_code="ambiguous_daemon_state",
        error_message="Still inconclusive.",
    )
    assert next_attempt["safe_result"]["reconciliation_streak"] == 1
