"""Durable create jobs must never turn an ambiguous POST into a blind retry."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from fastapi import HTTPException
from requests import exceptions as requests_exc

from app.models.schemas import (
    InstanceCreateEffectiveSummary,
    InstanceCreatePreflightResponse,
    InstanceCreateRequest,
)
from app.routers import instances as instances_router
from app.services.lxd import instance as instance_module
from app.services.lxd.create_preflight import CreatePreflightEvaluation
from app.services.lxd.instance import CreateSubmissionUnknown, InstanceManager
from app.services.instance_security_policy import InstanceSecurityPolicyViolation


def _request(**changes):
    body = {
        "name": "web01",
        "host": "cluster-a",
        "project": "tenant-a",
        "image": "ubuntu/24.04",
        "instance_type": "virtual-machine",
        "config": {"limits.cpu": "2", "limits.memory": "2GiB"},
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": "fast"},
            "eth0": {"type": "nic", "network": "lxdbr0"},
        },
        "cloud_init": "#cloud-config\npassword: never-store-this",
    }
    body.update(changes)
    return InstanceCreateRequest(**body)


def _evaluation(request):
    return CreatePreflightEvaluation(
        response=InstanceCreatePreflightResponse(
            ready=True,
            plan_revision="a" * 64,
            effective=InstanceCreateEffectiveSummary(
                profiles=request.profiles or ["default"],
                nic_names=["eth0"],
                root_device="root",
            ),
        ),
        effective_config={"limits.cpu": "2", "limits.memory": "2GiB"},
        effective_devices={
            "root": {"type": "disk", "path": "/", "pool": "fast"},
            "eth0": {"type": "nic", "network": "lxdbr0"},
        },
    )


def _job(request, **changes):
    values = {
        "job_id": "job-1",
        "kind": "create",
        "cluster_id": request.host,
        "project": request.project,
        "instance_name": request.name,
        "instance_type": request.instance_type,
        "requester": "user-123",
        "target_organization_id": 17,
        "target_organization_slug": "org-seventeen",
        "target_organization_resolution": "exact",
        "idempotency_key": "request-1",
        "payload_hash": instances_router._create_request_hash(request),
        "requested": {
            "instance_type": request.instance_type,
            "source_type": request.source_type,
            "start_on_create": request.start_on_create,
            "profiles": request.profiles or ["default"],
        },
        "phase": "pending",
        "status": "pending",
        "operation_id": None,
        "operation_ids": [],
        "result": None,
        "locked_by": "worker-1",
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_router_persists_only_safe_intent_before_daemon_mutation(monkeypatch):
    request = _request()
    row = _job(request)
    order = []
    captured = {}

    monkeypatch.setattr(instances_router, "verify_project_access", lambda _host, project: project)
    monkeypatch.setattr(
        instances_router,
        "_run_create_preflight",
        lambda _request, _service: asyncio.sleep(0, result=_evaluation(_request)),
    )

    async def no_replay(_requester, _key):
        return None

    async def policy(*_args, **_kwargs):
        order.append("policy")
        return SimpleNamespace()

    async def persist(**kwargs):
        order.append("persist")
        captured.update(kwargs)
        return row, True

    monkeypatch.setattr(instances_router, "get_operation_job_by_request", no_replay)
    monkeypatch.setattr(instances_router, "resolve_cluster_write_policy", policy)
    monkeypatch.setattr(
        instances_router,
        "resolve_instance_job_organization",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(
                organization_id=17,
                organization_slug="org-seventeen",
                resolution="exact",
            ),
        ),
    )
    monkeypatch.setattr(instances_router, "validate_target_project_isolation", lambda *_args: None)
    monkeypatch.setattr(instances_router, "validate_instance_payload", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(instances_router, "create_operation_job", persist)
    monkeypatch.setattr(
        instances_router.activity_logger,
        "log_activity",
        lambda **_kwargs: None,
    )

    @asynccontextmanager
    async def configuration_lock(_db, _host):
        order.append("lock-enter")
        yield
        order.append("lock-exit")

    monkeypatch.setattr(instances_router, "serialize_cluster_configuration", configuration_lock)

    class Service:
        async def create_instance(self, **kwargs):
            order.append("daemon")
            assert kwargs["job_id"] == row.job_id
            return "op-123"

    response = await instances_router.create_instance(
        request=request,
        idempotency_key="request-1",
        lxd_service=Service(),
        db=SimpleNamespace(),
        _rbac=SimpleNamespace(user_id="user-123", email="fallback@example.test"),
    )

    assert order == ["lock-enter", "policy", "persist", "daemon", "lock-exit"]
    assert captured["requester"] == "user-123"
    assert captured["payload_hash"] == instances_router._create_request_hash(request)
    assert set(captured["requested"]) == {
        "instance_type",
        "source_type",
        "start_on_create",
        "profiles",
        "networkless_acknowledged",
    }
    assert captured["target_organization_id"] == 17
    assert captured["target_organization_resolution"] == "exact"
    stored = repr(captured["requested"])
    assert "cloud" not in stored.lower()
    assert "password" not in stored.lower()
    assert "devices" not in stored.lower()
    assert "config" not in stored.lower()
    assert response.job_id == row.job_id
    assert response.operation_id == "op-123"


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_without_daemon_reads(monkeypatch):
    request = _request()
    row = _job(request, status="running", phase="verifying", operation_id="op-1")
    monkeypatch.setattr(instances_router, "verify_project_access", lambda _host, project: project)

    async def replay(_requester, _key):
        return row

    monkeypatch.setattr(instances_router, "get_operation_job_by_request", replay)

    class NoIO:
        def __getattr__(self, name):
            pytest.fail(f"idempotent replay reached daemon service attribute {name}")

    response = await instances_router.create_instance(
        request=request,
        idempotency_key="request-1",
        lxd_service=NoIO(),
        db=SimpleNamespace(),
        _rbac=SimpleNamespace(user_id="user-123", email="fallback@example.test"),
    )

    assert response.job_id == "job-1"
    assert response.operation_id == "op-1"


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_different_payload_is_a_conflict(monkeypatch):
    request = _request()
    row = _job(request, payload_hash="0" * 64)
    monkeypatch.setattr(instances_router, "verify_project_access", lambda _host, project: project)

    async def replay(_requester, _key):
        return row

    monkeypatch.setattr(instances_router, "get_operation_job_by_request", replay)

    with pytest.raises(HTTPException) as exc:
        await instances_router.create_instance(
            request=request,
            idempotency_key="request-1",
            lxd_service=SimpleNamespace(),
            db=SimpleNamespace(),
            _rbac=SimpleNamespace(user_id="user-123", email="fallback@example.test"),
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_ambiguous_submission_returns_pollable_unknown_job(monkeypatch):
    request = _request()
    row = _job(request)
    audits = []
    monkeypatch.setattr(instances_router, "verify_project_access", lambda _host, project: project)
    monkeypatch.setattr(
        instances_router,
        "_run_create_preflight",
        lambda _request, _service: asyncio.sleep(0, result=_evaluation(_request)),
    )
    monkeypatch.setattr(
        instances_router,
        "get_operation_job_by_request",
        lambda *_args: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        instances_router,
        "resolve_cluster_write_policy",
        lambda *_args: asyncio.sleep(0, result=SimpleNamespace()),
    )
    monkeypatch.setattr(
        instances_router,
        "resolve_instance_job_organization",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(
                organization_id=None,
                organization_slug=None,
                resolution="ambiguous",
            ),
        ),
    )
    monkeypatch.setattr(instances_router, "validate_target_project_isolation", lambda *_args: None)
    monkeypatch.setattr(
        instances_router,
        "validate_instance_payload",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        instances_router,
        "create_operation_job",
        lambda **_kwargs: asyncio.sleep(0, result=(row, True)),
    )
    monkeypatch.setattr(
        instances_router.activity_logger,
        "log_activity",
        lambda **kwargs: audits.append(kwargs),
    )

    class Service:
        async def create_instance(self, **_kwargs):
            raise CreateSubmissionUnknown("socket outcome unknown", job_id=row.job_id)

    response = await instances_router.create_instance(
        request=request,
        idempotency_key="request-1",
        lxd_service=Service(),
        db=SimpleNamespace(),
        _rbac=SimpleNamespace(user_id="user-123", email="fallback@example.test"),
    )

    assert response.job_id == row.job_id
    assert response.status == "unknown"
    assert len(audits) == 1
    assert audits[0]["result"] == "accepted"
    assert audits[0]["details"]["job_id"] == row.job_id
    assert audits[0]["target_organization_id"] is None
    assert audits[0]["target_organization_resolution"] == "ambiguous"


@pytest.mark.asyncio
async def test_public_preflight_reports_security_policy_as_a_blocker(monkeypatch):
    request = _request()
    monkeypatch.setattr(instances_router, "verify_project_access", lambda _host, project: project)
    monkeypatch.setattr(
        instances_router,
        "_run_create_preflight",
        lambda _request, _service: asyncio.sleep(0, result=_evaluation(_request)),
    )
    monkeypatch.setattr(
        instances_router,
        "resolve_cluster_write_policy",
        lambda *_args: asyncio.sleep(0, result=SimpleNamespace()),
    )

    def refuse_project(*_args):
        raise InstanceSecurityPolicyViolation(
            "shared_cluster_project_not_isolated",
            "This project is not isolated for shared-cluster use.",
        )

    monkeypatch.setattr(instances_router, "validate_target_project_isolation", refuse_project)

    response = await instances_router.preflight_create_instance(
        request=request,
        lxd_service=SimpleNamespace(),
        db=SimpleNamespace(),
        _rbac=SimpleNamespace(user_id="user-123"),
    )

    assert response.ready is False
    assert response.blockers[-1].code == "shared_cluster_project_not_isolated"


class _Response:
    def json(self):
        return {"operation": "/1.0/operations/op-accepted"}


class _Collection:
    def __init__(self, instance=None):
        self.instance = instance

    def exists(self, _name):
        return False

    def get(self, _name):
        if self.instance is None:
            raise AssertionError("readback was not configured")
        return self.instance


class _CreateClient:
    def __init__(self, post_error=None, instance=None):
        self.instances = _Collection(instance)
        self.operations = SimpleNamespace(wait_for_operation=lambda _operation_id: None)
        self.post_calls = 0

        def post(*, json):
            del json
            self.post_calls += 1
            if post_error:
                raise post_error
            return _Response()

        self.api = SimpleNamespace(instances=SimpleNamespace(post=post))


async def _direct_blocking(fn, *args, **kwargs):
    for key in (
        "timeout",
        "label",
        "executor",
        "max_in_flight",
        "concurrency_key",
        "max_in_flight_per_key",
    ):
        kwargs.pop(key, None)
    return fn(*args, **kwargs)


@pytest.mark.asyncio
async def test_manager_records_operation_before_returning_accepted(monkeypatch):
    request = _request(start_on_create=False)
    row = _job(request)
    client = _CreateClient()
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace(get_project_client=lambda *_args: client)
    manager._bg_tasks = set()
    manager._get_client_with_reconnect = lambda _host: client
    progress = []
    blocking_limits = []

    async def claim(*_args, **_kwargs):
        return True

    async def load(_job_id):
        return row

    async def progress_update(_job_id, **values):
        progress.append(values)
        return True

    async def watcher(*_args, **kwargs):
        await instance_module._stop_create_heartbeat(kwargs.get("heartbeat_task"))

    async def bounded_call(fn, *args, **kwargs):
        blocking_limits.append(
            (kwargs.get("concurrency_key"), kwargs.get("max_in_flight_per_key"))
        )
        return await _direct_blocking(fn, *args, **kwargs)

    monkeypatch.setattr(instance_module, "_claim_create_job", claim)
    monkeypatch.setattr(instance_module, "_load_create_job", load)
    monkeypatch.setattr(instance_module, "_set_create_job", progress_update)
    monkeypatch.setattr(instance_module, "run_blocking", bounded_call)
    monkeypatch.setattr(instance_module, "check_nics", lambda *_args: None)
    monkeypatch.setattr(manager, "_watch_create_operation", watcher)

    operation_id = await manager.create_instance(
        request.name,
        request.host,
        request.image,
        request.instance_type,
        request.profiles,
        request.config,
        request.cloud_init,
        request.devices,
        False,
        request.project,
        job_id=row.job_id,
    )
    await asyncio.sleep(0)

    assert operation_id == "op-accepted"
    assert client.post_calls == 1
    assert [item["phase"] for item in progress] == [
        "preflighting",
        "submitting",
        "verifying",
    ]
    assert progress[-1]["operation_id"] == "op-accepted"
    assert blocking_limits
    assert set(blocking_limits) == {
        (request.host, instance_module._CREATE_MAX_IN_FLIGHT_PER_CLUSTER)
    }


@pytest.mark.asyncio
async def test_submit_timeout_becomes_unknown_and_is_not_reported_as_rejected(monkeypatch):
    request = _request(start_on_create=False)
    row = _job(request)
    client = _CreateClient(post_error=requests_exc.Timeout("socket timed out"))
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace(get_project_client=lambda *_args: client)
    manager._bg_tasks = set()
    manager._get_client_with_reconnect = lambda _host: client
    unknown = []

    monkeypatch.setattr(instance_module, "_claim_create_job", lambda *_args, **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(instance_module, "_load_create_job", lambda _job_id: asyncio.sleep(0, result=row))
    monkeypatch.setattr(instance_module, "_set_create_job", lambda *_args, **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)
    monkeypatch.setattr(instance_module, "check_nics", lambda *_args: None)

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)

    with pytest.raises(CreateSubmissionUnknown):
        await manager.create_instance(
            request.name,
            request.host,
            request.image,
            request.instance_type,
            request.profiles,
            request.config,
            request.cloud_init,
            request.devices,
            False,
            request.project,
            job_id=row.job_id,
        )

    assert client.post_calls == 1
    assert unknown and unknown[0]["result"] == {"ambiguous_action": "create"}


@pytest.mark.asyncio
async def test_accepted_create_with_failed_operation_persistence_is_unknown(monkeypatch):
    request = _request(start_on_create=False)
    row = _job(request)
    client = _CreateClient()
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace(get_project_client=lambda *_args: client)
    manager._bg_tasks = set()
    manager._get_client_with_reconnect = lambda _host: client
    phases = []

    async def progress_update(_job_id, **values):
        phases.append(values["phase"])
        if values["phase"] == "verifying":
            raise RuntimeError("database temporarily unavailable")
        return True

    monkeypatch.setattr(
        instance_module,
        "_claim_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(instance_module, "_set_create_job", progress_update)
    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)
    monkeypatch.setattr(instance_module, "check_nics", lambda *_args: None)

    with pytest.raises(CreateSubmissionUnknown) as exc:
        await manager.create_instance(
            request.name,
            request.host,
            request.image,
            request.instance_type,
            request.profiles,
            request.config,
            request.cloud_init,
            request.devices,
            False,
            request.project,
            job_id=row.job_id,
        )

    assert client.post_calls == 1
    assert phases == ["preflighting", "submitting", "verifying"]
    assert exc.value.operation_id == "op-accepted"


@pytest.mark.asyncio
async def test_terminal_success_requires_exact_readback(monkeypatch):
    request = _request(start_on_create=False)
    row = _job(request, status="running", phase="verifying", operation_id="op-1")
    instance = SimpleNamespace(status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    terminal = []

    monkeypatch.setattr(instance_module, "_load_create_job", lambda _job_id: asyncio.sleep(0, result=row))
    monkeypatch.setattr(instance_module, "_set_create_job", lambda *_args, **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(
        manager, "_wait_for_create_operation", lambda *_args: asyncio.sleep(0, result=("success", None))
    )
    monkeypatch.setattr(
        manager, "_read_created_instance", lambda *_args: asyncio.sleep(0, result=(instance, True, None))
    )

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._reconcile_durable_create(SimpleNamespace(), row.job_id, worker_id="worker", recovery=False)

    assert terminal[0][0] == "success"
    assert terminal[0][1]["result"]["instance_status"] == "Stopped"
    assert terminal[0][1]["announce_created"] is True


@pytest.mark.asyncio
async def test_recovery_running_create_with_readback_stays_unknown_and_never_starts(
    monkeypatch,
):
    request = _request(start_on_create=True)
    row = _job(request, status="unknown", phase="verifying", operation_id="op-running")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    evidence_order = []
    unknown = []
    start_calls = []
    manager._submit_start_operation = lambda *_args: start_calls.append(True)

    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(
        instance_module,
        "_set_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )

    async def wait_for_operation(*_args):
        evidence_order.append("operation")
        return "unknown", "operation is still running"

    async def read_instance(*_args):
        evidence_order.append("readback")
        return stopped, True, None

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(manager, "_wait_for_create_operation", wait_for_operation)
    monkeypatch.setattr(manager, "_read_created_instance", read_instance)
    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)
    monkeypatch.setattr(
        instance_module,
        "_finish_create_job",
        lambda *_args, **_kwargs: pytest.fail(
            "a non-terminal create operation was reported as terminal"
        ),
    )

    await manager._reconcile_durable_create(
        SimpleNamespace(), row.job_id, worker_id=row.locked_by, recovery=True
    )

    assert evidence_order == ["operation", "readback"]
    assert start_calls == []
    assert unknown[0]["operation_id"] == "op-running"
    assert unknown[0]["result"]["ambiguous_action"] == "create"


@pytest.mark.asyncio
async def test_recovery_failed_create_with_observed_resource_is_partial(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="unknown", phase="verifying", operation_id="op-failed")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    evidence_order = []
    terminal = []
    start_calls = []
    manager._submit_start_operation = lambda *_args: start_calls.append(True)

    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(
        instance_module,
        "_set_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )

    async def wait_for_operation(*_args):
        evidence_order.append("operation")
        return "failure", "storage allocation failed"

    async def read_instance(*_args):
        evidence_order.append("readback")
        return stopped, True, None

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(manager, "_wait_for_create_operation", wait_for_operation)
    monkeypatch.setattr(manager, "_read_created_instance", read_instance)
    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._reconcile_durable_create(
        SimpleNamespace(), row.job_id, worker_id=row.locked_by, recovery=True
    )

    assert evidence_order == ["operation", "readback"]
    assert start_calls == []
    assert terminal[0][0] == "partial"
    assert terminal[0][1]["result"]["instance_status"] == "Stopped"
    assert "reported failure" in terminal[0][1]["error"]


@pytest.mark.asyncio
async def test_recovery_success_requires_terminal_success_then_exact_readback(monkeypatch):
    request = _request(start_on_create=False)
    row = _job(request, status="unknown", phase="verifying", operation_id="op-success")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    evidence_order = []
    terminal = []

    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(
        instance_module,
        "_set_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )

    async def wait_for_operation(*_args):
        evidence_order.append("operation")
        return "success", None

    async def read_instance(*_args):
        evidence_order.append("readback")
        return stopped, True, None

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(manager, "_wait_for_create_operation", wait_for_operation)
    monkeypatch.setattr(manager, "_read_created_instance", read_instance)
    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._reconcile_durable_create(
        SimpleNamespace(), row.job_id, worker_id=row.locked_by, recovery=True
    )

    assert evidence_order == ["operation", "readback"]
    assert terminal[0][0] == "success"
    assert terminal[0][1]["result"]["instance_status"] == "Stopped"


@pytest.mark.asyncio
async def test_recovery_expired_operation_with_readback_is_partial_without_restart(
    monkeypatch,
):
    request = _request(start_on_create=True)
    row = _job(request, status="unknown", phase="verifying", operation_id="op-expired")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    terminal = []
    start_calls = []
    manager._submit_start_operation = lambda *_args: start_calls.append(True)
    expired_response = SimpleNamespace(
        status_code=404,
        json=lambda: {"error": "operation not found"},
        content=b"operation not found",
    )

    def wait_for_expired_operation(_operation_id):
        raise instance_module.NotFound(expired_response)

    client = SimpleNamespace(
        operations=SimpleNamespace(wait_for_operation=wait_for_expired_operation)
    )

    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(
        instance_module,
        "_set_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)
    monkeypatch.setattr(
        manager,
        "_read_created_instance",
        lambda *_args: asyncio.sleep(0, result=(stopped, True, None)),
    )

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._reconcile_durable_create(
        client, row.job_id, worker_id=row.locked_by, recovery=True
    )

    assert start_calls == []
    assert terminal[0][0] == "partial"
    assert "expired" in terminal[0][1]["error"]


@pytest.mark.asyncio
async def test_start_failure_is_partial_after_stopped_readback(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="running", phase="starting", operation_id="op-1")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    terminal = []
    manager._submit_start_operation = lambda *_args: (_ for _ in ()).throw(RuntimeError("boot failed"))

    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)
    monkeypatch.setattr(instance_module, "_set_create_job", lambda *_args, **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(
        manager, "_read_created_instance", lambda *_args: asyncio.sleep(0, result=(stopped, True, None))
    )

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._complete_durable_create(SimpleNamespace(), row, stopped, row.operation_id)

    assert terminal[0][0] == "partial"
    assert terminal[0][1]["result"]["instance_status"] == "Stopped"


@pytest.mark.asyncio
async def test_ambiguous_start_submission_with_running_readback_stays_unknown(
    monkeypatch,
):
    request = _request(start_on_create=True)
    row = _job(request, status="running", phase="verifying", operation_id="create-op")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    running = SimpleNamespace(name=request.name, status="Running")
    manager = InstanceManager.__new__(InstanceManager)
    unknown = []
    manager._submit_start_operation = lambda *_args: (_ for _ in ()).throw(
        requests_exc.Timeout("start response timed out")
    )

    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)
    monkeypatch.setattr(
        instance_module,
        "_set_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        manager,
        "_read_created_instance",
        lambda *_args: asyncio.sleep(0, result=(running, True, None)),
    )

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)
    monkeypatch.setattr(
        instance_module,
        "_finish_create_job",
        lambda *_args, **_kwargs: pytest.fail(
            "Running readback converted an ambiguous start submission into success"
        ),
    )

    await manager._complete_durable_create(
        SimpleNamespace(), row, stopped, row.operation_id
    )

    assert unknown[0]["operation_id"] == "create-op"
    assert unknown[0]["result"]["ambiguous_action"] == "start"
    assert unknown[0]["result"]["instance_status"] == "Running"


@pytest.mark.asyncio
async def test_accepted_start_keeps_its_operation_when_followup_persistence_fails(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="running", phase="verifying", operation_id="create-op")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    start_calls = []
    unknown = []
    updates = 0

    def submit_start(*_args):
        start_calls.append(True)
        return "start-op"

    async def update_job(*_args, **_kwargs):
        nonlocal updates
        updates += 1
        if updates == 1:
            return True
        raise RuntimeError("database temporarily unavailable")

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    manager._submit_start_operation = submit_start
    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)
    monkeypatch.setattr(instance_module, "_set_create_job", update_job)
    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)

    await manager._complete_durable_create(
        SimpleNamespace(), row, stopped, row.operation_id
    )

    assert start_calls == [True]
    assert unknown[0]["operation_id"] == "start-op"
    assert unknown[0]["result"]["ambiguous_action"] == "start"
    assert unknown[0]["result"]["start_operation_id"] == "start-op"


@pytest.mark.asyncio
async def test_ambiguous_start_recovery_never_submits_start_again(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(
        request,
        status="unknown",
        phase="starting",
        operation_id="create-op",
        result={
            "ambiguous_action": "start",
            "create_operation_id": "create-op",
            "start_operation_id": None,
        },
    )
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    unknown = []
    manager._submit_start_operation = lambda *_args: pytest.fail("recovery replayed an ambiguous start")

    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(
        manager,
        "_read_created_instance",
        lambda *_args: asyncio.sleep(0, result=(stopped, True, None)),
    )

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)

    await manager._reconcile_durable_create(SimpleNamespace(), row.job_id, worker_id="worker-1", recovery=True)

    assert unknown
    assert unknown[0]["result"]["ambiguous_action"] == "start"


@pytest.mark.asyncio
async def test_running_readback_cannot_complete_an_unknown_start_operation(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="unknown", phase="starting", operation_id="start-op")
    running = SimpleNamespace(name=request.name, status="Running")
    manager = InstanceManager.__new__(InstanceManager)
    evidence_order = []
    unknown = []

    async def wait_for_operation(*_args):
        evidence_order.append("operation")
        return "unknown", "operation is still running"

    async def read_instance(*_args):
        evidence_order.append("readback")
        return running, True, None

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(manager, "_wait_for_create_operation", wait_for_operation)
    monkeypatch.setattr(manager, "_read_created_instance", read_instance)
    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)
    monkeypatch.setattr(
        instance_module,
        "_finish_create_job",
        lambda *_args, **_kwargs: pytest.fail(
            "Running readback bypassed the non-terminal start operation"
        ),
    )

    await manager._verify_durable_start(
        SimpleNamespace(),
        row,
        create_operation_id="create-op",
        start_operation_id="start-op",
    )

    assert evidence_order == ["operation", "readback"]
    assert unknown[0]["operation_id"] == "start-op"
    assert unknown[0]["result"]["instance_status"] == "Running"


@pytest.mark.asyncio
async def test_running_readback_with_failed_start_operation_is_partial(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="unknown", phase="starting", operation_id="start-op")
    running = SimpleNamespace(name=request.name, status="Running")
    manager = InstanceManager.__new__(InstanceManager)
    terminal = []

    monkeypatch.setattr(
        manager,
        "_wait_for_create_operation",
        lambda *_args: asyncio.sleep(0, result=("failure", "start hook failed")),
    )
    monkeypatch.setattr(
        manager,
        "_read_created_instance",
        lambda *_args: asyncio.sleep(0, result=(running, True, None)),
    )

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._verify_durable_start(
        SimpleNamespace(),
        row,
        create_operation_id="create-op",
        start_operation_id="start-op",
    )

    assert terminal[0][0] == "partial"
    assert terminal[0][1]["operation_id"] == "start-op"
    assert "reported failure" in terminal[0][1]["error"]


@pytest.mark.asyncio
async def test_start_success_requires_terminal_success_then_running_readback(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="running", phase="starting", operation_id="start-op")
    running = SimpleNamespace(name=request.name, status="Running")
    manager = InstanceManager.__new__(InstanceManager)
    evidence_order = []
    terminal = []

    async def wait_for_operation(*_args):
        evidence_order.append("operation")
        return "success", None

    async def read_instance(*_args):
        evidence_order.append("readback")
        return running, True, None

    async def finish(_row, status, **kwargs):
        terminal.append((status, kwargs))
        return True

    monkeypatch.setattr(manager, "_wait_for_create_operation", wait_for_operation)
    monkeypatch.setattr(manager, "_read_created_instance", read_instance)
    monkeypatch.setattr(instance_module, "_finish_create_job", finish)

    await manager._verify_durable_start(
        SimpleNamespace(),
        row,
        create_operation_id="create-op",
        start_operation_id="start-op",
    )

    assert evidence_order == ["operation", "readback"]
    assert terminal[0][0] == "success"
    assert terminal[0][1]["operation_id"] == "start-op"
    assert terminal[0][1]["result"]["instance_status"] == "Running"


@pytest.mark.asyncio
async def test_running_readback_without_start_operation_id_stays_unknown(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="unknown", phase="starting", operation_id="create-op")
    running = SimpleNamespace(name=request.name, status="Running")
    manager = InstanceManager.__new__(InstanceManager)
    unknown = []

    monkeypatch.setattr(
        manager,
        "_wait_for_create_operation",
        lambda *_args: pytest.fail("missing start operation id was still waited on"),
    )
    monkeypatch.setattr(
        manager,
        "_read_created_instance",
        lambda *_args: asyncio.sleep(0, result=(running, True, None)),
    )

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)
    monkeypatch.setattr(
        instance_module,
        "_finish_create_job",
        lambda *_args, **_kwargs: pytest.fail(
            "uncorrelated Running readback was reported as terminal"
        ),
    )

    await manager._verify_durable_start(
        SimpleNamespace(),
        row,
        create_operation_id="create-op",
        start_operation_id=None,
    )

    assert unknown[0]["operation_id"] == "create-op"
    assert unknown[0]["result"]["instance_status"] == "Running"
    assert unknown[0]["result"]["ambiguous_action"] == "start"


@pytest.mark.asyncio
async def test_ambiguous_create_readback_never_submits_an_uncorrelated_start(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(
        request,
        status="unknown",
        phase="unknown",
        operation_id=None,
        result={"ambiguous_action": "create"},
    )
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    unknown = []
    manager._submit_start_operation = lambda *_args: pytest.fail(
        "recovery started an instance from an uncorrelated create"
    )

    monkeypatch.setattr(
        instance_module,
        "_load_create_job",
        lambda _job_id: asyncio.sleep(0, result=row),
    )
    monkeypatch.setattr(
        manager,
        "_read_created_instance",
        lambda *_args: asyncio.sleep(0, result=(stopped, True, None)),
    )

    async def mark_unknown(_row, **kwargs):
        unknown.append(kwargs)
        return True

    monkeypatch.setattr(instance_module, "_mark_create_unknown", mark_unknown)
    monkeypatch.setattr(
        instance_module,
        "_finish_create_job",
        lambda *_args, **_kwargs: pytest.fail(
            "uncorrelated create readback was reported as terminal"
        ),
    )

    await manager._reconcile_durable_create(
        SimpleNamespace(), row.job_id, worker_id="worker-1", recovery=True
    )

    assert unknown[0]["operation_id"] is None
    assert unknown[0]["result"]["instance_status"] == "Stopped"
    assert unknown[0]["result"]["ambiguous_action"] == "create"


@pytest.mark.asyncio
async def test_lost_lease_fences_start_before_daemon_write(monkeypatch):
    request = _request(start_on_create=True)
    row = _job(request, status="running", phase="verifying", operation_id="create-op")
    stopped = SimpleNamespace(name=request.name, status="Stopped")
    manager = InstanceManager.__new__(InstanceManager)
    start_calls = []
    manager._submit_start_operation = lambda *_args: start_calls.append(True)

    monkeypatch.setattr(
        instance_module,
        "_set_create_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
    )

    await manager._complete_durable_create(SimpleNamespace(), row, stopped, row.operation_id)

    assert instance_module._CREATE_HEARTBEAT_SECONDS * 3 <= instance_module._CREATE_LEASE_SECONDS
    assert instance_module._CREATE_LEASE_SECONDS <= 5 * 60
    assert start_calls == []


@pytest.mark.asyncio
async def test_create_lease_heartbeat_renews_until_ownership_is_lost(monkeypatch):
    renewals = []

    async def no_wait(_seconds):
        return None

    async def renew(job_id, **values):
        renewals.append((job_id, values))
        return len(renewals) == 1

    monkeypatch.setattr(instance_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(instance_module, "_set_create_job", renew)

    await instance_module._create_lease_heartbeat("job-1", "worker-1")

    assert len(renewals) == 2
    assert all(item[0] == "job-1" for item in renewals)
    assert all(item[1]["worker_id"] == "worker-1" for item in renewals)
    assert all("locked_at" in item[1] for item in renewals)


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _UpdateResult:
    rowcount = 1


class _JobSession:
    def __init__(self, row):
        self.row = row
        self.select_statement = None
        self.update_statement = None

    async def execute(self, statement):
        if getattr(statement, "is_select", False):
            self.select_statement = statement
            return _ScalarResult(self.row)
        self.update_statement = statement
        return _UpdateResult()

    async def commit(self):
        return None


def _statement_values(statement):
    return {
        getattr(column, "key", str(column)): getattr(value, "value", value)
        for column, value in statement._values.items()
    }


@pytest.mark.asyncio
async def test_job_update_appends_unique_operation_ids_under_row_lock(monkeypatch):
    request = _request()
    row = _job(request, operation_id="create-op", operation_ids=["create-op"])
    db = _JobSession(row)

    @asynccontextmanager
    async def session_maker():
        yield db

    monkeypatch.setattr(instance_module, "async_session_maker", session_maker)

    updated = await instance_module._set_create_job(
        row.job_id,
        worker_id=row.locked_by,
        operation_id="start-op",
    )

    assert updated is True
    assert db.select_statement._for_update_arg is not None
    assert _statement_values(db.update_statement)["operation_ids"] == [
        "create-op",
        "start-op",
    ]


@pytest.mark.asyncio
async def test_terminal_create_audit_correlates_create_and_start_operations(monkeypatch):
    request = _request()
    row = _job(request, operation_id="create-op", operation_ids=["create-op"])
    db = _JobSession(row)
    audits = []

    @asynccontextmanager
    async def session_maker():
        yield db

    monkeypatch.setattr(instance_module, "async_session_maker", session_maker)
    monkeypatch.setattr(
        instance_module,
        "get_activity_logger",
        lambda: SimpleNamespace(log_activity=lambda **values: audits.append(values)),
    )

    transitioned = await instance_module._finish_create_job(
        row,
        "partial",
        operation_id="start-op",
        result={"instance_status": "Stopped"},
        error="start refused",
    )

    assert transitioned is True
    assert db.select_statement._for_update_arg is not None
    assert _statement_values(db.update_statement)["operation_ids"] == [
        "create-op",
        "start-op",
    ]
    assert audits[0]["details"]["operation_ids"] == ["create-op", "start-op"]
    assert audits[0]["target_organization_id"] == 17
    assert audits[0]["target_organization_slug"] == "org-seventeen"
    assert audits[0]["target_organization_resolution"] == "exact"


@pytest.mark.asyncio
async def test_recovery_reads_exact_project_and_never_submits_create(monkeypatch):
    request = _request(start_on_create=False)
    row = _job(request, status="unknown", phase="submitting")
    client = _CreateClient()
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace(
        get_project_client=lambda host, project, _timeout: (
            client
            if (host, project) == (request.host, request.project)
            else pytest.fail("recovery opened the wrong project")
        )
    )
    reconciled = []

    monkeypatch.setattr(instance_module, "_claim_create_job", lambda *_args, **_kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(instance_module, "_load_create_job", lambda _job_id: asyncio.sleep(0, result=row))
    monkeypatch.setattr(instance_module, "run_blocking", _direct_blocking)

    async def reconcile(_client, job_id, **kwargs):
        reconciled.append((job_id, kwargs))

    monkeypatch.setattr(manager, "_reconcile_durable_create", reconcile)

    await manager._recover_create_job(row.job_id)

    assert client.post_calls == 0
    assert reconciled == [(row.job_id, {"worker_id": ANY, "recovery": True})]


def test_startup_wires_create_recovery_and_recovery_has_no_create_call():
    main_source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "recover_create_operation_jobs" in main_source
    assert ".post(" not in inspect.getsource(InstanceManager._reconcile_durable_create)
