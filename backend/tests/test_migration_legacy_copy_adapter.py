"""The legacy Copy Instance surface delegates safely when durable copy is enabled."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.config import get_settings
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.routers import instance_mobility


def _body(**updates) -> instance_mobility.MoveRequest:
    values = {
        "host": "source-cluster",
        "project": "source-project",
        "target_host": "target-cluster",
        "target_project": "target-project",
        "target_name": "workload-copy",
        "target_member": "member-2",
        "target_pool": "fast-pool",
        "reason": "Capacity rebalance",
    }
    values.update(updates)
    return instance_mobility.MoveRequest(**values)


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="operator@example.test",
        email="operator@example.test",
        raw_claims={"iss": "https://auth.example.test", "sub": "actor"},
    )


def test_durable_adapter_requires_a_retry_stable_key(monkeypatch):
    async def must_not_execute(*args, **kwargs):
        pytest.fail("a durable job was created without an idempotency key")

    monkeypatch.setattr(
        instance_mobility.MigrationService,
        "execute_compatibility_copy",
        must_not_execute,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            instance_mobility._durable_copy(
                db=object(),
                lxd_service=object(),
                user=_user(),
                instance_name="workload",
                body=_body(),
                idempotency_key=None,
                justification=None,
            )
        )
    assert exc.value.status_code == 428


def test_durable_adapter_maps_the_legacy_request_without_a_daemon_operation(monkeypatch):
    captured = {}

    async def execute(_service, request, **kwargs):
        captured["request"] = request
        captured.update(kwargs)
        return SimpleNamespace(job_id="migration-job-1"), True

    monkeypatch.setattr(
        instance_mobility.MigrationService,
        "execute_compatibility_copy",
        execute,
    )
    result = asyncio.run(
        instance_mobility._durable_copy(
            db=object(),
            lxd_service=object(),
            user=_user(),
            instance_name="workload",
            body=_body(),
            idempotency_key="copy-request-1",
            justification=None,
        )
    )

    request = captured["request"]
    assert isinstance(request, MigrationPlanCreateRequest)
    assert request.source.resource_name == "workload"
    assert request.target.resource_name == "workload-copy"
    assert request.target.member == "member-2"
    assert request.mappings.storage_pools == {"root": "fast-pool"}
    assert request.checkpoint_requested is False
    assert captured["idempotency_key"] == "copy-request-1"
    assert captured["acknowledgements"] == [
        "copy-impact-reviewed",
        "billing-impact-reviewed",
    ]
    assert result.transport == "durable"
    assert result.operation_id is None
    assert result.migration_job_id == "migration-job-1"


def test_durable_adapter_carries_an_acknowledged_billing_impact(monkeypatch):
    captured = {}

    async def execute(_service, _request, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(job_id="migration-job-billed"), True

    monkeypatch.setattr(
        instance_mobility.MigrationService,
        "execute_compatibility_copy",
        execute,
    )

    asyncio.run(
        instance_mobility._durable_copy(
            db=object(),
            lxd_service=object(),
            user=_user(),
            instance_name="workload",
            body=_body(),
            idempotency_key="copy-request-billed",
            justification="Approved billing impact",
        )
    )

    assert captured["acknowledgements"] == [
        "copy-impact-reviewed",
        "billing-impact-reviewed",
        "billing-impact-acknowledged",
    ]


def test_durable_adapter_requires_an_auditable_reason(monkeypatch):
    async def must_not_execute(*args, **kwargs):
        pytest.fail("a durable job was created without an auditable reason")

    monkeypatch.setattr(
        instance_mobility.MigrationService,
        "execute_compatibility_copy",
        must_not_execute,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            instance_mobility._durable_copy(
                db=object(),
                lxd_service=object(),
                user=_user(),
                instance_name="workload",
                body=_body(reason="short"),
                idempotency_key="copy-request-2",
                justification=None,
            )
        )
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    ("center_enabled", "copy_enabled"),
    [(False, False), (False, True), (True, False)],
)
def test_route_rejects_when_either_rollout_gate_is_disabled(
    monkeypatch,
    center_enabled,
    copy_enabled,
):
    settings = get_settings()
    old_center = settings.migration_center_enabled
    old_copy = settings.migration_durable_copy_enabled
    object.__setattr__(settings, "migration_center_enabled", center_enabled)
    object.__setattr__(settings, "migration_durable_copy_enabled", copy_enabled)
    authorized = []

    def authorize(host, project):
        authorized.append((host, project))
        return project

    async def must_not_open_daemon(*args, **kwargs):
        pytest.fail("disabled durable-copy admission opened a daemon client")

    monkeypatch.setattr(instance_mobility, "verify_project_access", authorize)
    monkeypatch.setattr(instance_mobility, "_client", must_not_open_daemon)
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                instance_mobility.copy_instance(
                    instance_name="workload",
                    body=_body(),
                    lxd_service=object(),
                    idempotency_key="copy-request-disabled",
                    db=object(),
                    _=None,
                    user=_user(),
                )
            )
    finally:
        object.__setattr__(settings, "migration_center_enabled", old_center)
        object.__setattr__(settings, "migration_durable_copy_enabled", old_copy)

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "durable_copy_admission_disabled"
    assert authorized == [
        ("source-cluster", "source-project"),
        ("target-cluster", "target-project"),
    ]


def test_enabled_route_enters_durable_engine_before_opening_target_client(monkeypatch):
    settings = get_settings()
    old_center = settings.migration_center_enabled
    old_copy = settings.migration_durable_copy_enabled
    object.__setattr__(settings, "migration_center_enabled", True)
    object.__setattr__(settings, "migration_durable_copy_enabled", True)
    calls = []

    async def client(_service, host):
        calls.append(host)
        if host == "target-cluster":
            pytest.fail("durable acceptance opened the target daemon in the request")
        return object()

    async def policy(*args, **kwargs):
        return {}

    async def durable(**kwargs):
        return instance_mobility.MoveResponse(
            success=True,
            instance_name="workload",
            target_host="target-cluster",
            target_project="target-project",
            target_name="workload-copy",
            transport="durable",
            migration_job_id="migration-job-2",
            message="Accepted",
        )

    monkeypatch.setattr(instance_mobility, "_client", client)
    monkeypatch.setattr(instance_mobility, "_read_source_policy", policy)
    monkeypatch.setattr(instance_mobility, "_durable_copy", durable)
    monkeypatch.setattr(instance_mobility, "verify_project_access", lambda _h, p: p)
    try:
        result = asyncio.run(
            instance_mobility.copy_instance(
                instance_name="workload",
                body=_body(),
                lxd_service=object(),
                idempotency_key="copy-request-3",
                db=object(),
                _=None,
                user=_user(),
            )
        )
    finally:
        object.__setattr__(settings, "migration_center_enabled", old_center)
        object.__setattr__(settings, "migration_durable_copy_enabled", old_copy)

    assert calls == ["source-cluster"]
    assert result.migration_job_id == "migration-job-2"
