"""The legacy VM create route cannot bypass shared-cluster write policy."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.models.schemas import (
    InstanceCreatePreflightIssue,
    InstanceCreatePreflightResponse,
    InstanceCreateEffectiveSummary,
    VMCreateRequest,
    VMInfo,
    VMStatus,
)
from app.routers import vms
from app.services import shared_cluster_audit as share_guard
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy
from app.services.lxd.create_preflight import CreatePreflightEvaluation
from app.services.lxd.instance import CreateSubmissionUnknown
from app.services.lxd.nic_preflight import merge_effective_devices

HOST = "shared-cluster"
PROJECT = "partner-owned"
NAME = "legacy-vm"

SAFE_PROJECT_CONFIG = {
    "restricted": "true",
    "features.images": "true",
    "features.networks": "true",
    "features.profiles": "true",
    "features.storage.volumes": "true",
    "limits.cpu": "16",
}


class _DB:
    async def commit(self):
        return None


def _actor(role: Role) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"legacy-vm-{role.value}",
        email=f"{role.value}@example.test",
        groups=[f"role_{role.value}"],
        roles=[f"role_{role.value}"],
    )


def _vm(project: str) -> VMInfo:
    return VMInfo(
        name=NAME,
        status=VMStatus.STOPPED,
        status_code=102,
        host=HOST,
        host_endpoint="https://cluster.invalid:8443",
        project=project,
    )


class _Service:
    def __init__(
        self,
        project_config: dict[str, str],
        *,
        profile_config: dict[str, str] | None = None,
        profile_devices: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.project_config = project_config
        self.profile_config = profile_config or {}
        self.profile_devices = profile_devices or {}
        self.events: list[str] = []
        self.creates: list[dict[str, Any]] = []
        self.failed_jobs: list[tuple[str, dict[str, Any]]] = []

    def get_node_names(self):
        self.events.append("hosts")
        return [HOST]

    async def create_instance(self, **kwargs):
        self.events.append("create")
        self.creates.append(kwargs)
        return "create-operation"

    async def get_vm(self, name, host, project):
        assert (name, host) == (NAME, HOST)
        return _vm(project)

    async def fail_create_job(self, job_id, **kwargs):
        self.failed_jobs.append((job_id, kwargs))


def _wire_route(
    monkeypatch,
    policy: ClusterWritePolicy,
    service: _Service,
) -> None:
    def authorize(host, project):
        service.events.append("authorize")
        return (project or "default").strip() or "default"

    async def resolved_policy(user, host, db):
        service.events.append("ownership")
        return policy

    async def effective_payload(request, _lxd_service):
        service.events.append("project-read")
        config = dict(service.profile_config)
        config.update(request.config or {})
        if request.cloud_init:
            config["user.user-data"] = request.cloud_init
        return SimpleNamespace(
            effective_config=config,
            effective_devices=merge_effective_devices(
                request.devices,
                [service.profile_devices],
            ),
            target_project_config=dict(service.project_config),
        )

    async def target_organization(_db, *, cluster_id, project):
        return SimpleNamespace(
            organization_id=7,
            organization_slug="partner",
            resolution="exact",
        )

    async def create_job(**_kwargs):
        return SimpleNamespace(job_id="legacy-job", status="pending", error=None), True

    async def completed_job(_job_id):
        return SimpleNamespace(job_id="legacy-job", status="success", error=None)

    monkeypatch.setattr(vms, "verify_project_access", authorize)
    monkeypatch.setattr(vms, "resolve_cluster_write_policy", resolved_policy)
    monkeypatch.setattr(vms, "_legacy_effective_payload", effective_payload)
    monkeypatch.setattr(vms, "resolve_instance_job_organization", target_organization)
    monkeypatch.setattr(vms, "create_operation_job", create_job)
    monkeypatch.setattr(vms, "get_operation_job", completed_job)
    monkeypatch.setattr(vms.activity_logger, "log_activity", lambda **kwargs: None)
    monkeypatch.setattr(
        vms,
        "get_cache_manager",
        lambda: SimpleNamespace(invalidate_instance_cache=lambda: None),
    )


async def _create(
    service: _Service,
    actor: AuthenticatedUser,
    *,
    config: dict[str, str] | None = None,
    devices: dict[str, dict[str, str]] | None = None,
    db: Any | None = None,
):
    return await vms.create_vm(
        request=VMCreateRequest(
            name=NAME,
            host=HOST,
            image="ubuntu/24.04",
            project=PROJECT,
            config=config,
            devices=devices,
        ),
        lxd_service=service,
        _=None,
        _rbac=actor,
        db=db or _DB(),
    )


@pytest.mark.asyncio
async def test_shared_partner_unsafe_project_is_denied_before_create(monkeypatch) -> None:
    service = _Service({"restricted": "false", "user.marker": "do-not-echo"})
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)
    _wire_route(monkeypatch, policy, service)

    with pytest.raises(HTTPException) as caught:
        await _create(service, _actor(Role.PARTNER), config={"limits.cpu": "2"})

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "shared_cluster_project_not_isolated"
    assert "do-not-echo" not in str(caught.value.detail)
    assert service.creates == []
    assert service.events == ["authorize", "hosts", "ownership", "project-read"]


@pytest.mark.asyncio
async def test_shared_partner_unsafe_payload_is_denied_before_create(monkeypatch) -> None:
    service = _Service(SAFE_PROJECT_CONFIG)
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)
    _wire_route(monkeypatch, policy, service)

    with pytest.raises(HTTPException) as caught:
        await _create(
            service,
            _actor(Role.PARTNER),
            config={"raw.qemu": "-device vfio-pci"},
            devices={"gpu0": {"type": "gpu"}},
        )

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "shared_cluster_policy_violation"
    assert service.creates == []
    assert service.events[-1] == "project-read"


@pytest.mark.asyncio
async def test_shared_partner_safe_project_and_typed_payload_are_allowed(monkeypatch) -> None:
    service = _Service(SAFE_PROJECT_CONFIG)
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)
    _wire_route(monkeypatch, policy, service)

    response = await _create(
        service,
        _actor(Role.PARTNER),
        config={"limits.cpu": "2", "limits.memory": "2GiB"},
        devices={"eth0": {"type": "nic", "network": "tenant-net"}},
    )

    assert response.success is True
    assert len(service.creates) == 1
    assert service.events[-2:] == ["project-read", "create"]


@pytest.mark.asyncio
async def test_shared_partner_inherited_admin_device_is_denied_before_create(
    monkeypatch,
) -> None:
    service = _Service(
        SAFE_PROJECT_CONFIG,
        profile_devices={"accelerator": {"type": "gpu", "id": "0"}},
    )
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)
    _wire_route(monkeypatch, policy, service)

    with pytest.raises(HTTPException) as caught:
        await _create(service, _actor(Role.PARTNER), config={"limits.cpu": "2"})

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "shared_cluster_policy_violation"
    assert caught.value.detail["violations"][0]["field"] == "devices.accelerator"
    assert service.creates == []


@pytest.mark.asyncio
async def test_shared_partner_type_none_removes_inherited_admin_device(
    monkeypatch,
) -> None:
    service = _Service(
        SAFE_PROJECT_CONFIG,
        profile_devices={"accelerator": {"type": "gpu", "id": "0"}},
    )
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)
    _wire_route(monkeypatch, policy, service)

    response = await _create(
        service,
        _actor(Role.PARTNER),
        config={"limits.cpu": "2"},
        devices={"accelerator": {"type": "none"}},
    )

    assert response.success is True
    assert len(service.creates) == 1
    assert service.creates[0]["devices"] == {"accelerator": {"type": "none"}}


@pytest.mark.parametrize(
    ("role", "ownership"),
    [
        (Role.PARTNER, ClusterOwnership.EXCLUSIVE),
        (Role.ADMIN, ClusterOwnership.SHARED),
    ],
)
@pytest.mark.asyncio
async def test_exclusive_partner_and_admin_keep_full_create_access(
    monkeypatch,
    role: Role,
    ownership: ClusterOwnership,
) -> None:
    service = _Service({})
    policy = ClusterWritePolicy(HOST, role, ownership)
    _wire_route(monkeypatch, policy, service)

    response = await _create(
        service,
        _actor(role),
        config={"raw.qemu": "-device vfio-pci"},
        devices={"gpu0": {"type": "gpu"}},
    )

    assert response.success is True
    assert len(service.creates) == 1
    assert "project-read" in service.events
    assert service.events[-1] == "create"


@pytest.mark.asyncio
async def test_vms_route_still_uses_partner_rbac_for_tenant(monkeypatch) -> None:
    dependency = inspect.signature(vms.create_vm).parameters["_rbac"].default.dependency
    monkeypatch.setattr(
        "app.core.rbac.activity_logger.log_activity",
        lambda **kwargs: None,
    )
    request = SimpleNamespace(
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/vms"),
        method="POST",
    )

    with pytest.raises(HTTPException) as caught:
        await dependency(request=request, user=_actor(Role.TENANT))

    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_ownership_lock_covers_final_policy_and_daemon_submission(monkeypatch) -> None:
    service = _Service({})
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.EXCLUSIVE)
    _wire_route(monkeypatch, policy, service)

    @asynccontextmanager
    async def observed_lock(db, host):
        service.events.append("lock-enter")
        yield
        service.events.append("lock-exit")

    class ObservedDB:
        async def commit(self):
            service.events.append("db-commit")

    monkeypatch.setattr(vms, "serialize_cluster_configuration", observed_lock)

    await _create(
        service,
        _actor(Role.PARTNER),
        config={"limits.cpu": "2"},
        db=ObservedDB(),
    )

    assert service.events == [
        "authorize",
        "hosts",
        "lock-enter",
        "ownership",
        "project-read",
        "create",
        "db-commit",
        "lock-exit",
    ]


@pytest.mark.asyncio
async def test_legacy_preflight_blocker_refuses_before_job_or_create(monkeypatch) -> None:
    service = _Service(SAFE_PROJECT_CONFIG)
    request = VMCreateRequest(
        name=NAME,
        host=HOST,
        image="ubuntu/24.04",
        project=PROJECT,
    )
    blocker = InstanceCreatePreflightIssue(
        code="PROFILE_NOT_FOUND",
        field="profiles",
        message="The requested profile is unavailable.",
    )
    evaluation = CreatePreflightEvaluation(
        response=InstanceCreatePreflightResponse(
            ready=False,
            plan_revision="a" * 64,
            blockers=[blocker],
            effective=InstanceCreateEffectiveSummary(),
        ),
        effective_config={},
        effective_devices={},
        target_project_config=SAFE_PROJECT_CONFIG,
    )

    async def blocked(_service, _request):
        return evaluation

    monkeypatch.setattr(vms, "evaluate_instance_create", blocked)

    with pytest.raises(HTTPException) as caught:
        await vms._legacy_effective_payload(request, service)

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "CREATE_PREFLIGHT_FAILED"
    assert service.creates == []


@pytest.mark.asyncio
async def test_legacy_networkless_warning_remains_compatible(monkeypatch) -> None:
    service = _Service(SAFE_PROJECT_CONFIG)
    request = VMCreateRequest(
        name=NAME,
        host=HOST,
        image="ubuntu/24.04",
        project=PROJECT,
    )
    warning = InstanceCreatePreflightIssue(
        code="NETWORKLESS",
        field="devices",
        message="The instance will be isolated.",
        acknowledgement_required=True,
    )
    evaluation = CreatePreflightEvaluation(
        response=InstanceCreatePreflightResponse(
            ready=False,
            plan_revision="b" * 64,
            warnings=[warning],
            required_acknowledgements=["networkless"],
            effective=InstanceCreateEffectiveSummary(root_device="root"),
        ),
        effective_config={"limits.cpu": "1"},
        effective_devices={"root": {"type": "disk", "path": "/"}},
        target_project_config=SAFE_PROJECT_CONFIG,
    )

    async def networkless(_service, _request):
        return evaluation

    monkeypatch.setattr(vms, "evaluate_instance_create", networkless)

    assert await vms._legacy_effective_payload(request, service) is evaluation


def test_legacy_remote_image_server_rejects_ssrf_target() -> None:
    with pytest.raises(HTTPException) as caught:
        vms._validate_legacy_image_server("http://169.254.169.254/latest/meta-data")

    assert caught.value.status_code == 400
    assert "169.254.169.254" not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_legacy_wait_uses_bounded_exponential_backoff(monkeypatch) -> None:
    request = VMCreateRequest(
        name=NAME,
        host=HOST,
        image="ubuntu/24.04",
        project=PROJECT,
    )
    statuses = iter(["running", "running", "running", "running", "success"])
    sleeps: list[float] = []

    async def job(_job_id):
        return SimpleNamespace(status=next(statuses), error=None)

    async def sleep(delay):
        sleeps.append(delay)

    class Readback:
        async def get_vm(self, name, host, project):
            return _vm(project)

    monkeypatch.setattr(vms, "get_operation_job", job)
    monkeypatch.setattr(vms.asyncio, "sleep", sleep)

    result = await vms._wait_for_legacy_create("legacy-job", request, Readback())

    assert result.project == PROJECT
    assert sleeps == [0.25, 0.5, 1.0, 2.0]


class _GuardResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _GuardSession:
    def __init__(self, active):
        self.active = active
        self.calls = 0

    async def execute(self, _statement, _params=None):
        self.calls += 1
        if self.calls == 1:
            return _GuardResult([[1, 1, "owner@example.test"]])
        return _GuardResult([91] if self.active() else [])


@pytest.mark.asyncio
async def test_unknown_legacy_create_remains_a_pre_share_blocker(monkeypatch) -> None:
    service = _Service(SAFE_PROJECT_CONFIG)
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.EXCLUSIVE)
    _wire_route(monkeypatch, policy, service)
    state = {"status": "pending"}

    async def submit_unknown(**_kwargs):
        state["status"] = "unknown"
        raise CreateSubmissionUnknown("submission uncertain", job_id="legacy-job")

    async def job(_job_id):
        return SimpleNamespace(status=state["status"], error=None)

    async def must_not_audit(_cluster_id):
        raise AssertionError("active create must block sharing before daemon audit")

    service.create_instance = submit_unknown
    monkeypatch.setattr(vms, "get_operation_job", job)
    monkeypatch.setattr(share_guard, "audit_shared_cluster", must_not_audit)

    with pytest.raises(HTTPException) as caught:
        await _create(service, _actor(Role.PARTNER), config={"limits.cpu": "2"})

    assert caught.value.status_code == 503
    assert state["status"] == "unknown"
    assert service.failed_jobs == []
    with pytest.raises(share_guard.SharedClusterGuardError) as guard_error:
        await share_guard.assert_cluster_safe_to_share(
            _GuardSession(lambda: state["status"] in {"pending", "running", "unknown"}),
            HOST,
            prospective_organization_id=2,
        )
    assert guard_error.value.code == "cluster_configuration_operation_active"


@pytest.mark.asyncio
async def test_cancelled_legacy_wait_leaves_the_durable_job_active(monkeypatch) -> None:
    service = _Service(SAFE_PROJECT_CONFIG)
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.EXCLUSIVE)
    _wire_route(monkeypatch, policy, service)
    submitted = asyncio.Event()

    async def submit(**kwargs):
        service.creates.append(kwargs)
        submitted.set()
        return "create-operation"

    async def pending(_job_id):
        return SimpleNamespace(status="running", error=None)

    service.create_instance = submit
    monkeypatch.setattr(vms, "get_operation_job", pending)

    task = asyncio.create_task(
        _create(service, _actor(Role.PARTNER), config={"limits.cpu": "2"})
    )
    await asyncio.wait_for(submitted.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(service.creates) == 1
    assert service.failed_jobs == []
