"""A stop whose operation errors but leaves the instance Stopped is a stop, not a 500.

Found on the Incus lab (Incus 7.6, Debian 13 cloud VM): a graceful stop sent while the guest is
still in early boot fails its operation with "The instance is already running", and the instance
reaches Stopped anyway. Reproduced with the CLI on the host, so it is the daemon's answer, not
the dashboard's:

    incus launch images:debian/13/cloud qa-incus-repro --vm; sleep 20
    incus stop qa-incus-repro --timeout 120   -> Error: The instance is already running (20s)
    incus list qa-incus-repro                 -> STOPPED

The dashboard reported 500 "Could not stop VM" for a stop that had happened. The instance is
re-read after a failed stop operation: Stopped means done (the daemon's message is logged), and
anything else is still the error it was.
"""

from __future__ import annotations

import pytest
from pylxd import exceptions as lxd_exceptions

from app.services.lxd import instance as instance_module
from app.services.lxd.exceptions import LXDOperationError
from app.services.lxd.instance import InstanceManager


class _Response:
    status_code = 400
    content = b'{"error": "The instance is already running"}'

    def json(self):
        return {"error": "The instance is already running"}


class _Vm:
    def __init__(self, ends_as: str):
        self.name = "qa-vm"
        self.status = "Running"
        self._ends_as = ends_as

    def stop(self, force=False, wait=False, timeout=None):
        self.status = self._ends_as
        raise lxd_exceptions.LXDAPIException(_Response())

    def sync(self):
        pass


def _manager(vm, monkeypatch) -> InstanceManager:
    mgr = InstanceManager.__new__(InstanceManager)
    monkeypatch.setattr(mgr, "_get_client_with_reconnect", lambda host: object(), raising=False)
    monkeypatch.setattr(mgr, "_get_node_endpoint", lambda host: "https://10.1.1.242:8443", raising=False)
    monkeypatch.setattr(mgr, "_rebind_for_wait", lambda instance, host, project, timeout: instance, raising=False)
    monkeypatch.setattr(mgr, "_instance_to_vm_info", lambda instance, host, endpoint: type("V", (), {"status": instance.status, "project": None})(), raising=False)
    monkeypatch.setattr(instance_module, "find_instance_in_projects", lambda client, name, project: (vm, "default"))
    return mgr


async def test_a_failed_stop_operation_that_left_the_instance_stopped_is_a_stop(monkeypatch, caplog):
    vm = _Vm(ends_as="Stopped")
    info = await _manager(vm, monkeypatch).stop_vm("qa-vm", "incus-lab", timeout=120)
    assert info.status == "Stopped" and info.project == "default"
    assert "already running" in caplog.text


async def test_a_failed_stop_that_left_the_instance_running_is_still_an_error(monkeypatch):
    vm = _Vm(ends_as="Running")
    with pytest.raises(LXDOperationError, match="Could not stop VM"):
        await _manager(vm, monkeypatch).stop_vm("qa-vm", "incus-lab", timeout=120)
