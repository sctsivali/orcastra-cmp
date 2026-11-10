"""A create that LXD accepts and then fails must not stay recorded as a success.

The POST returns as soon as LXD queues the operation, so only synchronous validation reaches
the caller; a failure while the operation runs (ex; an image fingerprint pruned from the host)
leaves no instance behind. The router records `accepted`, and the terminal verdict comes from one
shared judge on both branches: `_watch_create_operation` without autostart and
`_finalize_and_start` with it, which is the create form's default. A create LXD rejects is
recorded as a create failure with its operation_id, never as an `instance.autostart` failure.

The assertions are behavioural so that moving the wait into another helper does not break them.
"""

import inspect
from types import SimpleNamespace

import pytest

from app.services.lxd.instance import InstanceManager


class _Operations:
    def __init__(self, error: Exception | None = None):
        self._error = error
        self.waited = []

    def wait_for_operation(self, operation_id):
        self.waited.append(operation_id)
        if self._error:
            raise self._error


class _Instance:
    # The start path rebinds the instance onto a client whose read timeout outlives the start,
    # which reads `.name`, and syncs afterwards, so the double needs both.
    def __init__(self, start_error: Exception | None = None, name: str = "web-01"):
        self._start_error = start_error
        self.started = False
        self.synced = False
        self.name = name

    def start(self, wait=False):
        if self._start_error:
            raise self._start_error
        self.started = True

    def sync(self):
        self.synced = True


class _Instances:
    def __init__(self, instance):
        self._instance = instance

    def get(self, name):
        if self._instance is None:
            raise RuntimeError(f"not found: {name}")
        return self._instance


class _Client:
    def __init__(self, op_error=None, instance=None):
        self.operations = _Operations(op_error)
        self.instances = _Instances(instance)


@pytest.fixture
def audit(monkeypatch):
    records = []

    async def _ignore_extension_event(*_args, **_kwargs):
        return 0

    async def _run_inline(fn, *args, **kwargs):
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

    class _Logger:
        def log_activity(self, **kwargs):
            records.append(kwargs)

    monkeypatch.setattr(
        "app.services.activity_logger.get_activity_logger", lambda: _Logger()
    )
    # Only the create verdict is under test. Both notification systems open their own database
    # sessions, so they are stubbed out.
    monkeypatch.setattr(
        "app.services.webhooks.emitter.emit_resource_event",
        _ignore_extension_event,
    )
    monkeypatch.setattr(
        "app.services.lxd.instance._announce_instance_created",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.lxd.instance._emit_instance_created",
        _ignore_extension_event,
    )
    monkeypatch.setattr("app.services.lxd.instance.run_blocking", _run_inline)
    return records


def _manager() -> InstanceManager:
    return InstanceManager.__new__(InstanceManager)


def _creates(records):
    return [r for r in records if r["action"] == "instance.create"]


def _autostarts(records):
    return [r for r in records if r["action"] == "instance.autostart"]


# --- the no-autostart branch -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rejected_create_without_autostart_is_recorded_as_a_failure(audit):
    client = _Client(op_error=RuntimeError("Failed creating instance record"))

    await _manager()._watch_create_operation(client, "web-01", "op-1", "nuc2", "default")

    creates = _creates(audit)
    assert len(creates) == 1
    assert creates[0]["result"] == "failure"
    assert creates[0]["error_code"] == "LXD_OPERATION_FAILED"
    assert creates[0]["details"]["operation_id"] == "op-1"


@pytest.mark.asyncio
async def test_a_good_create_without_autostart_is_recorded_as_a_success(audit):
    client = _Client()

    await _manager()._watch_create_operation(client, "web-01", "op-1", "nuc2", "default")

    creates = _creates(audit)
    assert len(creates) == 1
    assert creates[0]["result"] == "success", (
        "the router only records 'accepted', so something has to write the terminal verdict"
    )


# --- the autostart branch, the create form's default -----------------------------------------

@pytest.mark.asyncio
async def test_a_rejected_create_WITH_autostart_is_recorded_as_a_create_failure(audit):
    """A create LXD rejects is a create failure on this branch, never an instance.autostart one."""
    client = _Client(op_error=RuntimeError("Failed creating instance record"))

    await _manager()._finalize_and_start(client, "web-01", "op-1", "nuc2", "default")

    creates = _creates(audit)
    assert len(creates) == 1, "the create verdict must be recorded on this branch too"
    assert creates[0]["result"] == "failure"
    assert creates[0]["details"]["operation_id"] == "op-1", (
        "the operation_id was dropped on this path, which is what made the record unusable"
    )
    assert _autostarts(audit) == [], (
        "an instance that was never created cannot have failed to start; filing it that way "
        "sends the operator looking at a start problem for a thing that does not exist"
    )


@pytest.mark.asyncio
async def test_a_failed_start_on_a_created_instance_is_still_an_autostart_failure(audit):
    """The distinction must survive: the instance exists, it just did not start."""
    client = _Client(instance=_Instance(start_error=RuntimeError("boot failed")))

    await _manager()._finalize_and_start(client, "web-01", "op-1", "nuc2", "default")

    creates = _creates(audit)
    assert len(creates) == 1 and creates[0]["result"] == "success"
    autostarts = _autostarts(audit)
    assert len(autostarts) == 1 and autostarts[0]["result"] == "failure"
    assert autostarts[0]["details"]["operation_id"] == "op-1"


@pytest.mark.asyncio
async def test_a_rejected_create_does_not_try_to_start_anything(audit):
    """Phase ordering: no point calling instances.get for something that was never made."""
    client = _Client(op_error=RuntimeError("rejected"), instance=_Instance())

    await _manager()._finalize_and_start(client, "web-01", "op-1", "nuc2", "default")

    assert client.instances._instance.started is False


@pytest.mark.asyncio
async def test_the_happy_path_starts_the_instance(audit):
    instance = _Instance()
    client = _Client(instance=instance)

    await _manager()._finalize_and_start(client, "web-01", "op-1", "nuc2", "default")

    assert instance.started is True


# --- both branches share one judge, so they cannot drift apart -------------------------------

@pytest.mark.asyncio
async def test_both_branches_go_through_the_same_judge(monkeypatch):
    """The structural property that keeps the two verdicts identical."""
    seen = []

    async def _judge(self, client, name, operation_id, host, project):
        seen.append(name)
        return False

    monkeypatch.setattr(InstanceManager, "_judge_create_operation", _judge)
    client = _Client()

    await _manager()._watch_create_operation(client, "no-autostart", "op-1", "nuc2", "default")
    await _manager()._finalize_and_start(client, "autostart", "op-2", "nuc2", "default")

    assert seen == ["no-autostart", "autostart"]


@pytest.mark.asyncio
async def test_no_operation_id_is_not_treated_as_a_failure(audit):
    """A synchronous create has already answered, so no failure record is written."""
    client = _Client()

    await _manager()._watch_create_operation(client, "web-01", None, "nuc2", "default")

    assert _creates(audit) == []


def test_the_router_records_acceptance_not_success() -> None:
    """One create must not produce two contradictory audit verdicts."""
    from app.routers import instances as instances_router
    from app.services.instance_operation_audit import _audit_values

    src = inspect.getsource(instances_router.create_instance)
    accepted = _audit_values(
        SimpleNamespace(stage="accepted", event_id="event-1"),
        SimpleNamespace(
            job_id="job-1",
            kind="create",
            cluster_id="nuc2",
            project="default",
            instance_name="web-01",
            instance_type="container",
            requester="user-1",
            target_organization_id=None,
            target_organization_slug=None,
            target_organization_resolution="unbound",
            requested={"profiles": ["default"], "start_on_create": True},
            apply_mode=None,
        ),
    )
    assert accepted["result"] == "accepted", (
        "recording success before LXD has validated anything produced a success record and a "
        "failure record for the same create, with nothing to say which was final"
    )
    assert "drain_instance_operation_audits" in src
    assert 'result="success"' not in src


def test_the_watcher_task_is_strongly_referenced() -> None:
    """A bare create_task can be collected mid-run, which would make the watch unreliable."""
    src = inspect.getsource(InstanceManager.create_instance)
    watch_block = src.split("watch = asyncio.create_task(", 1)[1].split(
        "return operation_id", 1
    )[0]
    assert "_watch_create_operation(" in watch_block
    assert "_bg_tasks.add" in watch_block, (
        "hold a strong reference like the auto-start branch does, or the loop may collect the "
        "task before the operation finishes"
    )
