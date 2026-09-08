"""A single-instance read must report the project the instance is actually in.

An instance name is unique per project, not per host, so the project is the difference between
addressing one workload and addressing another. The list path built `VMInfo(project=...)` from
the project it resolved. The four single-instance paths did not: they wrote the resolved name
into a synthetic `config['project']` key that nothing in this codebase or the frontend ever read,
and left `VMInfo.project` as whatever the bound pylxd client reported, which is the client's
project rather than the instance's.

Two consequences, both live on this fleet:

* The detail dialog overwrites the list row with the detail payload, so opening any instance
  outside `default` and switching to Snapshots asked for it in `default` and rendered
  "Instance not found". Found by opening `acme-a20`, which lives in `central2-rehearsal`.
* `instances.py` checks the tenant ACL against `vm.project`, so an instance in another project
  was authorized as though it were in `default`.
"""

from types import SimpleNamespace

import pytest

from app.services.lxd import instance as instance_module
from app.services.lxd.instance import InstanceManager

REAL_PROJECT = "central2-rehearsal"
NAME = "acme-a20"
HOST = "nuc1"


def _instance_stub():
    # pylxd reports the BOUND client's project here, which is exactly why it cannot be trusted.
    return SimpleNamespace(
        name=NAME,
        status="Stopped",
        status_code=102,
        project="default",
        architecture="x86_64",
        config={"limits.cpu": "2"},
        devices={},
        expanded_devices={},
        profiles=["default"],
        description="",
        created_at="2026-09-01T00:00:00Z",
        last_used_at="2026-09-01T00:00:00Z",
        location="none",
        type="container",
        state=lambda: SimpleNamespace(
            network=None, memory=None, cpu=None, disk=None, processes=0
        ),
    )


@pytest.fixture
def manager(monkeypatch):
    mgr = InstanceManager.__new__(InstanceManager)
    # _instance_to_vm_info reads resources through this; the numbers are not what is under test.
    mgr.resource_manager = SimpleNamespace(get_resource_usage=lambda instance: None)
    monkeypatch.setattr(
        InstanceManager, "_get_client_with_reconnect", lambda self, host: object()
    )
    monkeypatch.setattr(
        InstanceManager, "_get_node_endpoint", lambda self, host: "https://nuc1:8443"
    )
    monkeypatch.setattr(
        instance_module,
        "find_instance_in_projects",
        lambda client, name, project=None: (_instance_stub(), REAL_PROJECT),
    )
    return mgr


@pytest.mark.asyncio
async def test_the_resolved_project_lands_on_the_model(manager):
    vm = await manager.get_vm(NAME, HOST, REAL_PROJECT)

    assert vm is not None
    assert vm.project == REAL_PROJECT, (
        "the caller and the tenant ACL both act on this field, and pylxd's own value is the "
        "bound client's project rather than the instance's"
    )


@pytest.mark.asyncio
async def test_the_project_is_not_smuggled_through_the_config_map(manager):
    """`config` is the instance's LXD configuration, not a place to park resolved metadata."""
    vm = await manager.get_vm(NAME, HOST, REAL_PROJECT)

    assert "project" not in (vm.config or {}), (
        "a synthetic key here shows up in the Configuration tab as if LXD had set it, and it is "
        "where the real project used to be written instead of onto the field"
    )
    assert (vm.config or {}).get("limits.cpu") == "2", "the real config must survive"
