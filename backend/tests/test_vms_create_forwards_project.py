"""POST /vms must create, start, report, and audit one authorized project."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.rbac import Role
from app.models.schemas import VMCreateRequest, VMInfo, VMStatus
from app.routers import vms
from app.services.lxd import instance as instance_module
from app.services.lxd import audit as audit_module
from app.services.lxd.exceptions import LXDOperationError
from app.services.lxd.instance import InstanceManager
from app.services.lxd.lxd_service import LXDService
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy


HOST = "shared-cluster"
PROJECT = "partner-owned"
NAME = "project-vm"


class _DB:
    async def commit(self):
        return None


@pytest.fixture(autouse=True)
def isolated_create_executor(monkeypatch):
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test_lxd_create")
    monkeypatch.setattr(instance_module, "_create_executor", pool)
    monkeypatch.setattr(instance_module, "_CREATE_MAX_IN_FLIGHT", 2)

    async def exclusive_policy(user, host, db):
        return ClusterWritePolicy(host, Role.PARTNER, ClusterOwnership.EXCLUSIVE)

    async def effective_payload(request, _service):
        config = dict(request.config or {})
        if request.cloud_init:
            config["user.user-data"] = request.cloud_init
        return SimpleNamespace(
            effective_config=config,
            effective_devices=request.devices or {},
            target_project_config={},
        )

    async def target_organization(_db, *, cluster_id, project):
        return SimpleNamespace(
            organization_id=1,
            organization_slug="partner",
            resolution="exact",
        )

    async def create_job(**_kwargs):
        return SimpleNamespace(job_id="legacy-create-job"), True

    async def completed_job(_job_id):
        return SimpleNamespace(status="success", error=None)

    async def drain_audit(**_kwargs):
        return 1

    monkeypatch.setattr(vms, "resolve_cluster_write_policy", exclusive_policy)
    monkeypatch.setattr(vms, "_legacy_effective_payload", effective_payload)
    monkeypatch.setattr(vms, "resolve_instance_job_organization", target_organization)
    monkeypatch.setattr(vms, "create_operation_job", create_job)
    monkeypatch.setattr(vms, "get_operation_job", completed_job)
    monkeypatch.setattr(vms, "drain_instance_operation_audits", drain_audit)
    try:
        yield
    finally:
        pool.shutdown(wait=True)


def _vm(project: str) -> VMInfo:
    return VMInfo(
        name=NAME,
        status=VMStatus.STOPPED,
        status_code=102,
        host=HOST,
        host_endpoint="https://cluster.invalid:8443",
        project=project,
    )


class _ActivityRecorder:
    def __init__(self):
        self.entries = []

    def log_activity(self, **kwargs):
        self.entries.append(kwargs)


@pytest.fixture
def partner_project_scope():
    allowed = org_scope._allowed_clusters_var.set({HOST})
    visible = org_scope._visible_projects_var.set({HOST: {"default", PROJECT}})
    tenant_acl = org_scope._tenant_acl_var.set(None)
    try:
        yield
    finally:
        org_scope._tenant_acl_var.reset(tenant_acl)
        org_scope._visible_projects_var.reset(visible)
        org_scope._allowed_clusters_var.reset(allowed)


@pytest.mark.asyncio
async def test_post_vms_forwards_the_authorized_project_and_audits_it(monkeypatch, partner_project_scope):
    seen = []
    drained = []

    class Service:
        def get_node_names(self):
            return [HOST]

        async def create_instance(self, **kwargs):
            seen.append(kwargs)
            return "operation-1"

        async def get_vm(self, name, host, project):
            assert (name, host) == (NAME, HOST)
            return _vm(project)

        async def fail_create_job(self, *args, **kwargs):
            pytest.fail("successful create must not fail its durable job")

    async def drain_audit(**kwargs):
        drained.append(kwargs)
        return 1

    monkeypatch.setattr(vms, "drain_instance_operation_audits", drain_audit)

    response = await vms.create_vm(
        request=VMCreateRequest(
            name=NAME,
            host=HOST,
            image="ubuntu/24.04",
            project=f" {PROJECT} ",
        ),
        lxd_service=Service(),
        _rbac=SimpleNamespace(email="partner@example.test"),
        db=_DB(),
    )

    assert seen[0]["project"] == PROJECT
    assert response.vm is not None
    assert response.vm.project == PROJECT
    assert drained == [{"job_id": "legacy-create-job"}]


@pytest.mark.asyncio
async def test_lxd_facade_forwards_project_by_name_and_preserves_default():
    calls = []

    class Manager:
        async def create_vm(self, **kwargs):
            calls.append(kwargs)
            return _vm(kwargs["project"])

    service = LXDService.__new__(LXDService)
    service.instance_manager = Manager()

    selected = await service.create_vm(NAME, HOST, "ubuntu", project=PROJECT)
    defaulted = await service.create_vm(NAME, HOST, "ubuntu")

    assert calls[0]["project"] == PROJECT
    assert calls[1]["project"] == "default"
    assert selected.project == PROJECT
    assert defaulted.project == "default"


class _Instance:
    name = NAME
    status = "Stopped"
    status_code = 102
    # Deliberately wrong, as on pylxd versions that report the bound wrapper's default.
    project = "default"
    architecture = "x86_64"
    config = {}
    expanded_config = {}
    devices = {}
    expanded_devices = {}
    profiles = ["default"]
    description = ""
    created_at = None
    last_used_at = None
    location = "none"
    type = "virtual-machine"


class _Instances:
    def __init__(self, creates):
        self.creates = creates

    def create(self, definition, wait):
        self.creates.append((definition, wait))
        return _Instance()


class _Client:
    def __init__(self, creates):
        self.virtual_machines = _Instances(creates)
        self.containers = _Instances(creates)


class _Connection:
    def __init__(self, *, scoped=True):
        self.pooled_creates = []
        self.scoped_creates = []
        self.pooled = _Client(self.pooled_creates)
        self.scoped = _Client(self.scoped_creates) if scoped else None
        self.project_requests = []

    def get_client(self, host):
        return self.pooled

    def get_project_client(self, host, project, timeout):
        self.project_requests.append((host, project, timeout))
        return self.scoped


def _manager(connection, monkeypatch, activity):
    manager = InstanceManager(
        connection,
        SimpleNamespace(get_resource_usage=lambda _instance: None),
        None,
    )
    monkeypatch.setattr(manager, "_get_node_endpoint", lambda _host: "endpoint")
    monkeypatch.setattr(audit_module, "get_activity_logger", lambda: activity)
    return manager


@pytest.mark.asyncio
async def test_manager_creates_and_starts_with_the_same_project(monkeypatch):
    connection = _Connection()
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)
    starts = []

    def start_in_project(instance, host, project):
        starts.append((instance, host, project))
        return instance

    monkeypatch.setattr(manager, "_start_and_wait", start_in_project)

    result = await manager.create_vm(
        name=NAME,
        host=HOST,
        image="ubuntu",
        project=PROJECT,
        start_on_create=True,
    )

    assert connection.project_requests == [(HOST, PROJECT, (5, 1800))]
    assert len(connection.scoped_creates) == 1
    assert connection.pooled_creates == []
    assert starts == [(ANY, HOST, PROJECT)]
    assert result.project == PROJECT
    assert activity.entries[-1]["target_project"] == PROJECT
    assert activity.entries[-1]["details"]["project"] == PROJECT


@pytest.mark.asyncio
async def test_manager_keeps_the_legacy_default_client_fallback(monkeypatch):
    connection = _Connection(scoped=False)
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)

    result = await manager.create_vm(
        name=NAME,
        host=HOST,
        image="ubuntu",
        start_on_create=False,
    )

    assert connection.project_requests == [(HOST, "default", (5, 1800))]
    assert len(connection.pooled_creates) == 1
    assert result.project == "default"


@pytest.mark.asyncio
async def test_manager_never_falls_back_to_default_for_a_named_project(monkeypatch):
    connection = _Connection(scoped=False)
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)

    with pytest.raises(LXDOperationError, match="Could not open project 'partner-owned'"):
        await manager.create_vm(
            name=NAME,
            host=HOST,
            image="ubuntu",
            project=PROJECT,
        )

    assert connection.pooled_creates == []


@pytest.mark.asyncio
async def test_slow_create_does_not_block_the_event_loop(monkeypatch):
    connection = _Connection()
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)
    entered = threading.Event()
    release = threading.Event()
    create_returned = threading.Event()
    readback_done = threading.Event()
    original_create = connection.scoped.virtual_machines.create
    original_readback = manager._instance_to_vm_info

    def slow_create(definition, wait):
        entered.set()
        assert release.wait(timeout=1), "test never released the fake LXD create"
        result = original_create(definition, wait)
        create_returned.set()
        return result

    def observed_readback(*args, **kwargs):
        result = original_readback(*args, **kwargs)
        readback_done.set()
        return result

    connection.scoped.virtual_machines.create = slow_create
    monkeypatch.setattr(manager, "_instance_to_vm_info", observed_readback)
    task = asyncio.create_task(
        manager.create_vm(
            name=NAME,
            host=HOST,
            image="ubuntu",
            project=PROJECT,
            start_on_create=False,
        )
    )
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.002)
        assert entered.is_set(), "fake create never entered its worker"

        heartbeats = 0
        for _ in range(5):
            await asyncio.sleep(0.005)
            heartbeats += 1

        assert heartbeats == 5
        assert not task.done(), "slow blocking create unexpectedly ran on the event loop"
    finally:
        release.set()

    for _ in range(100):
        if readback_done.is_set():
            break
        await asyncio.sleep(0.002)
    assert create_returned.is_set()
    assert readback_done.is_set()
    result = await asyncio.wait_for(task, timeout=1)
    assert result.project == PROJECT


@pytest.mark.parametrize(
    "blocking_error,message",
    [
        (BlockingTimeout("create", 1), "timed out"),
        (BlockingPoolFull("create", 4), "Too many VM creations"),
    ],
)
@pytest.mark.asyncio
async def test_manager_translates_executor_timeout_and_saturation(monkeypatch, blocking_error, message):
    connection = _Connection()
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)

    async def unavailable(*args, **kwargs):
        raise blocking_error

    monkeypatch.setattr(instance_module, "run_blocking", unavailable)

    with pytest.raises(LXDOperationError, match=message) as caught:
        await manager.create_vm(
            name=NAME,
            host=HOST,
            image="ubuntu",
            project=PROJECT,
        )

    assert caught.value.__cause__ is blocking_error
    assert connection.scoped_creates == []
    assert connection.pooled_creates == []


@pytest.mark.asyncio
async def test_create_times_out_without_waiting_for_the_blocked_worker(monkeypatch):
    connection = _Connection()
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)
    entered = threading.Event()
    release = threading.Event()
    original_create = connection.scoped.virtual_machines.create

    def blocked_create(definition, wait):
        entered.set()
        assert release.wait(timeout=1), "test never released the fake LXD create"
        return original_create(definition, wait)

    connection.scoped.virtual_machines.create = blocked_create
    monkeypatch.setattr(instance_module, "_CREATE_WALL_TIMEOUT_SECONDS", 0.02)

    try:
        with pytest.raises(LXDOperationError, match="timed out") as caught:
            await manager.create_vm(
                name=NAME,
                host=HOST,
                image="ubuntu",
                project=PROJECT,
                start_on_create=False,
            )
    finally:
        release.set()

    assert entered.is_set()
    assert isinstance(caught.value.__cause__, BlockingTimeout)


@pytest.mark.asyncio
async def test_create_sheds_load_when_its_pool_is_full(monkeypatch):
    connection = _Connection()
    activity = _ActivityRecorder()
    manager = _manager(connection, monkeypatch, activity)
    entered = threading.Event()
    release = threading.Event()
    original_create = connection.scoped.virtual_machines.create

    def blocked_create(definition, wait):
        entered.set()
        assert release.wait(timeout=1), "test never released the fake LXD create"
        return original_create(definition, wait)

    connection.scoped.virtual_machines.create = blocked_create
    monkeypatch.setattr(instance_module, "_CREATE_MAX_IN_FLIGHT", 1)
    first = asyncio.create_task(
        manager.create_vm(
            name="first-vm",
            host=HOST,
            image="ubuntu",
            project=PROJECT,
            start_on_create=False,
        )
    )
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.002)
        assert entered.is_set()

        with pytest.raises(LXDOperationError, match="Too many VM creations") as caught:
            await manager.create_vm(
                name="second-vm",
                host=HOST,
                image="ubuntu",
                project=PROJECT,
                start_on_create=False,
            )
        assert isinstance(caught.value.__cause__, BlockingPoolFull)
    finally:
        release.set()

    for _ in range(500):
        if first.done():
            break
        await asyncio.sleep(0.002)
    assert first.done()
    await first


@pytest.mark.parametrize(
    "blocking_error",
    [BlockingTimeout("create", 1), BlockingPoolFull("create", 4)],
)
@pytest.mark.asyncio
async def test_post_vms_maps_executor_pressure_to_503(monkeypatch, partner_project_scope, blocking_error):
    drained = []

    class Service:
        def get_node_names(self):
            return [HOST]

        async def create_instance(self, **kwargs):
            try:
                raise blocking_error
            except (BlockingTimeout, BlockingPoolFull) as exc:
                raise LXDOperationError("VM creation is temporarily unavailable") from exc

        async def fail_create_job(self, *_args, **_kwargs):
            return None

    async def drain_audit(**kwargs):
        drained.append(kwargs)
        return 1

    monkeypatch.setattr(vms, "drain_instance_operation_audits", drain_audit)

    with pytest.raises(HTTPException) as caught:
        await vms.create_vm(
            request=VMCreateRequest(
                name=NAME,
                host=HOST,
                image="ubuntu/24.04",
                project=PROJECT,
            ),
            lxd_service=Service(),
            _rbac=SimpleNamespace(email="partner@example.test"),
            db=_DB(),
    )

    assert caught.value.status_code == 503
    assert drained == [{"job_id": "legacy-create-job"}]
