"""Regression guards for destructive and replay-sensitive migration paths."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations import (
    context,
    executor,
    planning,
    preflight,
    preparation,
    reconciliation,
    recovery_actions,
    repository,
)
from app.services.migrations.authority import ActorIdentityPin, MigrationAuthorityPin
from app.services.migrations.contracts import MigrationAuthorityError, MigrationConflict
from app.services.migrations.plan_contracts import (
    _preflight_payload,
    approval_report_hash,
)
from app.services.migrations.security import canonical_json_hash


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _SnapshotNode:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def get(self, **_kwargs: Any) -> _Response:
        return _Response({"metadata": self._rows})


class _InstanceNode:
    def __init__(self, rows: list[Any]) -> None:
        self.snapshots = _SnapshotNode(rows)


class _InstanceCollection:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __getitem__(self, _name: str) -> _InstanceNode:
        return _InstanceNode(self._rows)


def _snapshot_client(rows: list[Any]) -> Any:
    return SimpleNamespace(api=SimpleNamespace(instances=_InstanceCollection(rows)))


def _instance(
    identity: str,
    *,
    status: str = "Stopped",
    cpu_limit: str = "2",
) -> dict[str, Any]:
    return {
        "name": "workload",
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "status": status,
        "created_at": "2026-09-22T00:00:00Z",
        "config": {
            "limits.cpu": cpu_limit,
            "volatile.uuid": identity,
        },
        "expanded_config": {
            "limits.cpu": cpu_limit,
            "volatile.uuid": identity,
        },
        "expanded_devices": {},
    }


def _request(*, intent: str = "move") -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": intent,
            "source": {
                "cluster_id": "cluster-a",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "cluster-a",
                "project": "target-project",
                "resource_name": "workload-copy",
            },
            "mappings": {"storage_pools": {"root": "fast"}},
        }
    )


def test_mutation_without_operation_identity_is_unknown() -> None:
    response = _Response({"type": "async", "status_code": 202, "metadata": {}})

    with pytest.raises(executor.MigrationOperationUnknown, match="operation identity"):
        executor._operation(response, object())

    with pytest.raises(executor.MigrationOperationUnknown, match="operation identity"):
        executor.wait_operation(object(), operation_id=None, project="default")


def test_immutable_identity_distinguishes_same_definition_replacement() -> None:
    original = _instance("original-uuid")
    replacement = _instance("replacement-uuid")

    assert executor.instance_fingerprint(original) == executor.instance_fingerprint(
        replacement
    )
    assert executor.instances_equivalent(original, replacement) is True
    assert executor.instance_identity_fingerprint(
        original
    ) != executor.instance_identity_fingerprint(replacement)


def test_snapshot_inventory_fingerprint_detects_new_snapshot() -> None:
    original = [
        {
            "name": "checkpoint-a",
            "created_at": "2026-09-22T00:00:00Z",
            "expires_at": None,
            "stateful": False,
        }
    ]
    with_new_snapshot = [
        *original,
        {
            "name": "operator-created",
            "created_at": "2026-09-22T01:00:00Z",
            "expires_at": None,
            "stateful": False,
        },
    ]

    original_hash = executor.snapshot_inventory_fingerprint(
        _snapshot_client(original), project="source-project", name="workload"
    )
    changed_hash = executor.snapshot_inventory_fingerprint(
        _snapshot_client(with_new_snapshot),
        project="source-project",
        name="workload",
    )

    assert original_hash != changed_hash


async def _run_cleanup_until_blocked(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_source: dict[str, Any],
    expected_source: dict[str, Any],
    current_snapshot_hash: str,
    expected_snapshot_hash: str,
) -> dict[str, Any]:
    source_client = object()
    target_client = object()
    target = _instance("target-uuid")
    resource = SimpleNamespace(
        resource_id="resource-a",
        resource_type="instance",
        state="retained",
        source_ref={"name": "workload"},
        target_ref={"name": "workload-copy"},
        safe_result={
            "source_identity_hash": executor.instance_identity_fingerprint(
                expected_source
            ),
            "source_snapshot_inventory_hash": expected_snapshot_hash,
            "source_definition_hash": executor.instance_fingerprint(expected_source),
            "target_identity_hash": executor.instance_identity_fingerprint(target),
            "target_started": False,
        },
    )
    job = SimpleNamespace(
        job_id="job-a",
        scope="instance",
        source_project="source-project",
        target_project="target-project",
        source_cluster_id="cluster-a",
        retention_deadline=repository.utcnow(),
    )
    fenced_updates: list[dict[str, Any]] = []

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def direct_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    def read_instance(client: Any, *, project: str, name: str) -> dict[str, Any]:
        del project, name
        return current_source if client is source_client else target

    async def fenced_update(_job: Any, _worker_id: str, **values: Any) -> bool:
        fenced_updates.append(values)
        return True

    def unexpected_delete(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("cleanup reached destructive deletion after drift")

    monkeypatch.setattr(recovery_actions, "_resources", resources)
    monkeypatch.setattr(recovery_actions, "_blocking", direct_call)
    monkeypatch.setattr(recovery_actions, "_fenced_job_update", fenced_update)
    monkeypatch.setattr(recovery_actions.executor, "read_instance", read_instance)
    monkeypatch.setattr(
        recovery_actions.executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: current_snapshot_hash,
    )
    monkeypatch.setattr(
        recovery_actions.executor, "submit_delete_instance", unexpected_delete
    )

    await recovery_actions._cleanup(job, "worker-a", source_client, target_client)

    assert len(fenced_updates) == 1
    return fenced_updates[0]


@pytest.mark.asyncio
async def test_cleanup_blocks_same_definition_source_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = await _run_cleanup_until_blocked(
        monkeypatch,
        current_source=_instance("replacement-uuid"),
        expected_source=_instance("original-uuid"),
        current_snapshot_hash="snapshots-a",
        expected_snapshot_hash="snapshots-a",
    )

    assert update["phase"] == "cleanup_blocked"
    assert update["error_code"] == "source_identity_drift"


@pytest.mark.asyncio
async def test_cleanup_blocks_snapshot_inventory_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _instance("original-uuid")
    update = await _run_cleanup_until_blocked(
        monkeypatch,
        current_source=source,
        expected_source=source,
        current_snapshot_hash="snapshots-after",
        expected_snapshot_hash="snapshots-before",
    )

    assert update["phase"] == "cleanup_blocked"
    assert update["error_code"] == "source_snapshot_drift"


def _recovery_resource(
    source: dict[str, Any], target: dict[str, Any]
) -> SimpleNamespace:
    return SimpleNamespace(
        resource_id="resource-recovery",
        resource_type="instance",
        state="retained",
        source_ref={"name": "workload"},
        target_ref={"name": "workload-copy"},
        safe_result={
            "original_status": "stopped",
            "checkpoint_name": "orcastra-migrate-job",
            "source_identity_hash": executor.instance_identity_fingerprint(source),
            "source_definition_hash": executor.instance_fingerprint(source),
            "source_snapshot_inventory_hash": "snapshot-inventory",
            "target_identity_hash": executor.instance_identity_fingerprint(target),
            "target_definition_hash": executor.instance_fingerprint(target),
            "target_started": False,
            "target_start_attempted": False,
        },
    )


def _recovery_job(**updates: Any) -> SimpleNamespace:
    values = {
        "job_id": "job-recovery",
        "strategy": "same_cluster_copy",
        "source_cluster_id": "cluster-a",
        "target_cluster_id": "cluster-a",
        "source_project": "source-project",
        "target_project": "target-project",
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_rollback_never_deletes_a_same_name_target_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client = object()
    target_client = object()
    source = _instance("source-uuid")
    expected_target = _instance("target-uuid")
    replacement_target = _instance("replacement-target-uuid")
    resource = _recovery_resource(source, expected_target)

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def direct_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    def read_instance(client: Any, **_kwargs: Any) -> dict[str, Any]:
        return source if client is source_client else replacement_target

    def unexpected_delete(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("rollback deleted a replacement target")

    monkeypatch.setattr(recovery_actions, "_resources", resources)
    monkeypatch.setattr(recovery_actions, "_blocking", direct_call)
    monkeypatch.setattr(recovery_actions.executor, "read_instance", read_instance)
    monkeypatch.setattr(
        recovery_actions.executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: "snapshot-inventory",
    )
    monkeypatch.setattr(
        recovery_actions.executor, "submit_delete_instance", unexpected_delete
    )

    with pytest.raises(RuntimeError, match="immutable identity"):
        await recovery_actions._rollback(
            _recovery_job(), "worker-a", source_client, target_client
        )


@pytest.mark.asyncio
async def test_restore_never_overwrites_a_same_name_source_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client = object()
    target_client = object()
    expected_source = _instance("source-uuid")
    replacement_source = _instance("replacement-source-uuid")
    target = _instance("target-uuid")
    resource = _recovery_resource(expected_source, target)

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def direct_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    def read_instance(client: Any, **_kwargs: Any) -> Any:
        return replacement_source if client is source_client else None

    def unexpected_restore(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("restore overwrote a replacement source")

    monkeypatch.setattr(recovery_actions, "_resources", resources)
    monkeypatch.setattr(recovery_actions, "_blocking", direct_call)
    monkeypatch.setattr(recovery_actions.executor, "read_instance", read_instance)
    monkeypatch.setattr(
        recovery_actions.executor,
        "snapshot_inventory_fingerprint",
        lambda *_args, **_kwargs: "snapshot-inventory",
    )
    monkeypatch.setattr(
        recovery_actions.executor, "submit_restore_checkpoint", unexpected_restore
    )

    with pytest.raises(RuntimeError, match="immutable identity"):
        await recovery_actions._restore(
            _recovery_job(), "worker-a", source_client, target_client
        )


@pytest.mark.asyncio
async def test_member_relocation_never_uses_delete_as_rollback() -> None:
    with pytest.raises(RuntimeError, match="reverse relocation plan"):
        await recovery_actions._rollback(
            _recovery_job(strategy="member_relocation"),
            "worker-a",
            object(),
            object(),
        )


def _authority() -> Any:
    return SimpleNamespace(
        pin=MigrationAuthorityPin(
            identity=ActorIdentityPin(
                email="operator@example.com",
                issuer="https://identity.example/",
                subject="operator-subject",
            ),
            provider_uuid="2f54d4e3-6029-4e9a-bdec-b93f9880053d",
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


@pytest.mark.asyncio
async def test_execute_replay_reauthorizes_before_disclosing_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    replay = SimpleNamespace(payload_hash="digest-a", **_actor_fields())
    calls: list[str] = []

    async def get_replay(*_args: Any, **_kwargs: Any) -> Any:
        return replay

    async def pinned_plan(_job: Any) -> Any:
        calls.append("plan")
        return SimpleNamespace(**_actor_fields())

    async def denied(_job: Any, *, current_authority: Any) -> Any:
        assert current_authority is authority
        calls.append("authority")
        raise MigrationAuthorityError("revoked")

    monkeypatch.setattr(planning.repository, "get_job_by_request", get_replay)
    monkeypatch.setattr(context, "_job_plan", pinned_plan)
    monkeypatch.setattr(context, "_authoritative_clients", denied)
    service = planning.MigrationPlanningMixin(
        db=object(), lxd_service=object(), settings=SimpleNamespace()
    )

    with pytest.raises(MigrationAuthorityError, match="revoked"):
        await service._execute_plan_with_digest(
            "plan-a",
            expected_revision=1,
            idempotency_key="request-a",
            digest="digest-a",
            reason="Approved operation",
            acknowledgements=[],
            user=object(),
            authority=authority,
        )

    assert calls == ["plan", "authority"]


@pytest.mark.asyncio
async def test_compatibility_copy_replay_reauthorizes_before_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    request = _request(intent="copy")
    reason = "Approved legacy copy"
    acknowledgements = ["source-reviewed"]
    digest = canonical_json_hash(
        {
            "contract": "legacy-instance-copy-v1",
            "request": request.model_dump(mode="json"),
            "reason": reason,
            "acknowledgements": acknowledgements,
        }
    )
    replay = SimpleNamespace(payload_hash=digest, **_actor_fields())
    calls: list[str] = []

    class Verifier:
        def __init__(self, _settings: Any) -> None:
            pass

        async def pin_user(self, _user: Any) -> Any:
            return authority

    async def get_replay(*_args: Any, **_kwargs: Any) -> Any:
        return replay

    async def pinned_plan(_job: Any) -> Any:
        calls.append("plan")
        return SimpleNamespace(**_actor_fields())

    async def denied(_job: Any, *, current_authority: Any) -> Any:
        assert current_authority is authority
        calls.append("authority")
        raise MigrationAuthorityError("revoked")

    monkeypatch.setattr(planning, "AuthentikAuthorityVerifier", Verifier)
    monkeypatch.setattr(planning.repository, "get_job_by_request", get_replay)
    monkeypatch.setattr(context, "_job_plan", pinned_plan)
    monkeypatch.setattr(context, "_authoritative_clients", denied)
    service = planning.MigrationPlanningMixin(
        db=object(), lxd_service=object(), settings=SimpleNamespace()
    )

    with pytest.raises(MigrationAuthorityError, match="revoked"):
        await service.execute_compatibility_copy(
            request,
            idempotency_key="legacy-copy-a",
            reason=reason,
            acknowledgements=acknowledgements,
            user=object(),
            requester_role="admin",
        )

    assert calls == ["plan", "authority"]


def _configure_project_creation_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MigrationPlanCreateRequest, Any, dict[str, Any], dict[str, Any]]:
    client = object()
    request = _request(intent="copy")
    state: dict[str, Any] = {
        "target_project": None,
        "source_instance": _instance("source-uuid"),
    }
    source_project = {
        "name": "source-project",
        "config": {
            "restricted": "true",
            **{
                feature: "true"
                for feature in preflight._PROJECT_DEPENDENCY_FEATURES
            },
        },
    }
    identity = {
        "engine": "lxd",
        "version": "6.10",
        "extensions": ["container_copy_project"],
        "architectures": ["x86_64"],
    }
    empty_resources = {
        "instances": [],
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

    monkeypatch.setattr(preflight, "daemon_identity", lambda _client: identity)
    monkeypatch.setattr(preflight, "approved_build", lambda *_args: True)
    monkeypatch.setattr(preflight, "_check_active_operations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        preflight,
        "_get_project",
        lambda _client, project: (
            source_project if project == "source-project" else state["target_project"]
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_instance",
        lambda _client, project, _name: (
            state["source_instance"] if project == "source-project" else None
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_profile",
        lambda *_args, **_kwargs: {"config": {}, "devices": {}},
    )
    monkeypatch.setattr(
        preflight, "_known_project_resources", lambda *_args: empty_resources
    )
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_args: 100)
    monkeypatch.setattr(
        preflight.mobility,
        "pool_headroom",
        lambda *_args: {"total": 1_000, "used": 100, "free": 900},
    )
    return request, client, state, source_project


def _inspect_for_request(request: MigrationPlanCreateRequest, client: Any) -> Any:
    return preflight.inspect(
        scope="instance",
        intent=request.intent.value,
        source_client=client,
        target_client=client,
        source_cluster=request.source.cluster_id,
        source_project=request.source.project,
        source_name=request.source.resource_name,
        target_cluster=request.target.cluster_id,
        target_project=request.target.project,
        target_name=request.target.resource_name,
        target_member=None,
        target_pool="fast",
        settings=SimpleNamespace(),
        mappings=request.mappings.model_dump(mode="json"),
    )


async def _fresh_report(
    monkeypatch: pytest.MonkeyPatch,
    *,
    request: MigrationPlanCreateRequest,
    client: Any,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    plan = SimpleNamespace(
        request_payload=request.model_dump(mode="json"),
        preflight_payload=baseline,
    )
    job = SimpleNamespace(
        scope="instance",
        intent=request.intent.value,
        source_cluster_id=request.source.cluster_id,
        source_project=request.source.project,
        source_resource_name=request.source.resource_name,
        target_cluster_id=request.target.cluster_id,
        target_project=request.target.project,
        target_resource_name=request.target.resource_name,
        target_member=None,
    )

    async def job_plan(_job: Any) -> Any:
        return plan

    async def direct_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    monkeypatch.setattr(context, "_job_plan", job_plan)
    monkeypatch.setattr(context, "_blocking", direct_call)
    monkeypatch.setattr(context, "get_settings", lambda: SimpleNamespace())
    return await context._fresh_worker_preflight(job, client, client)


@pytest.mark.asyncio
async def test_created_target_project_does_not_false_positive_graph_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, client, state, source_project = _configure_project_creation_preflight(
        monkeypatch
    )
    initial = _inspect_for_request(request, client)
    baseline = _preflight_payload(initial, request)
    assert not baseline["blockers"]
    assert initial.capabilities["target_project_create"] is True

    state["target_project"] = {
        "name": "target-project",
        "config": dict(source_project["config"]),
    }
    report = await _fresh_report(
        monkeypatch, request=request, client=client, baseline=baseline
    )

    assert "resource_graph_changed" not in {
        blocker["code"] for blocker in report["blockers"]
    }


@pytest.mark.asyncio
async def test_fresh_preflight_detects_approval_relevant_instance_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, client, state, source_project = _configure_project_creation_preflight(
        monkeypatch
    )
    baseline = _preflight_payload(_inspect_for_request(request, client), request)
    state["target_project"] = {
        "name": "target-project",
        "config": dict(source_project["config"]),
    }
    state["source_instance"] = _instance("source-uuid", cpu_limit="4")

    report = await _fresh_report(
        monkeypatch, request=request, client=client, baseline=baseline
    )

    assert "resource_graph_changed" in {
        blocker["code"] for blocker in report["blockers"]
    }


def test_preflight_pins_hashed_immutable_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, client, state, _source_project = _configure_project_creation_preflight(
        monkeypatch
    )

    report = _preflight_payload(_inspect_for_request(request, client), request)
    instance_resource = next(
        resource
        for resource in report["resources"]
        if resource["resource_type"] == "instance"
    )

    assert instance_resource["source"]["identity_fingerprint"] == (
        executor.instance_identity_fingerprint(state["source_instance"])
    )
    assert "source-uuid" not in str(instance_resource)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("strategy", "cross_cluster_pull"),
        (
            "capabilities",
            [{"code": "same_engine", "supported": False, "reason": "drift"}],
        ),
        ("daemon_capabilities", {"source": {"extensions": ["new-extension"]}}),
        (
            "resources",
            [
                {
                    "resource_key": "instance:workload",
                    "source": {"project_fingerprint": "changed-policy"},
                }
            ],
        ),
        ("mappings", {"storage_pools": {"root": "different-pool"}}),
        ("blockers", [{"code": "new-security-blocker"}]),
        ("rollback_contract", {"source_retained": False}),
    ],
)
def test_approval_hash_covers_every_approval_dimension(
    field: str,
    changed: Any,
) -> None:
    baseline = {
        "strategy": "same_cluster_copy",
        "capabilities": [
            {"code": "same_engine", "supported": True, "reason": None}
        ],
        "daemon_capabilities": {"source": {"extensions": ["copy"]}},
        "resources": [
            {
                "resource_key": "instance:workload",
                "source": {"project_fingerprint": "approved-policy"},
            }
        ],
        "mappings": {"storage_pools": {"root": "fast"}},
        "blockers": [],
        "warnings": [],
        "rollback_contract": {"source_retained": True},
    }
    modified = {**baseline, field: changed}

    assert approval_report_hash(modified) != approval_report_hash(baseline)


def test_approval_hash_ignores_derived_hash_and_unordered_collection_order() -> None:
    baseline = {
        "capabilities": [
            {"code": "rollback", "supported": True},
            {"code": "checkpoint", "supported": True},
        ],
        "resources": [
            {"resource_key": "instance:b"},
            {"resource_key": "instance:a"},
        ],
        "blockers": [{"code": "b"}, {"code": "a"}],
        "warnings": [{"code": "d"}, {"code": "c"}],
    }
    reordered = {
        **baseline,
        "approval_hash": "stale-derived-value",
        "capabilities": list(reversed(baseline["capabilities"])),
        "resources": list(reversed(baseline["resources"])),
        "blockers": list(reversed(baseline["blockers"])),
        "warnings": list(reversed(baseline["warnings"])),
    }

    assert approval_report_hash(reordered) == approval_report_hash(baseline)


def test_preflight_blocks_source_without_immutable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, client, state, _source_project = _configure_project_creation_preflight(
        monkeypatch
    )
    source = dict(state["source_instance"])
    source.pop("created_at", None)
    source["config"] = {"limits.cpu": "2"}
    source["expanded_config"] = {"limits.cpu": "2"}
    state["source_instance"] = source

    result = _inspect_for_request(request, client)

    assert "source_identity_unavailable" in {
        blocker["code"] for blocker in result.blockers
    }


@pytest.mark.asyncio
async def test_execute_revises_plan_when_immutable_source_identity_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    original_identity = executor.instance_identity_fingerprint(_instance("source-a"))
    replacement_identity = executor.instance_identity_fingerprint(_instance("source-b"))
    approved = {
        "blockers": [],
        "resources": [
            {
                "resource_key": "instance:workload",
                "resource_type": "instance",
                "source": {
                    "name": "workload",
                    "identity_fingerprint": original_identity,
                },
                "target": {"name": "workload-copy"},
                "dependency_keys": [],
            }
        ],
    }
    fresh = {
        **approved,
        "resources": [
            {
                **approved["resources"][0],
                "source": {
                    "name": "workload",
                    "identity_fingerprint": replacement_identity,
                },
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
        preflight_payload=approved,
    )
    revised_reports: list[dict[str, Any]] = []

    async def no_replay(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_plan(*_args: Any, **_kwargs: Any) -> Any:
        return plan

    async def organizations(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        return 7, 7

    async def fingerprints(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        return "source-fingerprint", "target-fingerprint"

    async def revise(_db: Any, row: Any, *, report: dict[str, Any]) -> Any:
        revised_reports.append(report)
        row.revision += 1
        return row

    async def fresh_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return fresh

    async def unexpected_create(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("identity drift created a migration job")

    service = planning.MigrationPlanningMixin(
        db=object(), lxd_service=object(), settings=SimpleNamespace()
    )
    monkeypatch.setattr(planning.repository, "get_job_by_request", no_replay)
    monkeypatch.setattr(planning.repository, "get_plan", get_plan)
    monkeypatch.setattr(planning.repository, "revise_plan_preflight", revise)
    monkeypatch.setattr(planning.repository, "create_job", unexpected_create)
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
            reason="Approved operation",
            acknowledgements=[],
            user=object(),
            authority=_authority(),
        )

    assert revised_reports == [fresh]


@pytest.mark.asyncio
async def test_preflight_revision_is_durable_and_refreshes_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = repository.utcnow()
    row = SimpleNamespace(
        revision=4,
        status="ready",
        preflight_payload={"blockers": []},
        expires_at=before,
        updated_at=before,
    )

    class Database:
        def __init__(self) -> None:
            self.commits = 0
            self.refreshes = 0

        async def commit(self) -> None:
            self.commits += 1

        async def refresh(self, refreshed: Any) -> None:
            assert refreshed is row
            self.refreshes += 1

    database = Database()
    report = {"blockers": [{"code": "source_identity_drift"}]}
    monkeypatch.setattr(
        repository,
        "get_settings",
        lambda: SimpleNamespace(migration_plan_ttl_seconds=900),
    )

    revised = await repository.revise_plan_preflight(database, row, report=report)

    assert revised is row
    assert row.revision == 5
    assert row.status == "blocked"
    assert row.preflight_payload == report
    assert row.expires_at >= before + timedelta(minutes=14)
    assert database.commits == 1
    assert database.refreshes == 1


@pytest.mark.asyncio
async def test_source_replacement_is_refused_before_cutover_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _instance("source-a", status="Running")
    replacement = _instance("source-b", status="Running")
    resource = SimpleNamespace(
        resource_id="resource-a",
        resource_type="instance",
        source_ref={
            "name": "workload",
            "identity_fingerprint": executor.instance_identity_fingerprint(original),
        },
    )
    job = SimpleNamespace(source_project="source-project")

    async def direct_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    monkeypatch.setattr(preparation, "_blocking", direct_call)
    monkeypatch.setattr(
        preparation.executor,
        "read_instance",
        lambda *_args, **_kwargs: replacement,
    )
    monkeypatch.setattr(
        preparation.executor,
        "submit_instance_state",
        lambda *_args, **_kwargs: pytest.fail("source replacement was stopped"),
    )

    with pytest.raises(MigrationAuthorityError, match="approved immutable identity"):
        await preparation._prepare_sources(
            job,
            "worker-a",
            object(),
            [resource],
            checkpoint_requested=False,
        )


@pytest.mark.asyncio
async def test_successful_reconciliation_emits_stable_completion_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _instance("source-uuid")
    target = _instance("target-uuid")
    resource = SimpleNamespace(
        resource_id="resource-a",
        resource_type="instance",
        state="syncing",
        source_ref={
            "name": "workload",
            "identity_fingerprint": executor.instance_identity_fingerprint(source),
        },
        target_ref={"name": "workload-copy"},
        operation_refs=[
            {
                "kind": "source_transfer",
                "cluster_id": "source-cluster",
                "project": "source-project",
                "operation_id": "source-operation-a",
            },
            {
                "kind": "transfer",
                "cluster_id": "target-cluster",
                "project": "target-project",
                "operation_id": "operation-a",
            },
        ],
        safe_result={
            "original_status": "stopped",
            "source_definition_hash": executor.instance_fingerprint(source),
            "source_snapshot_inventory_hash": "snapshot-inventory",
            "source_submission_intent": True,
            "target_submission_intent": True,
        },
    )
    job = SimpleNamespace(
        job_id="job-a",
        scope="instance",
        intent="copy",
        strategy="cross_cluster_pull",
        source_cluster_id="source-cluster",
        source_project="source-project",
        source_resource_name="workload",
        target_cluster_id="target-cluster",
        target_project="target-project",
        target_resource_name="workload-copy",
        safe_result={},
    )
    source_client = object()
    target_client = object()
    job_updates: list[dict[str, Any]] = []

    async def resources(_job_id: str) -> list[Any]:
        return [resource]

    async def resource_update(_resource_id: str, **_values: Any) -> None:
        return None

    async def fenced_update(_job: Any, _worker_id: str, **values: Any) -> bool:
        job_updates.append(values)
        return True

    async def blocking(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        if function is reconciliation.executor.read_operation:
            return {"settled": True, "failed": False, "err": None}
        if function is reconciliation.executor.read_instance:
            return source if args[0] is source_client else target
        if function is reconciliation.executor.snapshot_inventory_fingerprint:
            return "snapshot-inventory"
        pytest.fail(f"unexpected reconciliation read: {function}")

    monkeypatch.setattr(reconciliation, "_resources", resources)
    monkeypatch.setattr(reconciliation, "_resource_update", resource_update)
    monkeypatch.setattr(reconciliation, "_fenced_job_update", fenced_update)
    monkeypatch.setattr(reconciliation, "_blocking", blocking)

    await reconciliation._reconcile(job, "worker-a", source_client, target_client)

    assert len(job_updates) == 1
    update = job_updates[0]
    assert update["phase"] == "completed"
    assert update["status"] == "success"
    assert update["terminal"] is True
    assert update["safe_result"]["verified"] is True
    assert [event["channel"] for event in update["outbox_events"]] == [
        "cache",
        "webhook",
        "billing",
    ]
    assert len({event["event_id"] for event in update["outbox_events"]}) == 3
