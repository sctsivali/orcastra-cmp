"""The value a read resolves must be the value everything downstream uses.

* `InstanceWrapper` carries `location`, so a single-instance read reports the cluster member as
  the list payload, which reads the LXD response directly, does.
* `delete_vm` returns the project it deleted from, and the router stamps the tags, the webhook,
  the upstream event and the audit row with it. Stamping the requested project instead would
  write "default" for a caller that named none, a project the deletion never touched.
* A rebuild job is driven by an unreferenced task, so a restart can lose the driver while the row
  stays mid-flight; a recovery sweep claims only rows no worker can still be driving.
* The events stream drops, for a tenant, any frame it cannot attribute to an instance, so a
  tenant on a shared cluster does not receive its neighbours' storage, image and project
  operations.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def test_the_instance_wrapper_carries_the_cluster_member():
    """Every single-instance read builds its VMInfo from this, with a getattr default of None."""
    from app.services.lxd.utils import InstanceWrapper

    wrapped = InstanceWrapper(
        {"name": "ct-a", "status": "Running", "location": "nuc1-b"}, object(), "default"
    )
    assert getattr(wrapped, "location", None) == "nuc1-b", (
        "the detail read reports the cluster member as None while the list read has it right"
    )


def test_a_wrapper_without_a_location_is_none_rather_than_missing():
    """A standalone host reports no location, which must read as None instead of raising."""
    from app.services.lxd.utils import InstanceWrapper

    wrapped = InstanceWrapper({"name": "ct-a", "status": "Running"}, object(), "default")
    assert wrapped.location is None


@pytest.mark.asyncio
async def test_delete_returns_the_project_it_actually_deleted_from(monkeypatch):
    """The router stamps four side effects with this; a bare True would put them on "default"."""
    from app.services.lxd import instance as instance_module
    from app.services.lxd.instance import InstanceManager

    target = SimpleNamespace(name="web-01", status="Stopped", stop=lambda **k: None, delete=lambda **k: None)
    monkeypatch.setattr(
        instance_module, "find_instance_in_projects",
        lambda client, name, project=None: (target, "center2-rehearsal"),
    )
    mgr = InstanceManager.__new__(InstanceManager)
    monkeypatch.setattr(InstanceManager, "_get_client_with_reconnect", lambda self, host: object())
    monkeypatch.setattr(InstanceManager, "_rebind_for_wait", lambda self, i, h, p, t: i)

    resolved = await mgr.delete_vm("web-01", "nuc1")
    assert resolved == "center2-rehearsal", (
        "the caller stamps tags, a webhook, an upstream event and the audit row with this"
    )


@pytest.mark.asyncio
async def test_the_delete_wait_runs_off_the_event_loop_and_outlasts_the_pooled_read(monkeypatch):
    """The pooled client reads for 8 seconds; tearing down a root disk takes longer.

    Waiting on the pooled client would turn its ReadTimeout into an LXDOperationError and answer
    500 for a delete LXD completed. Stop and restart follow the same rule.
    """
    from app.services.lxd import instance as instance_module
    from app.services.lxd.instance import InstanceManager, _DELETE_WAIT_SECONDS

    assert _DELETE_WAIT_SECONDS > 8, "the wait must outlast the pooled client's read"

    rebinds = []
    target = SimpleNamespace(name="web-01", status="Stopped", stop=lambda **k: None, delete=lambda **k: None)
    monkeypatch.setattr(
        instance_module, "find_instance_in_projects",
        lambda client, name, project=None: (target, "default"),
    )
    mgr = InstanceManager.__new__(InstanceManager)
    monkeypatch.setattr(InstanceManager, "_get_client_with_reconnect", lambda self, host: object())
    monkeypatch.setattr(
        InstanceManager, "_rebind_for_wait",
        lambda self, i, h, p, t: (rebinds.append(t), i)[1],
    )

    await mgr.delete_vm("web-01", "nuc1")
    assert rebinds, "the delete never asked for a client that outlasts the pooled read"
    assert rebinds[0] > 8


def test_the_rebuild_recovery_sweep_only_claims_what_no_worker_can_be_driving():
    """LXD's own rebuild wait runs to 1800s, longer than the stranded window, so a live rebuild must
    keep its row fresh: the worker driving it refreshes it every HEARTBEAT, and only a row silent for
    several beats can have no worker behind it (test_a_rebuild_row_tells_the_truth_about_its_driver)."""
    from datetime import timedelta

    from app.routers.instance_rebuild import _HEARTBEAT_SECONDS, _IN_FLIGHT_STATUSES
    from app.services.rebuild_admission import HEARTBEAT, REBUILD_OP_WINDOW, STRANDED_AFTER, silent_window

    assert _HEARTBEAT_SECONDS == HEARTBEAT.total_seconds()
    assert STRANDED_AFTER >= HEARTBEAT * 5, (
        "a window of fewer than five missed beats would fail jobs that are still running"
    )
    # A worker that died in `rebuilding` may leave a daemon rebuild LXD accepted and still runs.
    assert silent_window("rebuilding") == REBUILD_OP_WINDOW > timedelta(seconds=1800), (
        "a silent rebuilding row must outlast LXD's own rebuild wait"
    )
    assert "ready" not in _IN_FLIGHT_STATUSES and "failed" not in _IN_FLIGHT_STATUSES, (
        "a settled job must never be reopened by the sweep"
    )
    for expected in ("pending", "rebuilding", "provisioning"):
        assert expected in _IN_FLIGHT_STATUSES


def test_the_events_stream_drops_a_frame_it_cannot_attribute_for_a_tenant(monkeypatch):
    """A frame naming no instance must not reach a tenant who shares the cluster.

    Asserted against the real source rather than a copy of the predicates, so it moves with them.
    """
    import inspect

    from app.routers import events

    source = inspect.getsource(events)
    assert "tenant_acl_active()" in source, (
        "the stream must know whether the subscriber carries an ACL at all"
    )
    # The decision lives in frame_is_visible, which the sender consults before every send. The
    # unattributable branch is its last line: a tenant gets nothing the stream cannot attribute.
    send = source.index("await websocket.send_json(payload)")
    consult = source.index("if not frame_is_visible(host, payload):")
    guard = source.index("return not tenant_acl_active()")
    assert guard < consult < send, "the guard must sit before the send, or it guards nothing"
