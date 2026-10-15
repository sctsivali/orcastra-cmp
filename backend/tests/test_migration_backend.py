from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.dependencies import check_read_only_mode
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.routers import migrations as migration_router
from app.services.migrations import preflight_source, target_admission
from app.services.migrations import billing, executor, orchestrator, preflight, repository
from app.services.migrations.completion import target_completion_events
from app.services.migrations.contracts import MigrationAuthorityError
from app.services.migrations.security import sanitize_error


class _Response:
    def __init__(self, metadata: Any) -> None:
        self._metadata = metadata

    def json(self) -> dict[str, Any]:
        return {"metadata": self._metadata}


class _OperationNode:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self._metadata = metadata

    def get(self, **_kwargs: Any) -> _Response:
        return _Response(self._metadata)


class _OperationCollection:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self._metadata = metadata

    def __getitem__(self, _key: str) -> _OperationNode:
        return _OperationNode(self._metadata)


def _operation_client(metadata: dict[str, Any]) -> Any:
    return SimpleNamespace(
        api=SimpleNamespace(operations=_OperationCollection(metadata))
    )


@pytest.mark.parametrize(
    ("status_code", "status", "settled", "failed"),
    [
        (200, "Running", True, False),
        (400, "Success", True, True),
        (401, "Success", True, True),
        (103, "Success", False, False),
        (None, "Cancelled", True, True),
    ],
)
def test_operation_reconciliation_prefers_numeric_contract(
    status_code: int | None,
    status: str,
    settled: bool,
    failed: bool,
) -> None:
    metadata = {"status": status, "err": ""}
    if status_code is not None:
        metadata["status_code"] = status_code

    result = executor.read_operation(
        _operation_client(metadata), operation_id="op-1", project="default"
    )

    assert result["settled"] is settled
    assert result["failed"] is failed


def test_instance_equivalence_includes_expanded_devices() -> None:
    source = {
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "expanded_config": {"limits.cpu": "2"},
        "expanded_devices": {
            "root": {"type": "disk", "path": "/", "pool": "source"},
            "eth0": {"type": "nic", "network": "private"},
        },
    }
    target = {
        **source,
        "expanded_devices": {
            "root": {"type": "disk", "path": "/", "pool": "target"},
            "eth0": {"type": "nic", "network": "other"},
        },
    }

    assert executor.instances_equivalent(source, target) is False
    target["expanded_devices"]["eth0"]["network"] = "private"
    assert executor.instances_equivalent(source, target) is True


def _request(**overrides: Any) -> MigrationPlanCreateRequest:
    body: dict[str, Any] = {
        "scope": "instance",
        "intent": "move",
        "source": {
            "cluster_id": "source",
            "project": "source-project",
            "resource_name": "workload",
        },
        "target": {
            "cluster_id": "target",
            "project": "target-project",
            "resource_name": "workload",
        },
        "mappings": {"storage_pools": {"root": "fast"}},
    }
    body.update(overrides)
    return MigrationPlanCreateRequest.model_validate(body)


def _preflight_result(resources: list[dict[str, Any]]) -> preflight.PreflightResult:
    return preflight.PreflightResult(
        strategy="cross_cluster_pull",
        downtime="cold",
        capabilities={
            "source": {"engine": "lxd"},
            "target": {"engine": "lxd"},
            "checkpoint": True,
            "rollback": True,
            "live": False,
            "incremental_refresh": False,
        },
        resources=resources,
    )


def test_dependency_graph_never_contains_a_dangling_project_key() -> None:
    result = _preflight_result(
        [
            {
                "resource_type": "instance",
                "source_name": "workload",
                "target_name": "workload",
            }
        ]
    )
    payload = orchestrator._preflight_payload(result, _request())
    keys = {resource["resource_key"] for resource in payload["resources"]}

    assert all(
        dependency in keys
        for resource in payload["resources"]
        for dependency in resource["dependency_keys"]
    )
    assert payload["resources"][0]["dependency_keys"] == []


def test_copy_contract_does_not_promise_retained_source_rollback() -> None:
    result = _preflight_result([])
    request = _request(intent="copy")

    payload = orchestrator._preflight_payload(result, request)

    rollback = next(
        capability
        for capability in payload["capabilities"]
        if capability["code"] == "rollback"
    )
    assert rollback == {
        "code": "rollback",
        "supported": False,
        "reason": "Copy creates an independent destination and has no rollback window.",
    }
    assert payload["rollback_contract"] == {
        "supported": False,
        "source_retained": False,
        "retention_hours": None,
        "automatic_cleanup": False,
        "target_writes_discarded_on_rollback": False,
        "manual_recovery_only": False,
        "reason": (
            "Copy creates an independent destination and has no retained-source rollback."
        ),
    }


def test_staged_move_contract_offers_rollback_and_retention_cleanup() -> None:
    result = _preflight_result([])
    result.capabilities["automatic_cleanup"] = True
    payload = orchestrator._preflight_payload(result, _request())

    rollback = next(
        capability
        for capability in payload["capabilities"]
        if capability["code"] == "rollback"
    )
    assert rollback["supported"] is True
    assert "discarded" in str(rollback["reason"])
    contract = payload["rollback_contract"]
    assert contract["supported"] is True
    assert contract["source_retained"] is True
    assert contract["retention_hours"] == 24
    assert contract["automatic_cleanup"] is True
    assert contract["target_writes_discarded_on_rollback"] is True
    assert contract["manual_recovery_only"] is False


def test_staged_move_contract_without_automatic_cleanup_requires_finalize() -> None:
    payload = orchestrator._preflight_payload(_preflight_result([]), _request())

    assert payload["rollback_contract"]["automatic_cleanup"] is False
    assert "Finalize deletes it" in payload["rollback_contract"]["reason"]


def test_member_relocation_acknowledgements_do_not_claim_a_retained_source() -> None:
    required = billing.required_acknowledgements(
        intent="move",
        strategy="member_relocation",
        external_cutover_required=False,
    )

    assert required == {"downtime-reviewed", "billing-impact-reviewed"}


def test_staged_move_acknowledgements_require_retention_understanding() -> None:
    required = billing.required_acknowledgements(
        intent="move",
        strategy="cross_cluster_pull",
        external_cutover_required=False,
    )

    assert "source-retention-understood" in required


def test_dependency_graph_links_an_explicit_project_resource() -> None:
    result = _preflight_result(
        [
            {
                "resource_type": "project",
                "source_name": "source-project",
                "target_name": "target-project",
            },
            {
                "resource_type": "instance",
                "source_name": "workload",
                "target_name": "workload",
            },
        ]
    )
    payload = orchestrator._preflight_payload(result, _request())

    assert payload["resources"][1]["dependency_keys"] == [
        "project:source-project"
    ]


def _usage() -> Any:
    limits = {
        "cpu": None,
        "memory_bytes": None,
        "instances": 10,
        "containers": None,
        "virtual_machines": None,
        "disk_bytes": None,
    }
    return SimpleNamespace(
        limits=limits,
        used={key: 1 for key in limits},
        complete=True,
        complete_fields=set(limits),
    )


def _patch_instance_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_project: dict[str, Any],
    target_project: dict[str, Any] | None,
    source_instance: dict[str, Any],
    target_resources: dict[str, list[Any]] | None = None,
    source_profile: dict[str, Any] | None = None,
    target_profile: dict[str, Any] | None = None,
    free: int = 1_000,
) -> tuple[Any, Any]:
    source_client = SimpleNamespace(api=SimpleNamespace(networks=object(), operations=object()))
    target_client = SimpleNamespace(api=SimpleNamespace(networks=object(), operations=object()))
    source_instance = {
        "status": "Stopped",
        "created_at": "2026-01-01T00:00:00Z",
        **source_instance,
    }
    identity = {
        "engine": "lxd",
        "version": "6.10",
        "extensions": ["container_copy_project"],
        "architectures": ["x86_64"],
    }
    monkeypatch.setattr(preflight, "daemon_identity", lambda _client: identity)
    monkeypatch.setattr(preflight, "approved_build", lambda *_args: True)
    monkeypatch.setattr(
        preflight,
        "_get_project",
        lambda client, _project: (
            source_project if client is source_client else target_project
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_instance",
        lambda client, _project, _name: source_instance if client is source_client else None,
    )
    monkeypatch.setattr(
        preflight,
        "_profile",
        lambda client, _project, _name: (
            source_profile if client is source_client else target_profile
        ),
    )
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_args: 100)
    monkeypatch.setattr(
        preflight_source, "active_instance_operations", lambda _client, _wanted: set()
    )
    monkeypatch.setattr(
        preflight,
        "_get_collection",
        lambda _node, *, project, resource: [
            {"name": "lxdbr0", "managed": True, "type": "bridge", "status": "Created"}
        ],
    )
    monkeypatch.setattr(target_admission, "read_project_usage", lambda *_args: _usage())
    monkeypatch.setattr(
        preflight.mobility,
        "pool_headroom",
        lambda *_args: {"total": 2_000, "used": 2_000 - free, "free": free},
    )
    empty = {
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
    monkeypatch.setattr(
        preflight,
        "_known_project_resources",
        lambda client, _project: (
            empty if client is source_client else (target_resources or empty)
        ),
    )
    monkeypatch.setattr(
        preflight,
        "api_endpoint",
        lambda client: (
            "https://source.example:8443"
            if client is source_client
            else "https://target.example:8443"
        ),
    )
    return source_client, target_client


def _inspect_instance(source_client: Any, target_client: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "scope": "instance",
        "intent": "move",
        "source_client": source_client,
        "target_client": target_client,
        "source_cluster": "source",
        "source_project": "source-project",
        "source_name": "workload",
        "target_cluster": "target",
        "target_project": "target-project",
        "target_name": "workload",
        "target_member": None,
        "target_pool": "fast",
        "settings": SimpleNamespace(),
        "mappings": {"storage_pools": {"root": "fast"}},
    }
    values.update(overrides)
    return preflight.inspect(**values)


def test_cross_project_move_admits_a_populated_target_with_its_own_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {"restricted": "true", "limits.instances": "2"}},
        target_project={"config": {"restricted": "false", "limits.instances": "10"}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "expanded_config": {},
            "expanded_devices": {},
        },
        target_resources={
            "instances": [{"name": "another"}],
            "profiles": [{"name": "default", "config": {}, "devices": {}}],
        },
        source_profile={"config": {}, "devices": {}},
        target_profile={"config": {}, "devices": {}},
    )

    result = _inspect_instance(source_client, target_client)
    codes = {issue["code"] for issue in result.blockers}

    assert "target_project_policy_mismatch" not in codes
    assert "target_project_not_empty" not in codes
    assert not codes


def test_cross_project_move_pins_profile_values_the_target_would_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {}},
        target_project={"config": {}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "config": {},
            "devices": {},
            "expanded_config": {"limits.cpu": "2"},
            "expanded_devices": {},
        },
        source_profile={"config": {"limits.cpu": "2"}, "devices": {}},
        target_profile={"config": {"limits.cpu": "4"}, "devices": {}},
    )

    result = _inspect_instance(
        source_client,
        target_client,
        source_cluster="same",
        target_cluster="same",
    )

    assert "profile_drift" not in {issue["code"] for issue in result.blockers}
    contract = result.contracts["workload"]
    assert contract.config["limits.cpu"] == "2"
    assert "limits.cpu" in contract.pinned_config
    assert contract.expected_config["limits.cpu"] == "2"


def test_known_insufficient_storage_is_a_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {}},
        target_project={"config": {}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "expanded_config": {},
            "expanded_devices": {},
        },
        source_profile={"config": {}, "devices": {}},
        target_profile={"config": {}, "devices": {}},
        free=50,
    )

    result = _inspect_instance(source_client, target_client)

    assert "target_pool_insufficient_capacity" in {
        issue["code"] for issue in result.blockers
    }


def test_unsupported_mapping_groups_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {}},
        target_project={"config": {}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "expanded_config": {},
            "expanded_devices": {},
        },
        source_profile={"config": {}, "devices": {}},
        target_profile={"config": {}, "devices": {}},
    )

    result = _inspect_instance(
        source_client,
        target_client,
        mappings={
            "storage_pools": {"root": "fast"},
            "networks": {"old": "new"},
            "volumes": {"data": "data"},
        },
    )
    codes = {issue["code"] for issue in result.blockers}

    assert "unsupported_volumes_mapping" in codes
    assert "unsupported_networks_mapping" not in codes


def test_identical_instance_source_and_target_is_a_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, _target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {}},
        target_project={"config": {}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "location": "node-a",
            "expanded_config": {},
            "expanded_devices": {},
        },
        source_profile={"config": {}, "devices": {}},
        target_profile={"config": {}, "devices": {}},
    )

    result = _inspect_instance(
        source_client,
        source_client,
        source_cluster="same",
        target_cluster="same",
        target_project="source-project",
        target_name="workload",
        target_member=None,
    )

    assert "source_target_identical" in {
        issue["code"] for issue in result.blockers
    }


def test_member_relocation_rejects_current_source_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, _target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {}},
        target_project={"config": {}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "location": "node-a",
            "expanded_config": {},
            "expanded_devices": {},
        },
        source_profile={"config": {}, "devices": {}},
        target_profile={"config": {}, "devices": {}},
    )

    result = _inspect_instance(
        source_client,
        source_client,
        source_cluster="same",
        target_cluster="same",
        target_project="source-project",
        target_member="node-a",
    )

    assert "target_member_is_source" in {
        issue["code"] for issue in result.blockers
    }


def test_copy_with_target_member_never_selects_member_relocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client, _target_client = _patch_instance_preflight(
        monkeypatch,
        source_project={"config": {}},
        target_project={"config": {}},
        source_instance={
            "name": "workload",
            "type": "container",
            "architecture": "x86_64",
            "profiles": ["default"],
            "location": "node-a",
            "expanded_config": {},
            "expanded_devices": {},
        },
        source_profile={"config": {}, "devices": {}},
        target_profile={"config": {}, "devices": {}},
    )

    result = _inspect_instance(
        source_client,
        source_client,
        intent="copy",
        source_cluster="same",
        target_cluster="same",
        target_project="source-project",
        target_name="workload-copy",
        target_member="node-b",
    )

    assert result.strategy == "same_cluster_copy"
    assert result.strategy != "member_relocation"


def test_safe_recovery_never_resubmits_after_a_mutation_intent() -> None:
    job = SimpleNamespace(status="running", phase="queued", intent="copy")
    clean = SimpleNamespace(
        resource_type="instance",
        state="pending",
        operation_refs=[],
        safe_result={},
    )
    ambiguous = SimpleNamespace(
        resource_type="instance",
        state="syncing",
        operation_refs=[],
        safe_result={"transfer_submission_intent": True},
    )

    assert orchestrator._safe_mutation_resume(job, [clean]) is True
    assert orchestrator._safe_mutation_resume(job, [ambiguous]) is False


def test_job_plan_pin_requires_exact_id_revision_and_payload_hash() -> None:
    job = SimpleNamespace(
        plan_id="plan-a", plan_revision=3, plan_payload_hash="hash-a"
    )
    matching = SimpleNamespace(plan_id="plan-a", revision=3, payload_hash="hash-a")
    orchestrator._assert_job_plan_pin(job, matching)

    with pytest.raises(MigrationAuthorityError):
        orchestrator._assert_job_plan_pin(
            job,
            SimpleNamespace(plan_id="plan-a", revision=3, payload_hash="hash-b"),
        )


def test_validated_copy_completion_effects_have_stable_separate_channels() -> None:
    job = SimpleNamespace(
        job_id="job-a",
        scope="instance",
        intent="copy",
        target_cluster_id="target",
        target_project="project",
        target_resource_name="copy",
        source_resource_name="source",
    )

    first = target_completion_events(
        job, include_created_events=True, occurred_at="2026-09-22T00:00:00+00:00"
    )
    second = target_completion_events(
        job, include_created_events=True, occurred_at="2026-09-22T00:00:00+00:00"
    )

    assert [event["channel"] for event in first] == ["cache", "webhook", "billing"]
    assert [event["event_id"] for event in first] == [
        event["event_id"] for event in second
    ]
    assert all(len(event["event_id"]) <= 64 for event in first)


@pytest.mark.asyncio
async def test_outbox_sequence_advances_after_zero() -> None:
    class Result:
        def __init__(self, value: int | None) -> None:
            self._value = value

        def scalar(self) -> int | None:
            return self._value

    class DB:
        def __init__(self) -> None:
            self.values = iter([None, 0, 1])

        async def execute(self, _statement: Any) -> Result:
            return Result(next(self.values))

    db = DB()
    assert [
        await repository._next_sequence(db, "job", "audit") for _ in range(3)
    ] == [0, 1, 2]


def test_router_exposes_the_durable_public_contract() -> None:
    routes = {
        (route.path, method)
        for route in migration_router.router.routes
        for method in getattr(route, "methods", set())
    }
    expected = {
        ("/migrations/plans", "POST"),
        ("/migrations/plans", "GET"),
        ("/migrations/plans/{plan_id}", "GET"),
        ("/migrations/plans/{plan_id}", "PATCH"),
        ("/migrations/plans/{plan_id}/execute", "POST"),
        ("/migrations", "GET"),
        ("/migrations/{job_id}", "GET"),
        ("/migrations/{job_id}/cancel", "POST"),
        ("/migrations/{job_id}/cutover", "POST"),
        ("/migrations/{job_id}/confirm-external-cutover", "POST"),
        ("/migrations/{job_id}/rollback", "POST"),
        ("/migrations/{job_id}/restore-checkpoint", "POST"),
        ("/migrations/{job_id}/cleanup", "POST"),
        ("/migrations/{job_id}/recheck", "POST"),
    }

    assert expected <= routes


def test_header_contract_rejects_weak_revision_and_unsafe_idempotency() -> None:
    assert migration_router._if_match('"7"') == 7
    with pytest.raises(Exception):
        migration_router._if_match('W/"7"')
    with pytest.raises(Exception):
        migration_router._idempotency_key("contains spaces")


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer SYNTHETIC_SENTINEL",
        '{"password":"SYNTHETIC_SENTINEL"}',
        "-----BEGIN CERTIFICATE-----\nSYNTHETIC_SENTINEL\n-----END CERTIFICATE-----",
        "https://api.example/token/SYNTHETIC_SENTINEL",
        "https://user:SYNTHETIC_SENTINEL@api.example/1.0",
        "request?client_secret=SYNTHETIC_SENTINEL",
        (
            "eyJSYNTHETIC_SENTINELabc."
            "eyJSYNTHETIC_SENTINELdef."
            "SYNTHETIC_SENTINELsignature"
        ),
        "ghp_SYNTHETIC_SENTINEL1234567890",
    ],
)
def test_error_sanitizer_removes_synthetic_secrets(raw: str) -> None:
    assert "SYNTHETIC_SENTINEL" not in sanitize_error(raw)


def test_read_only_dependency_is_only_on_execution_and_actions() -> None:
    routes = {route.path: route for route in migration_router.router.routes}

    def dependency_calls(path: str) -> set[Any]:
        return {
            dependency.call
            for dependency in routes[path].dependant.dependencies
            if dependency.call is not None
        }

    assert check_read_only_mode not in dependency_calls("/migrations/plans")
    assert check_read_only_mode not in dependency_calls("/migrations/plans/{plan_id}")
    assert check_read_only_mode in dependency_calls(
        "/migrations/plans/{plan_id}/execute"
    )
    assert check_read_only_mode in dependency_calls("/migrations/{job_id}/cleanup")


@pytest.mark.asyncio
async def test_revoked_current_authority_blocks_migration_read_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup_called = False

    async def denied(_user: Any) -> Any:
        raise HTTPException(status_code=403, detail="revoked")

    class Service:
        def __init__(self, _db: Any) -> None:
            nonlocal lookup_called
            lookup_called = True

    monkeypatch.setattr(migration_router, "_current_authority", denied)
    monkeypatch.setattr(migration_router, "MigrationService", Service)

    with pytest.raises(HTTPException) as exc:
        await migration_router.get_migration(
            "job-id", user=SimpleNamespace(), db=object()
        )

    assert exc.value.status_code == 403
    assert lookup_called is False
