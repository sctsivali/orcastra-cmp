"""The legacy VM create route cannot bypass shared-cluster write policy."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.models.schemas import VMCreateRequest, VMInfo, VMStatus
from app.routers import vms
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy

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
    def __init__(self, project_config: dict[str, str]) -> None:
        self.project_config = project_config
        self.events: list[str] = []
        self.creates: list[dict[str, Any]] = []

    def get_node_names(self):
        self.events.append("hosts")
        return [HOST]

    async def list_projects(self, host):
        self.events.append("project-read")
        return [
            {
                "name": PROJECT,
                "host": host,
                "description": "",
                "config": dict(self.project_config),
                "used_by": [],
            }
        ]

    async def create_vm(self, **kwargs):
        self.events.append("create")
        self.creates.append(kwargs)
        return _vm(kwargs["project"])


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

    monkeypatch.setattr(vms, "verify_project_access", authorize)
    monkeypatch.setattr(vms, "resolve_cluster_write_policy", resolved_policy)
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
        db=object(),
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
    assert "project-read" not in service.events
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

    monkeypatch.setattr(vms, "serialize_cluster_configuration", observed_lock)

    await _create(service, _actor(Role.PARTNER), config={"limits.cpu": "2"})

    assert service.events == [
        "authorize",
        "hosts",
        "lock-enter",
        "ownership",
        "create",
        "lock-exit",
    ]
