"""A durable specification job is reconciled at each crash boundary, never replayed."""

from types import SimpleNamespace

import pytest
import requests

from app.services import instance_specifications as service
from app.services.instance_specifications import SpecificationRead, SpecificationWriteUnknown
from app.services.lxd.project_usage import ProjectUsage

GIB = 1024**3


def _read(*, cpu: int, status: str) -> SpecificationRead:
    public = {
        "instance_name": "vm-1",
        "host": "cluster-a",
        "project": "project-a",
        "type": "virtual-machine",
        "status": status,
        "cpu": {"value": cpu, "source": "instance"},
        "memory": {"value_bytes": 4 * GIB, "source": "instance"},
        "root_disk": {
            "value_bytes": 10 * GIB,
            "pool": "pool-a",
            "source": "instance",
            "editable": True,
            "pending_quota_change": False,
            "reason": None,
        },
        "capabilities": {"live_update": True, "restart_supported": True},
        "etag": '"v1"',
    }
    return SpecificationRead(
        public=public,
        raw={
            "config": {"limits.cpu": str(cpu), "limits.memory": "4GiB"},
            "expanded_config": {"limits.cpu": str(cpu), "limits.memory": "4GiB"},
            "devices": {},
            "expanded_devices": {},
        },
        etag='"v1"',
        root_device_name="root",
    )


def _pending_quota_read(*, cpu: int, status: str) -> SpecificationRead:
    current = _read(cpu=cpu, status=status)
    current.public["root_disk"].update(
        {
            "editable": False,
            "pending_quota_change": True,
            "reason": "Incus reports a deferred root-disk quota change.",
        }
    )
    current.raw["config"]["volatile.root.apply_quota"] = "true"
    current.raw["expanded_config"]["volatile.root.apply_quota"] = "true"
    return current


def _row(**overrides):
    values = {
        "job_id": "job-1",
        "kind": "specification",
        "cluster_id": "cluster-a",
        "project": "project-a",
        "instance_name": "vm-1",
        "requester": "user-1",
        "target_organization_id": 17,
        "target_organization_slug": "org-seventeen",
        "target_organization_resolution": "exact",
        "requested": {"cpu_count": 6},
        "target_spec": {"execution": "restart_required", "acknowledgements": ["restart_instance"]},
        "before_spec": {},
        "plan_revision": "original-running-plan",
        "apply_mode": "graceful_restart",
        "status": "running",
        "phase": "stopping",
        "operation_id": "stop-op",
        "operation_ids": ["stop-op"],
        "original_status": "Running",
        "stopped_by_job": False,
        "result": None,
        "locked_by": "worker-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pending_quota(read: SpecificationRead) -> SpecificationRead:
    read.public["root_disk"]["pending_quota_change"] = True
    read.public["root_disk"]["editable"] = False
    read.public["root_disk"]["reason"] = "A deferred root-disk quota change is pending."
    return read


@pytest.mark.asyncio
async def test_recovery_after_graceful_stop_applies_then_restarts(monkeypatch):
    """After the job's own graceful stop, recovery applies the change and restarts."""
    row = _row(phase="applying", stopped_by_job=True)
    stopped_before = _read(cpu=4, status="Stopped")
    stopped_after = _read(cpu=6, status="Stopped")
    running_after = _read(cpu=6, status="Running")
    reads = iter([stopped_before, stopped_after, running_after])
    calls: list[str] = []
    terminal = []

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            return ProjectUsage(limits={}, used={}, complete=True)
        if fn is service.apply_specification_sync:
            calls.append("apply")
            return stopped_after, "apply-op"
        if fn is service.change_instance_state_sync:
            calls.append(args[-1])
            return "start-op"
        raise AssertionError(f"unexpected bounded call: {fn}")

    async def set_job(*args, **kwargs):
        return True

    async def finish(*args, **kwargs):
        terminal.append((args, kwargs))

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(next(reads)))
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_terminal", finish)
    monkeypatch.setattr(
        service,
        "build_preflight",
        lambda *args, **kwargs: {
            "plan_revision": "stopped-runtime-plan",
            "blockers": [],
            "execution": "stopped",
        },
    )
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert calls == ["apply", "start"]
    assert terminal[-1][0][1:3] == ("success",)
    assert terminal[-1][1]["result"]["specification"]["status"] == "Running"


@pytest.mark.asyncio
async def test_a_known_graceful_stop_failure_never_submits_the_update(monkeypatch):
    row = _row(
        phase="pending",
        operation_id=None,
        operation_ids=[],
        original_status=None,
    )
    current = _read(cpu=4, status="Running")
    calls: list[str] = []
    terminal = []

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            return ProjectUsage(limits={}, used={}, complete=True)
        if fn is service.change_instance_state_sync:
            calls.append(args[-1])
            raise RuntimeError("guest refused the graceful stop")
        if fn is service.apply_specification_sync:
            raise AssertionError("the update must not run after a known stop failure")
        raise AssertionError(f"unexpected bounded call: {fn}")

    async def finish(*args, **kwargs):
        terminal.append((args, kwargs))

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(current))
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_terminal", finish)
    monkeypatch.setattr(
        service,
        "build_preflight",
        lambda *args, **kwargs: {
            "plan_revision": "original-running-plan",
            "blockers": [],
            "execution": "restart_required",
        },
    )
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert calls == ["stop"]
    assert terminal[-1][0][1] == "failure"
    assert "graceful stop" in terminal[-1][1]["error"]


@pytest.mark.asyncio
async def test_a_known_update_failure_restores_the_instance_before_reporting_failure(monkeypatch):
    row = _row(phase="applying", stopped_by_job=True)
    stopped_before = _read(cpu=4, status="Stopped")
    running_restored = _read(cpu=4, status="Running")
    reads = iter([stopped_before, stopped_before, running_restored])
    calls: list[str] = []
    terminal = []

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            return ProjectUsage(limits={}, used={}, complete=True)
        if fn is service.apply_specification_sync:
            calls.append("apply")
            raise RuntimeError("daemon refused the update")
        if fn is service.change_instance_state_sync:
            calls.append(args[-1])
            return "restore-start-op"
        raise AssertionError(f"unexpected bounded call: {fn}")

    async def finish(*args, **kwargs):
        terminal.append((args, kwargs))

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(next(reads)))
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_terminal", finish)
    monkeypatch.setattr(
        service,
        "build_preflight",
        lambda *args, **kwargs: {
            "plan_revision": "stopped-runtime-plan",
            "blockers": [],
            "execution": "stopped",
        },
    )
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert calls == ["apply", "start"]
    assert terminal[-1][0][1] == "failure"
    assert terminal[-1][1]["result"]["instance_status"] == "Running"
    assert "daemon refused the update" in terminal[-1][1]["error"]


@pytest.mark.parametrize("helper_name", ["complete", "restore"])
@pytest.mark.parametrize("guard_result", ["pending", "unreadable"])
@pytest.mark.asyncio
async def test_automatic_start_is_withheld_when_root_safety_is_not_proven(
    monkeypatch,
    helper_name,
    guard_result,
):
    row = _row(operation_id="apply-op", stopped_by_job=True, phase="verifying")
    current = _read(cpu=6 if helper_name == "complete" else 4, status="Stopped")
    terminal = []

    async def guarded_read(*args, **kwargs):
        if guard_result == "unreadable":
            raise service.SpecificationUnavailable("fresh read failed")
        return _pending_quota_read(cpu=current.public["cpu"]["value"], status="Stopped")

    async def must_not_start(*args, **kwargs):
        raise AssertionError("automatic start must be withheld")

    monkeypatch.setattr(service, "_read_for_job", guarded_read)
    monkeypatch.setattr(service, "_bounded", must_not_start)
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: _capture_async(terminal, args, kwargs),
    )

    if helper_name == "complete":
        await service._complete_restart_after_apply(row, object(), current)
    else:
        await service._restore_after_failed_apply(
            row,
            object(),
            current,
            "the update was refused",
        )

    assert terminal[-1][0][1] == "partial"
    assert "withheld" in terminal[-1][1]["error"]


@pytest.mark.parametrize("verdict", ["success", "failure", "expired"])
@pytest.mark.asyncio
async def test_unknown_apply_recovery_never_starts_with_pending_root_quota(
    monkeypatch,
    verdict,
):
    row = _row(
        status="unknown",
        phase="unknown",
        operation_id="apply-op",
        stopped_by_job=True,
        result={"ambiguous_action": "apply"},
        requested={"cpu_count": 6},
    )
    pending = _pending_quota_read(cpu=6, status="Stopped")
    terminal = []

    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async(
            {
                "verdict": verdict,
                "error": "daemon refused the update" if verdict == "failure" else None,
            }
        ),
    )
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(pending))
    monkeypatch.setattr(
        service,
        "_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not start an instance with pending root quota")
        ),
    )
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: _capture_async(terminal, args, kwargs),
    )

    await service._reconcile_unknown(row, object())

    assert terminal[-1][0][1] == "partial"
    assert "deferred root-disk quota" in terminal[-1][1]["error"]


@pytest.mark.asyncio
async def test_pending_quota_after_graceful_stop_prevents_put_and_restore_start(monkeypatch):
    row = _row(phase="applying", stopped_by_job=True)
    pending = _pending_quota_read(cpu=4, status="Stopped")
    calls = []
    terminal = []

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            calls.append("usage")
            return ProjectUsage(limits={}, used={}, complete=True)
        calls.append(getattr(fn, "__name__", str(fn)))
        raise AssertionError("pending quota must prevent every daemon mutation")

    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(pending))
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: _capture_async(terminal, args, kwargs),
    )
    service._cluster_locks.pop(row.cluster_id, None)

    await service._run_claimed_specification_job(row, "worker-1")

    assert calls == []
    assert terminal[-1][0][1] == "partial"
    assert "deferred root-disk quota" in terminal[-1][1]["error"]


@pytest.mark.asyncio
async def test_recovery_at_submitting_boundary_never_replays_put(monkeypatch):
    row = _row(phase="submitting", original_status=None, stopped_by_job=False)
    current = _read(cpu=4, status="Running")
    updates = []
    scheduled = []

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(current))
    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    async def must_not_run(*args, **kwargs):
        raise AssertionError("an ambiguous PUT must not be submitted again")

    def schedule(coroutine):
        if coroutine.cr_code.co_name == "_lease_heartbeat":
            coroutine.close()
            return None
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async({"verdict": "running", "error": None}),
    )
    monkeypatch.setattr(service, "_bounded", must_not_run)
    monkeypatch.setattr(service.asyncio, "create_task", schedule)
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert updates[-1]["locked_by"] is None
    assert updates[-1]["locked_at"] is None
    assert "phase" not in updates[-1]
    assert len(scheduled) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stopping", "submitting", "starting", "verifying"])
async def test_repeated_recovery_read_failure_preserves_each_ambiguity_marker(
    monkeypatch,
    phase,
):
    row = _row(phase=phase, status="running")
    updates = []
    terminal = []
    scheduled = []

    async def unreadable(*args, **kwargs):
        raise service.SpecificationUnavailable("readback unavailable")

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    async def finish(*args, **kwargs):
        terminal.append((args, kwargs))

    def schedule(coroutine):
        if coroutine.cr_code.co_name == "_lease_heartbeat":
            coroutine.close()
            return None
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", unreadable)
    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async({"verdict": "running", "error": None}),
    )
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_terminal", finish)
    monkeypatch.setattr(service.asyncio, "create_task", schedule)
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert terminal == []
    assert updates[-1]["locked_by"] is None
    assert updates[-1]["locked_at"] is None
    assert "phase" not in updates[-1]
    assert "status" not in updates[-1]
    assert len(scheduled) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected_status"),
    [("running", None), ("failure", "partial"), ("success", "success")],
)
async def test_matching_readback_waits_for_the_apply_operations_terminal_verdict(
    monkeypatch,
    verdict,
    expected_status,
):
    row = _row(
        status="unknown",
        phase="unknown",
        result={"ambiguous_action": "apply"},
        operation_id="apply-op",
        stopped_by_job=False,
    )
    current = _read(cpu=6, status="Running")
    terminal = []
    updates = []
    scheduled = []

    async def finish(*args, **kwargs):
        terminal.append((args, kwargs))

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    def schedule(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async(
            {
                "verdict": verdict,
                "error": "daemon refused" if verdict == "failure" else None,
            }
        ),
    )
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(current))
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_terminal", finish)
    monkeypatch.setattr(service.asyncio, "create_task", schedule)

    await service._reconcile_unknown(row, object())

    if expected_status is None:
        assert terminal == []
        assert updates[-1]["locked_by"] is None
        assert len(scheduled) == 1
    else:
        assert terminal[-1][0][1] == expected_status
        assert scheduled == []


@pytest.mark.asyncio
async def test_worker_losing_lease_before_submit_never_writes(monkeypatch):
    row = _row(
        phase="pending",
        original_status=None,
        stopped_by_job=False,
        plan_revision="current-plan",
    )
    current = _read(cpu=4, status="Running")
    phases = []

    async def set_job(*args, **kwargs):
        phases.append(kwargs.get("phase"))
        return kwargs.get("phase") != "submitting"

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            return ProjectUsage(limits={}, used={}, complete=True)
        if fn is service.apply_specification_sync:
            raise AssertionError("a worker without the lease submitted a specification PUT")
        raise AssertionError(f"unexpected bounded call: {fn}")

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(current))
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(
        service,
        "build_preflight",
        lambda *args, **kwargs: {
            "plan_revision": "current-plan",
            "blockers": [],
            "execution": "live",
        },
    )
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert "submitting" in phases


@pytest.mark.asyncio
async def test_apply_timeout_without_new_operation_id_never_reuses_stop_operation(
    monkeypatch,
):
    row = _row(
        phase="pending",
        operation_id=None,
        operation_ids=[],
        original_status=None,
    )
    running = _read(cpu=4, status="Running")
    stopped = _read(cpu=4, status="Stopped")
    reads = iter([running, stopped])
    calls: list[str] = []
    updates = []
    unknown = []

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            return ProjectUsage(limits={}, used={}, complete=True)
        if fn is service.change_instance_state_sync:
            calls.append("stop")
            return "stop-op"
        if fn is service.apply_specification_sync:
            calls.append("apply")
            raise SpecificationWriteUnknown(None, requests.Timeout("submit timed out"))
        raise AssertionError(f"unexpected bounded call: {fn}")

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    async def mark_unknown(*args, **kwargs):
        unknown.append(kwargs)

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(next(reads)))
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_mark_unknown", mark_unknown)
    monkeypatch.setattr(
        service,
        "build_preflight",
        lambda *args, **kwargs: {
            "plan_revision": "original-running-plan",
            "blockers": [],
            "execution": "restart_required",
        },
    )
    service._cluster_locks.pop(row.cluster_id, None)

    await service.run_specification_job(row.job_id)

    assert calls == ["stop", "apply"]
    assert any(
        update.get("phase") == "submitting" and update.get("operation_id") is None
        for update in updates
    )
    assert unknown[-1]["action"] == "apply"
    assert unknown[-1]["operation_id"] is None


@pytest.mark.asyncio
async def test_recovered_stop_replaces_its_marker_before_an_ambiguous_apply(
    monkeypatch,
):
    stopped = _read(cpu=4, status="Stopped")
    stop_row = _row(
        status="unknown",
        phase="unknown",
        result={"ambiguous_action": "stop"},
        operation_id="stop-op",
        stopped_by_job=False,
    )
    updates = []
    scheduled = []

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    def schedule(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async({"verdict": "success", "error": None}),
    )
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(stopped))
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service.asyncio, "create_task", schedule)

    await service._reconcile_unknown(stop_row, object())

    stop_transition = updates[-1]
    assert stop_transition["phase"] == "applying"
    assert stop_transition["result"] is None
    assert len(scheduled) == 1

    resumed = _row(
        status="running",
        phase="applying",
        result=stop_transition["result"],
        operation_id="stop-op",
        stopped_by_job=True,
    )
    updates.clear()
    unknown = []

    async def bounded(fn, *args, **kwargs):
        if fn is service.read_project_usage:
            return ProjectUsage(limits={}, used={}, complete=True)
        if fn is service.apply_specification_sync:
            raise SpecificationWriteUnknown(None, requests.Timeout("apply submit timed out"))
        raise AssertionError(f"unexpected bounded call: {fn}")

    async def mark_unknown(*args, **kwargs):
        unknown.append(kwargs)

    monkeypatch.setattr(service, "get_client", lambda *args, **kwargs: _async(object()))
    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_mark_unknown", mark_unknown)
    monkeypatch.setattr(
        service,
        "build_preflight",
        lambda *args, **kwargs: {
            "plan_revision": "stopped-runtime-plan",
            "blockers": [],
            "execution": "stopped",
        },
    )
    service._cluster_locks.pop(resumed.cluster_id, None)

    await service._run_claimed_specification_job(resumed, "worker-1")

    submit_transition = next(
        update for update in updates if update.get("phase") == "submitting"
    )
    assert submit_transition["operation_id"] is None
    assert submit_transition["result"] == {"ambiguous_action": "apply"}
    assert unknown[-1]["action"] == "apply"
    assert unknown[-1]["operation_id"] is None


@pytest.mark.asyncio
async def test_start_timeout_without_new_operation_id_never_reuses_apply_operation(
    monkeypatch,
):
    row = _row(operation_id="apply-op", stopped_by_job=True, phase="verifying")
    stopped = _read(cpu=6, status="Stopped")
    updates = []
    unknown = []

    async def bounded(fn, *args, **kwargs):
        assert fn is service.change_instance_state_sync
        raise SpecificationWriteUnknown(None, requests.Timeout("start submit timed out"))

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    async def mark_unknown(*args, **kwargs):
        unknown.append(kwargs)

    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(
        service, "_read_for_job", lambda *args, **kwargs: _async(stopped)
    )
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_mark_unknown", mark_unknown)

    await service._complete_restart_after_apply(row, object(), stopped)

    assert updates[0]["phase"] == "starting"
    assert updates[0]["operation_id"] is None
    assert unknown[-1]["action"] == "start"
    assert unknown[-1]["operation_id"] is None


@pytest.mark.asyncio
async def test_start_operation_crash_marker_remains_starting_and_never_restarts(
    monkeypatch,
):
    row = _row(
        operation_id="start-op",
        operation_ids=["stop-op", "apply-op", "start-op"],
        stopped_by_job=True,
        phase="starting",
        result={
            "ambiguous_action": "start",
            "completion_status": "success",
            "completion_error": None,
        },
    )
    still_stopped = _read(cpu=6, status="Stopped")
    updates = []
    scheduled = []

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    async def must_not_mutate(*args, **kwargs):
        raise AssertionError("recovery must not submit a second start operation")

    def schedule(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async({"verdict": "running", "error": None}),
    )
    monkeypatch.setattr(
        service, "_read_for_job", lambda *args, **kwargs: _async(still_stopped)
    )
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_bounded", must_not_mutate)
    monkeypatch.setattr(service.asyncio, "create_task", schedule)

    await service._reconcile_unknown(row, object())

    assert updates[-1]["locked_by"] is None
    assert "phase" not in updates[-1]
    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_start_readback_outage_preserves_a_partial_apply_verdict(monkeypatch):
    row = _row(operation_id="apply-op", stopped_by_job=True, phase="verifying")
    stopped = _read(cpu=6, status="Stopped")
    unknown = []

    async def bounded(fn, *args, **kwargs):
        assert fn is service.change_instance_state_sync
        return "start-op"

    reads = iter([stopped, service.SpecificationUnavailable("readback unavailable")])

    async def unreadable(*args, **kwargs):
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value

    async def mark_unknown(*args, **kwargs):
        unknown.append(kwargs)

    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_read_for_job", unreadable)
    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_mark_unknown", mark_unknown)

    await service._complete_restart_after_apply(
        row,
        object(),
        stopped,
        completion_status="partial",
        completion_error="The apply operation expired after changing the target.",
    )

    assert unknown[-1]["operation_id"] == "start-op"
    assert unknown[-1]["result_extra"] == {
        "completion_status": "partial",
        "completion_error": "The apply operation expired after changing the target.",
    }

    recovery_row = _row(
        status="unknown",
        phase="unknown",
        operation_id="start-op",
        stopped_by_job=True,
        result={"ambiguous_action": "start", **unknown[-1]["result_extra"]},
    )
    terminal = []
    monkeypatch.setattr(
        service,
        "_operation_verdict",
        lambda *args, **kwargs: _async({"verdict": "success", "error": None}),
    )
    monkeypatch.setattr(
        service,
        "_read_for_job",
        lambda *args, **kwargs: _async(_read(cpu=6, status="Running")),
    )
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: _capture_async(terminal, args, kwargs),
    )

    await service._reconcile_unknown(recovery_row, object())

    assert terminal[-1][0][1] == "partial"
    assert "expired" in terminal[-1][1]["error"]


@pytest.mark.asyncio
async def test_terminal_explicitly_clears_a_previous_phase_operation(monkeypatch):
    row = _row(operation_id="stop-op", phase="submitting")
    updates = []

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(
        service,
        "drain_instance_operation_audits",
        lambda **kwargs: _async(1),
    )

    await service._terminal(
        row,
        "partial",
        phase="partial",
        operation_id=None,
        result={"specification": _read(cpu=6, status="Running").public},
        error="The current operation could not be correlated.",
    )

    assert updates[-1]["operation_id"] is None
    assert updates[-1]["audit_stage"] == "terminal"


@pytest.mark.asyncio
async def test_terminal_audit_failure_does_not_change_durable_verdict(monkeypatch):
    row = _row(status="running", phase="verifying")
    updates = []

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    monkeypatch.setattr(service, "_set_job", set_job)
    async def audit_offline(**_kwargs):
        raise RuntimeError("audit offline")

    monkeypatch.setattr(service, "drain_instance_operation_audits", audit_offline)

    transitioned = await service._terminal(
        row,
        "success",
        phase="success",
        result={"specification": {"cpu": 6}},
        operation_id="apply-op",
    )

    assert transitioned is True
    assert updates == [
        {
            "worker_id": "worker-1",
            "audit_stage": "terminal",
            "status": "success",
            "phase": "success",
            "result": {"specification": {"cpu": 6}},
            "error": None,
            "operation_id": "apply-op",
            "locked_by": None,
            "locked_at": None,
            "finished_at": updates[0]["finished_at"],
        }
    ]


@pytest.mark.asyncio
async def test_terminal_audit_is_drained_from_the_durable_job(monkeypatch):
    row = _row(status="running", phase="verifying")
    audits = []

    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))

    async def drain(**kwargs):
        audits.append(kwargs)
        return 1

    monkeypatch.setattr(service, "drain_instance_operation_audits", drain)

    transitioned = await service._terminal(
        row,
        "success",
        phase="success",
        result={"specification": {"cpu": 6}},
        operation_id="apply-op",
    )

    assert transitioned is True
    assert audits == [{"job_id": row.job_id}]


@pytest.mark.asyncio
async def test_specification_worker_heartbeats_until_its_lease_is_lost(monkeypatch):
    renewals = []

    async def no_wait(_seconds):
        return None

    async def renew(job_id, **kwargs):
        renewals.append((job_id, kwargs))
        return len(renewals) == 1

    monkeypatch.setattr(service.asyncio, "sleep", no_wait)
    monkeypatch.setattr(service, "_set_job", renew)

    await service._lease_heartbeat("job-1", "worker-1")

    assert len(renewals) == 2
    assert all(item[1]["worker_id"] == "worker-1" for item in renewals)
    assert all(item[1]["locked_at"] is not None for item in renewals)


@pytest.mark.asyncio
async def test_terminal_start_requires_running_readback(monkeypatch):
    row = _row(stopped_by_job=True, phase="starting")
    still_stopped = _read(cpu=6, status="Stopped")
    terminal = []

    async def bounded(fn, *args, **kwargs):
        assert fn is service.change_instance_state_sync
        return "start-op"

    async def finish(*args, **kwargs):
        terminal.append((args, kwargs))

    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(service, "_read_for_job", lambda *args, **kwargs: _async(still_stopped))
    updates = []

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(service, "_terminal", finish)

    await service._complete_restart_after_apply(row, object(), still_stopped)

    assert terminal[-1][0][1] == "partial"
    assert terminal[-1][1]["result"]["instance_status"] == "Stopped"
    assert updates[0]["result"]["ambiguous_action"] == "start"
    assert updates[1]["phase"] == "starting"
    assert updates[1]["operation_id"] == "start-op"


@pytest.mark.asyncio
async def test_terminal_start_requires_matching_root_readback(monkeypatch):
    row = _row(
        requested={"root_disk_bytes": 20 * GIB},
        stopped_by_job=True,
        phase="verifying",
    )
    stopped = _read(cpu=6, status="Stopped")
    stopped.public["root_disk"]["value_bytes"] = 20 * GIB
    running_mismatch = _read(cpu=6, status="Running")
    reads = iter([stopped, running_mismatch])
    terminal = []

    async def bounded(fn, *args, **kwargs):
        assert fn is service.change_instance_state_sync
        return "start-op"

    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(
        service, "_read_for_job", lambda *args, **kwargs: _async(next(reads))
    )
    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: _capture_async(terminal, args, kwargs),
    )

    await service._complete_restart_after_apply(row, object(), stopped)

    assert terminal[-1][0][1] == "partial"
    assert terminal[-1][1]["result"]["instance_status"] == "Running"
    assert "does not match" in terminal[-1][1]["error"]


@pytest.mark.asyncio
async def test_terminal_start_rejects_a_new_pending_root_quota(monkeypatch):
    row = _row(stopped_by_job=True, phase="verifying")
    stopped = _read(cpu=6, status="Stopped")
    running_pending = _pending_quota_read(cpu=6, status="Running")
    reads = iter([stopped, running_pending])
    terminal = []

    async def bounded(fn, *args, **kwargs):
        assert fn is service.change_instance_state_sync
        return "start-op"

    monkeypatch.setattr(service, "_bounded", bounded)
    monkeypatch.setattr(
        service, "_read_for_job", lambda *args, **kwargs: _async(next(reads))
    )
    monkeypatch.setattr(service, "_set_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: _capture_async(terminal, args, kwargs),
    )

    await service._complete_restart_after_apply(row, object(), stopped)

    assert terminal[-1][0][1] == "partial"
    assert terminal[-1][1]["result"]["instance_status"] == "Running"
    assert "deferred root-disk quota" in terminal[-1][1]["error"]


def test_state_wait_timeout_is_ambiguous_not_a_known_failure(monkeypatch):
    response = SimpleNamespace(
        json=lambda: {"operation": "/1.0/operations/start-op"}
    )
    state = SimpleNamespace(put=lambda **kwargs: response)
    instance = SimpleNamespace(state=state)
    client = SimpleNamespace(api=SimpleNamespace(instances={"vm-1": instance}))
    monkeypatch.setattr(
        service,
        "wait_for_lxd_operation",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout("timed out")),
    )

    with pytest.raises(SpecificationWriteUnknown) as excinfo:
        service.change_instance_state_sync(client, "vm-1", "project-a", "start")

    assert excinfo.value.operation_id == "start-op"


def test_put_transport_timeout_is_ambiguous(monkeypatch):
    current = _read(cpu=4, status="Running")
    instance = SimpleNamespace(
        put=lambda **kwargs: (_ for _ in ()).throw(requests.Timeout("submit timed out"))
    )
    client = SimpleNamespace(api=SimpleNamespace(instances={"vm-1": instance}))
    monkeypatch.setattr(service, "read_specification_sync", lambda *args, **kwargs: current)
    monkeypatch.setattr(
        service,
        "read_project_usage",
        lambda *args, **kwargs: ProjectUsage(limits={}, used={}, complete=True),
    )

    with pytest.raises(SpecificationWriteUnknown) as excinfo:
        service.apply_specification_sync(
            client,
            instance_name="vm-1",
            host="cluster-a",
            project="project-a",
            expected_etag='"v1"',
            expected_plan_revision="",
            requested={"cpu_count": 6},
            acknowledgements=[],
        )

    assert excinfo.value.operation_id is None


def test_accepted_put_without_readback_is_ambiguous(monkeypatch):
    current = _read(cpu=4, status="Running")
    response = SimpleNamespace(json=lambda: {"operation": "/1.0/operations/apply-op"})
    instance = SimpleNamespace(put=lambda **kwargs: response)
    client = SimpleNamespace(api=SimpleNamespace(instances={"vm-1": instance}))
    reads = iter([current, requests.ConnectionError("readback unavailable")])

    def read(*args, **kwargs):
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(service, "read_specification_sync", read)
    monkeypatch.setattr(
        service,
        "read_project_usage",
        lambda *args, **kwargs: ProjectUsage(limits={}, used={}, complete=True),
    )
    monkeypatch.setattr(service, "wait_for_lxd_operation", lambda *args, **kwargs: None)

    with pytest.raises(SpecificationWriteUnknown) as excinfo:
        service.apply_specification_sync(
            client,
            instance_name="vm-1",
            host="cluster-a",
            project="project-a",
            expected_etag='"v1"',
            expected_plan_revision="",
            requested={"cpu_count": 6},
            acknowledgements=[],
        )

    assert excinfo.value.operation_id == "apply-op"


@pytest.mark.asyncio
async def test_unknown_job_stays_active_when_cluster_is_offline(monkeypatch):
    row = _row(status="unknown", phase="unknown", result={"ambiguous_action": "apply"})
    updates = []
    scheduled = []

    async def offline(*args, **kwargs):
        raise service.SpecificationUnavailable("cluster offline")

    async def set_job(*args, **kwargs):
        updates.append(kwargs)
        return True

    def schedule(coroutine):
        if coroutine.cr_code.co_name == "_lease_heartbeat":
            coroutine.close()
            return None
        scheduled.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(service, "_claim_job", lambda *args, **kwargs: _async(True))
    monkeypatch.setattr(service, "_load_job", lambda *args, **kwargs: _async(row))
    monkeypatch.setattr(service, "get_client", offline)
    monkeypatch.setattr(service, "_set_job", set_job)
    monkeypatch.setattr(
        service,
        "_terminal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an ambiguous job must not become terminal while offline")
        ),
    )
    monkeypatch.setattr(service.asyncio, "create_task", schedule)

    await service.run_specification_job(row.job_id, recover_unknown=True)

    assert updates[-1]["locked_by"] is None
    assert updates[-1]["locked_at"] is None
    assert "status" not in updates[-1]
    assert len(scheduled) == 1


def test_disk_growth_checks_authoritative_project_aggregate():
    current = _read(cpu=4, status="Stopped")
    usage = ProjectUsage(
        limits={"disk_bytes": 30 * GIB},
        used={"disk_bytes": 25 * GIB},
        complete=True,
        complete_fields={"disk_bytes"},
    )

    plan = service.build_preflight(
        current,
        {"root_disk_bytes": 20 * GIB},
        project_usage=usage,
    )

    assert any(
        blocker["code"] == "PROJECT_QUOTA_EXCEEDED"
        and blocker["field"] == "root_disk_bytes"
        for blocker in plan["blockers"]
    )


async def _async(value):
    return value


async def _capture_async(target, args, kwargs):
    target.append((args, kwargs))
