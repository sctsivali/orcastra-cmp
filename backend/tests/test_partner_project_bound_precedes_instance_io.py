"""Shared-cluster project boundaries must run before any instance I/O."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.config import get_settings
from app.routers import (
    console,
    instance_mobility,
    instance_rebuild,
    instance_snapshots,
    instances,
    terminal,
    vms,
)
from app.models.schemas import InstanceCreateRequest, VMCreateRequest


HOST = "shared-cluster"
TARGET = "target-cluster"
NAME = "same-name"
BOUND = {HOST: {"default", "partner-owned"}, TARGET: {"default"}}


@pytest.fixture(autouse=True)
def partner_scope():
    allowed_token = org_scope._allowed_clusters_var.set({HOST, TARGET})
    projects_token = org_scope._visible_projects_var.set(BOUND)
    acl_token = org_scope._tenant_acl_var.set(None)
    settings = get_settings()
    original_flag = settings.tenant_perm_enforcement_enabled
    object.__setattr__(settings, "tenant_perm_enforcement_enabled", False)
    try:
        yield
    finally:
        object.__setattr__(
            settings, "tenant_perm_enforcement_enabled", original_flag
        )
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._visible_projects_var.reset(projects_token)
        org_scope._allowed_clusters_var.reset(allowed_token)


@pytest.mark.parametrize("raw", [None, "", "   ", " default "])
def test_the_shared_project_gate_normalizes_default_once(raw):
    assert org_scope.verify_project_access(HOST, raw) == "default"


def test_the_shared_project_gate_refuses_a_foreign_project():
    with pytest.raises(HTTPException) as exc:
        org_scope.verify_project_access(HOST, "other-org")
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "handler,body",
    [
        (
            instances.create_instance,
            InstanceCreateRequest(
                name=NAME, host=HOST, image="ubuntu", project="other-org"
            ),
        ),
        (
            vms.create_vm,
            VMCreateRequest(
                name=NAME, host=HOST, image="ubuntu", project="other-org"
            ),
        ),
    ],
)
def test_partner_create_denies_before_the_service(handler, body):
    class Service:
        def get_node_names(self):
            pytest.fail("foreign project enumerated clusters")

        async def create_instance(self, **kwargs):
            pytest.fail("foreign project reached create_instance")

        async def create_vm(self, **kwargs):
            pytest.fail("foreign project reached create_vm")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            handler(
                request=body,
                lxd_service=Service(),
                _rbac=SimpleNamespace(email="partner@example.test"),
            )
        )
    assert exc.value.status_code == 403


class _Socket:
    def __init__(self):
        self.accepted = False
        self.closed = None

    async def accept(self, *args, **kwargs):
        self.accepted = True

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)


@pytest.mark.parametrize(
    "handler",
    [console.console_websocket, terminal.terminal_websocket],
)
def test_a_foreign_project_websocket_is_closed_before_accept(monkeypatch, handler):
    async def keep_test_scope(_user, **_kwargs):
        return None

    owner = console if handler is console.console_websocket else terminal
    monkeypatch.setattr(owner, "set_org_scope_for_ws", keep_test_scope)
    socket = _Socket()
    user = SimpleNamespace(email="partner@example.test")

    kwargs = {
        "websocket": socket,
        "instance_name": NAME,
        "host": HOST,
        "project": "other-org",
        "user": user,
    }
    if handler is console.console_websocket:
        kwargs["console_type"] = "vga"
    asyncio.run(handler(**kwargs))

    assert socket.accepted is False
    assert socket.closed == (1008, "forbidden")


class _NoRebuildIO:
    async def get_rebuild_preflight(self, *args, **kwargs):
        pytest.fail("foreign project reached rebuild preflight")


def test_rebuild_denies_before_preflight():
    body = instance_rebuild.RebuildRequest(
        host=HOST,
        project="other-org",
        mode="empty",
        confirm_name=NAME,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            instance_rebuild.rebuild_instance(
                NAME,
                body,
                lxd_service=_NoRebuildIO(),
                user=SimpleNamespace(email="partner@example.test", user_id="partner"),
            )
        )
    assert exc.value.status_code == 403


def test_rebuild_preflight_carries_a_blank_project_as_default():
    seen = []

    class Service:
        async def get_rebuild_preflight(self, name, host, project):
            seen.append(project)
            return {"status": "Stopped", "type": "container"}

    result = asyncio.run(
        instance_rebuild.rebuild_preflight(
            NAME,
            host=HOST,
            project="   ",
            lxd_service=Service(),
        )
    )

    assert result["instance_name"] == NAME
    assert seen == ["default"]


def test_snapshot_denies_before_opening_an_lxd_client(monkeypatch):
    async def forbidden_client(*args, **kwargs):
        pytest.fail("foreign project opened an LXD client")

    monkeypatch.setattr(instance_snapshots, "_client", forbidden_client)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            instance_snapshots.list_snapshots(
                NAME,
                host=HOST,
                project="other-org",
                lxd_service=object(),
            )
        )
    assert exc.value.status_code == 403


class _NoMobilityIO:
    def get_node_names(self):
        pytest.fail("foreign project enumerated destination clusters")


def test_mobility_preflight_denies_before_opening_the_source(monkeypatch):
    async def forbidden_client(*args, **kwargs):
        pytest.fail("foreign project opened the source client")

    monkeypatch.setattr(instance_mobility, "_client", forbidden_client)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            instance_mobility.move_preflight(
                NAME,
                host=HOST,
                project="other-org",
                lxd_service=_NoMobilityIO(),
                _rbac=SimpleNamespace(email="partner@example.test"),
            )
        )
    assert exc.value.status_code == 403


def test_mobility_preflight_hides_foreign_destination_projects(monkeypatch):
    async def source_client(*args, **kwargs):
        return object()

    async def candidate_client(*args, **kwargs):
        return object()

    async def immediate(_fn, *args, label, **kwargs):
        values = {
            "mobility.footprint": 1024,
            "mobility.arch": "x86_64",
            "mobility.arches": ["x86_64"],
            "mobility.projects": ["default", "other-org"],
            "mobility.pools": [],
            "mobility.headroom": [],
        }
        return values[label]

    class Service:
        def get_node_names(self):
            return [TARGET]

    monkeypatch.setattr(instance_mobility, "_client", source_client)
    monkeypatch.setattr(instance_mobility, "client_for", candidate_client)
    monkeypatch.setattr(instance_mobility, "_blocking", immediate)

    result = asyncio.run(
        instance_mobility.move_preflight(
            NAME,
            host=HOST,
            project="partner-owned",
            lxd_service=Service(),
            _rbac=SimpleNamespace(email="partner@example.test"),
        )
    )

    assert result.destinations[0]["projects"] == ["default"]


@pytest.mark.parametrize(
    "source_project,target_project",
    [("other-org", "default"), ("default", "other-org")],
)
def test_copy_denies_either_foreign_project_before_opening_a_client(
    monkeypatch, source_project, target_project
):
    async def forbidden_client(*args, **kwargs):
        pytest.fail("foreign copy endpoint opened an LXD client")

    monkeypatch.setattr(instance_mobility, "_client", forbidden_client)
    body = instance_mobility.MoveRequest(
        host=HOST,
        project=source_project,
        target_host=TARGET,
        target_project=target_project,
        target_name="copy-name",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            instance_mobility.copy_instance(
                NAME,
                body,
                lxd_service=_NoMobilityIO(),
                user=SimpleNamespace(email="partner@example.test", user_id="partner"),
            )
        )
    assert exc.value.status_code == 403


def test_operation_grants_never_substitute_for_view_when_rollout_is_off():
    acl_token = org_scope._tenant_acl_var.set(
        {HOST: {("default", NAME): {"console", "operate", "snapshot"}}}
    )
    try:
        with pytest.raises(HTTPException) as exc:
            console._verify_instance_access(HOST, "default", NAME, "console")
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
    assert exc.value.status_code == 403
