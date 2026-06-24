"""Unit tests for live instance resource updates (app.services.lxd.instance).

Covers limit-string validation, the read-modify-write of limits.cpu/limits.memory,
the live vs restart-required classification, and the running-constraint fallback.
Runnable with pytest or directly: ``python3 tests/test_instance_resource_update.py``.
"""

from pylxd.exceptions import LXDAPIException

import app.services.lxd.instance as inst_mod
from app.services.lxd.instance import InstanceManager, validate_resource_limits
from app.services.lxd.exceptions import LXDOperationError


def _expect(exc_type, fn, *args):
    """Assert fn(*args) raises exc_type (works with or without pytest)."""
    try:
        fn(*args)
    except exc_type:
        return
    raise AssertionError(f"{getattr(fn, '__name__', fn)} did not raise {exc_type.__name__}")


class _FakeResponse:
    """Minimal response so LXDAPIException.__str__ yields our message."""

    def __init__(self, err: str):
        self.status_code = 200
        self._err = err

    def json(self):
        return {"metadata": {"err": self._err}}


class _FakeInstance:
    def __init__(self, instance_type: str, config: dict, status: str = "Running"):
        self.name = "c1"
        self.type = instance_type
        self.status = status
        self.status_code = 103
        self.architecture = "x86_64"
        self.created_at = None
        self.last_used_at = None
        self.description = ""
        self.config = dict(config)
        self.profiles = ["default"]
        self.updated_with = None
        self.synced = False
        self._raise = None

    def update(self, config, devices=None, wait=True):
        self.updated_with = dict(config)
        if self._raise is not None:
            raise self._raise
        self.config = dict(config)

    def sync(self):
        self.synced = True


class _FakeResourceManager:
    def get_resource_usage(self, instance):
        return None


def _make_manager(fake_instance, found_project="default"):
    mgr = InstanceManager(
        connection_manager=object(),
        resource_manager=_FakeResourceManager(),
        cert_manager=None,
    )
    mgr._get_client_with_reconnect = lambda host: object()
    mgr._get_node_endpoint = lambda host: "https://node1:8443"
    inst_mod.find_instance_in_projects = lambda client, name, project=None: (
        (fake_instance, found_project) if fake_instance is not None else (None, None)
    )
    inst_mod.extract_ip_addresses = lambda instance: []
    return mgr


# --- validation ---------------------------------------------------------------

def test_validate_accepts_valid_values():
    validate_resource_limits("4", "2GiB")
    validate_resource_limits("0-3", "512MiB")
    validate_resource_limits("1,3", "1024MB")
    validate_resource_limits(None, None)   # untouched
    validate_resource_limits("0", "0")     # clear


def test_validate_rejects_bad_cpu():
    _expect(LXDOperationError, validate_resource_limits, "four", None)


def test_validate_rejects_bad_memory():
    _expect(LXDOperationError, validate_resource_limits, None, "2 gigs")


# --- read-modify-write --------------------------------------------------------

def test_container_running_applies_live():
    inst = _FakeInstance("container", {"limits.cpu": "1"})
    mgr = _make_manager(inst)
    out = mgr._update_instance_resources_blocking("c1", "node1", "4", "2GiB", None)
    assert inst.updated_with["limits.cpu"] == "4"
    assert inst.updated_with["limits.memory"] == "2GiB"
    assert out["applied_live"] is True
    assert out["restart_required"] is False
    assert inst.synced is True


def test_vm_running_reports_restart_required():
    inst = _FakeInstance("virtual-machine", {})
    mgr = _make_manager(inst)
    out = mgr._update_instance_resources_blocking("c1", "node1", None, "4GiB", None)
    assert inst.updated_with["limits.memory"] == "4GiB"
    assert out["applied_live"] is False
    assert out["restart_required"] is True


def test_stopped_instance_applies_on_next_start():
    inst = _FakeInstance("container", {}, status="Stopped")
    mgr = _make_manager(inst)
    out = mgr._update_instance_resources_blocking("c1", "node1", "2", None, None)
    assert out["restart_required"] is False
    assert out["applied_live"] is False


def test_clear_removes_override():
    inst = _FakeInstance("container", {"limits.cpu": "4", "limits.memory": "2GiB"})
    mgr = _make_manager(inst)
    mgr._update_instance_resources_blocking("c1", "node1", "0", None, None)
    assert "limits.cpu" not in inst.updated_with
    assert inst.updated_with["limits.memory"] == "2GiB"  # untouched


def test_no_change_is_noop():
    inst = _FakeInstance("container", {"limits.cpu": "4"})
    mgr = _make_manager(inst)
    out = mgr._update_instance_resources_blocking("c1", "node1", "4", None, None)
    assert inst.updated_with is None  # update() never called
    assert "No changes" in out["message"]


def test_running_constraint_maps_to_restart_required():
    inst = _FakeInstance("virtual-machine", {})
    inst._raise = LXDAPIException(_FakeResponse("Cannot update limits.memory while the instance is running"))
    mgr = _make_manager(inst)
    out = mgr._update_instance_resources_blocking("c1", "node1", None, "8GiB", None)
    assert out["restart_required"] is True
    assert out["applied_live"] is False


def test_not_found_raises():
    mgr = _make_manager(None)
    _expect(
        LXDOperationError,
        mgr._update_instance_resources_blocking,
        "missing", "node1", "2", None, None,
    )


if __name__ == "__main__":
    test_validate_accepts_valid_values()
    test_validate_rejects_bad_cpu()
    test_validate_rejects_bad_memory()
    test_container_running_applies_live()
    test_vm_running_reports_restart_required()
    test_stopped_instance_applies_on_next_start()
    test_clear_removes_override()
    test_no_change_is_noop()
    test_running_constraint_maps_to_restart_required()
    test_not_found_raises()
    print("instance_resource_update tests passed")
