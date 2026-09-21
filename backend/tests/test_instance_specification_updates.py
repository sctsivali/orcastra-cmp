"""Typed instance resource edits preserve state and never guess at concurrency or disk size."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.instance_specifications import (
    SpecificationConflict,
    SpecificationStale,
    SpecificationWriteUnknown,
    apply_specification_sync,
    build_preflight,
    read_specification_sync,
)
from app.services.lxd.project_usage import ProjectUsage

GIB = 1024**3


class _Response:
    def __init__(self, body, *, etag=None):
        self._body = body
        self.headers = {"ETag": etag} if etag else {}

    def json(self):
        return self._body


class _Wait:
    def get(self, params=None, timeout=None):
        return _Response({"metadata": {"status": "Success", "err": ""}})


class _Operation:
    @property
    def wait(self):
        return _Wait()

    def get(self, params=None):
        return _Response({"metadata": {"status": "Success", "err": ""}})


class _Operations:
    def __getitem__(self, operation_id):
        assert operation_id == "op-1"
        return _Operation()


class _State:
    def put(self, **kwargs):
        raise AssertionError("these unit tests must not change instance power state")


class _Instance:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.state = _State()

    def get(self, params=None, timeout=None):
        self.client.reads.append((self.name, dict(params or {})))
        return _Response({"metadata": deepcopy(self.client.record)}, etag=self.client.etag)

    def put(self, **kwargs):
        self.client.writes.append(deepcopy(kwargs))
        payload = kwargs["json"]
        self.client.record.update(
            {
                "architecture": payload.get("architecture"),
                "config": deepcopy(payload["config"]),
                "devices": deepcopy(payload["devices"]),
                "profiles": list(payload["profiles"]),
                "description": payload["description"],
                "ephemeral": payload["ephemeral"],
                "stateful": payload["stateful"],
            }
        )
        self.client.record["expanded_config"] = deepcopy(payload["config"])
        expanded = deepcopy(self.client.record.get("expanded_devices") or {})
        expanded.update(deepcopy(payload["devices"]))
        self.client.record["expanded_devices"] = expanded
        if self.client.defer_quota_on_put:
            key = "volatile.root.apply_quota"
            self.client.record["config"][key] = "true"
            self.client.record["expanded_config"][key] = "true"
        self.client.etag = '"v2"'
        return _Response(
            {
                "type": "async",
                "operation": "/1.0/operations/op-1",
                "metadata": {"id": "op-1"},
            }
        )


class _Instances:
    def __init__(self, client):
        self.client = client

    def __getitem__(self, name):
        return _Instance(self.client, name)


class _StorageVolumeState:
    def __init__(self, client):
        self.client = client

    def get(self, params=None, timeout=None):
        self.client.volume_state_reads.append((dict(params or {}), timeout))
        if self.client.volume_state_error is not None:
            raise self.client.volume_state_error
        return _Response(
            {"metadata": {"usage": {"total": self.client.volume_total}}}
        )


class _StorageVolume:
    def __init__(self, client):
        self.state = _StorageVolumeState(client)


class _VolumeType:
    def __init__(self, client):
        self.client = client

    def __getitem__(self, _name):
        return _StorageVolume(self.client)


class _Volumes:
    def __init__(self, client):
        self.client = client

    def __getitem__(self, _volume_type):
        return _VolumeType(self.client)


class _Pool:
    def __init__(self, client):
        self.volumes = _Volumes(client)


class _StoragePools:
    def __init__(self, client):
        self.client = client

    def __getitem__(self, _pool):
        return _Pool(self.client)


class _Client:
    def __init__(
        self,
        record,
        etag='"v1"',
        *,
        volume_total=None,
        volume_state_error=None,
        defer_quota_on_put=False,
    ):
        self.record = deepcopy(record)
        self.etag = etag
        self.volume_total = volume_total
        self.volume_state_error = volume_state_error
        self.defer_quota_on_put = defer_quota_on_put
        self.volume_state_reads = []
        self.reads = []
        self.writes = []
        self.api = SimpleNamespace(
            instances=_Instances(self),
            operations=_Operations(),
            storage_pools=_StoragePools(self),
        )


def _record(*, instance_type="container", status="Running", own_root=False):
    root = {"type": "disk", "path": "/", "pool": "fast", "size": "10GiB"}
    return {
        "name": "db-1",
        "type": instance_type,
        "status": status,
        "architecture": "x86_64",
        "config": {"user.note": "preserve-me"},
        "expanded_config": {
            "user.note": "preserve-me",
            "limits.cpu": "0-3",
            "limits.memory": "4GiB",
        },
        "devices": (
            {
                "root": root,
                "data": {"type": "disk", "source": "data", "path": "/srv/data"},
            }
            if own_root
            else {"data": {"type": "disk", "source": "data", "path": "/srv/data"}}
        ),
        "expanded_devices": {
            "root": root,
            "data": {"type": "disk", "source": "data", "path": "/srv/data"},
        },
        "profiles": ["default", "database"],
        "description": "database node",
        "ephemeral": False,
        "stateful": False,
    }


def _usage(cpu_limit=32, memory_limit=64 * GIB):
    return ProjectUsage(
        limits={
            "cpu": cpu_limit,
            "memory_bytes": memory_limit,
            "instances": None,
            "containers": None,
            "virtual_machines": None,
            "disk_bytes": None,
        },
        used={
            "cpu": 4,
            "memory_bytes": 4 * GIB,
            "instances": 1,
            "containers": 1,
            "virtual_machines": 0,
        },
        complete=True,
    )


def test_read_uses_the_exact_project_and_reports_inherited_values():
    client = _Client(_record())
    got = read_specification_sync(client, "db-1", "mc-a", "org-a")

    assert client.reads == [("db-1", {"project": "org-a"})]
    assert got.public["cpu"] == {"value": 4, "source": "profile"}
    assert got.public["memory"] == {"value_bytes": 4 * GIB, "source": "profile"}
    assert got.public["root_disk"] == {
        "value_bytes": 10 * GIB,
        "pool": "fast",
        "source": "profile",
        "editable": True,
        "pending_quota_change": False,
        "reason": None,
    }
    assert got.etag == '"v1"'


def test_a_running_vm_requires_an_explicit_restart_and_irreversible_acknowledgements():
    current = read_specification_sync(
        _Client(_record(instance_type="virtual-machine")), "db-1", "mc-a", "org-a"
    )
    plan = build_preflight(
        current,
        {"cpu_count": 2, "memory_bytes": 2 * GIB, "root_disk_bytes": 20 * GIB},
        project_usage=_usage(),
    )

    assert plan["execution"] == "restart_required"
    assert plan["blockers"] == []
    assert plan["required_acknowledgements"] == [
        "memory_decrease",
        "restart_instance",
        "root_disk_growth",
    ]
    assert len(plan["plan_revision"]) == 64


def test_running_vm_cpu_and_memory_are_live_without_a_blanket_restart():
    record = _record(instance_type="virtual-machine")
    record["expanded_config"]["limits.cpu"] = "4"
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"cpu_count": 6, "memory_bytes": 6 * GIB},
        project_usage=_usage(),
    )

    assert plan["execution"] == "live"
    assert "restart_instance" not in plan["required_acknowledgements"]
    assert any(warning["code"] == "VM_GUEST_ACTION_MAY_BE_REQUIRED" for warning in plan["warnings"])


@pytest.mark.parametrize(
    ("instance_type", "status", "expected_execution"),
    [
        ("container", "Running", "live"),
        ("container", "Stopped", "stopped"),
        ("virtual-machine", "Running", "live"),
        ("virtual-machine", "Stopped", "stopped"),
    ],
)
@pytest.mark.parametrize(
    ("cpu_count", "memory_bytes", "memory_acknowledgement"),
    [
        (2, 2 * GIB, True),
        (6, 6 * GIB, False),
    ],
)
def test_cpu_and_memory_can_move_up_or_down_for_running_and_stopped_instances(
    instance_type,
    status,
    expected_execution,
    cpu_count,
    memory_bytes,
    memory_acknowledgement,
):
    record = _record(instance_type=instance_type, status=status)
    record["expanded_config"]["limits.cpu"] = "4"
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")

    plan = build_preflight(
        current,
        {"cpu_count": cpu_count, "memory_bytes": memory_bytes},
        project_usage=_usage(),
    )

    assert plan["blockers"] == []
    assert plan["execution"] == expected_execution
    assert {change["field"] for change in plan["changes"]} == {
        "cpu_count",
        "memory_bytes",
    }
    assert ("memory_decrease" in plan["required_acknowledgements"]) is memory_acknowledgement
    assert "restart_instance" not in plan["required_acknowledgements"]


@pytest.mark.parametrize("instance_type", ["container", "virtual-machine"])
def test_a_stopped_instance_can_grow_its_root_without_a_restart_cycle(instance_type):
    current = read_specification_sync(
        _Client(_record(instance_type=instance_type, status="Stopped")),
        "db-1",
        "mc-a",
        "org-a",
    )

    plan = build_preflight(
        current,
        {"root_disk_bytes": 20 * GIB},
        project_usage=_usage(),
    )

    assert plan["blockers"] == []
    assert plan["execution"] == "stopped"
    assert plan["required_acknowledgements"] == ["root_disk_growth"]


@pytest.mark.parametrize("instance_type", ["container", "virtual-machine"])
def test_a_running_instance_root_growth_requires_a_graceful_restart(instance_type):
    current = read_specification_sync(
        _Client(_record(instance_type=instance_type)), "db-1", "mc-a", "org-a"
    )

    plan = build_preflight(
        current,
        {"root_disk_bytes": 20 * GIB},
        project_usage=_usage(),
    )

    assert plan["blockers"] == []
    assert plan["execution"] == "restart_required"
    assert plan["required_acknowledgements"] == [
        "restart_instance",
        "root_disk_growth",
    ]


@pytest.mark.parametrize("current_cpu,target_cpu", [("0-3", 6), ("sockets=2,cores=2", 6), ("4", 65)])
def test_running_vm_cpu_restart_covers_pinning_topology_and_hotplug_ceiling(
    current_cpu,
    target_cpu,
):
    record = _record(instance_type="virtual-machine")
    record["expanded_config"]["limits.cpu"] = current_cpu
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"cpu_count": target_cpu},
        project_usage=_usage(cpu_limit=128),
    )

    assert plan["execution"] == "restart_required"
    assert "restart_instance" in plan["required_acknowledgements"]


def test_vm_memory_growth_restarts_when_hotplug_is_explicitly_disabled():
    record = _record(instance_type="virtual-machine")
    record["config"]["limits.memory.hotplug"] = "false"
    record["expanded_config"]["limits.memory.hotplug"] = "false"
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"memory_bytes": 6 * GIB},
        project_usage=_usage(),
    )

    assert plan["execution"] == "restart_required"
    assert "restart_instance" in plan["required_acknowledgements"]


def test_vm_memory_growth_restarts_above_an_explicit_hotplug_limit():
    record = _record(instance_type="virtual-machine")
    record["config"]["limits.memory.hotplug"] = "8GiB"
    record["expanded_config"]["limits.memory.hotplug"] = "8GiB"
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"memory_bytes": 10 * GIB},
        project_usage=_usage(memory_limit=16 * GIB),
    )

    assert plan["execution"] == "restart_required"
    assert "restart_instance" in plan["required_acknowledgements"]


def test_root_disk_shrink_is_a_blocker_not_a_write_plan():
    current = read_specification_sync(_Client(_record()), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"root_disk_bytes": 8 * GIB},
        project_usage=_usage(),
    )
    assert [item["code"] for item in plan["blockers"]] == [
        "ROOT_DISK_SHRINK_UNSUPPORTED"
    ]
    assert plan["changes"] == []


@pytest.mark.parametrize(
    ("instance_type", "configured_size"),
    [("container", "2GiB"), ("virtual-machine", "4GiB")],
)
def test_inherited_root_size_is_reported_from_the_expanded_device(
    instance_type,
    configured_size,
):
    record = _record(instance_type=instance_type)
    record["expanded_devices"]["root"]["size"] = configured_size

    current = read_specification_sync(
        _Client(record),
        "db-1",
        "mc-a",
        "org-a",
    )

    assert current.public["root_disk"] == {
        "value_bytes": int(configured_size.removesuffix("GiB")) * GIB,
        "pool": "fast",
        "source": "profile",
        "editable": True,
        "pending_quota_change": False,
        "reason": None,
    }


@pytest.mark.parametrize("pending_value", ["true", "false", "1"])
@pytest.mark.parametrize("root_name", ["root", "system-disk"])
def test_deferred_container_quota_blocks_another_resize(pending_value, root_name):
    record = _record()
    record["expanded_devices"][root_name] = record["expanded_devices"].pop("root")
    record["config"][f"volatile.{root_name}.apply_quota"] = pending_value
    record["expanded_config"][f"volatile.{root_name}.apply_quota"] = pending_value
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"root_disk_bytes": 20 * GIB},
        project_usage=_usage(),
    )

    assert current.public["root_disk"]["value_bytes"] == 10 * GIB
    assert current.public["root_disk"]["pending_quota_change"] is True
    assert current.public["root_disk"]["editable"] is False
    assert [item["code"] for item in plan["blockers"]] == ["ROOT_DISK_QUOTA_PENDING"]


def test_deferred_root_quota_blocks_an_unrelated_restart_required_change():
    record = _record(instance_type="virtual-machine")
    record["config"]["volatile.root.apply_quota"] = "true"
    record["expanded_config"].update(
        {
            "limits.memory.hotplug": "false",
            "volatile.root.apply_quota": "true",
        }
    )
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")

    plan = build_preflight(
        current,
        {"memory_bytes": 8 * GIB},
        project_usage=_usage(),
    )

    assert plan["execution"] == "restart_required"
    assert any(item["code"] == "ROOT_DISK_QUOTA_PENDING" for item in plan["blockers"])


def test_vm_without_an_explicit_size_uses_the_daemon_effective_block_size():
    record = _record(instance_type="virtual-machine")
    record["expanded_devices"]["root"].pop("size")
    client = _Client(record, volume_total=10 * GIB)
    current = read_specification_sync(client, "db-1", "mc-a", "org-a")

    assert current.public["root_disk"]["value_bytes"] == 10 * GIB
    assert current.public["root_disk"]["editable"] is True
    assert client.volume_state_reads == [({"project": "org-a"}, (3, 8))]


@pytest.mark.parametrize("volume_total", [None, 0, -1, True, 1.5, "2GiB"])
def test_ambiguous_vm_state_total_never_enables_resize(volume_total):
    record = _record(instance_type="virtual-machine")
    record["expanded_devices"]["root"].pop("size")
    current = read_specification_sync(
        _Client(record, volume_total=volume_total),
        "db-1",
        "mc-a",
        "org-a",
    )

    assert current.public["root_disk"]["value_bytes"] is None
    assert current.public["root_disk"]["editable"] is False


def test_failed_vm_state_read_does_not_guess_a_default_size():
    record = _record(instance_type="virtual-machine")
    record["expanded_devices"]["root"].pop("size")
    current = read_specification_sync(
        _Client(
            record,
            volume_state_error=RuntimeError("volume state read failed"),
        ),
        "db-1",
        "mc-a",
        "org-a",
    )

    assert current.public["root_disk"]["value_bytes"] is None
    assert current.public["root_disk"]["editable"] is False


def test_container_without_an_explicit_size_is_not_editable_or_state_probed():
    record = _record()
    root = record["expanded_devices"]["root"]
    root.pop("size")
    client = _Client(record, volume_total=20 * GIB)
    current = read_specification_sync(client, "db-1", "mc-a", "org-a")

    assert current.public["root_disk"]["value_bytes"] is None
    assert current.public["root_disk"]["editable"] is False
    assert client.volume_state_reads == []


def test_vm_state_disk_size_is_not_used_as_the_root_allocation():
    record = _record(instance_type="virtual-machine")
    root = record["expanded_devices"]["root"]
    root.pop("size")
    root["size.state"] = "32GiB"

    current = read_specification_sync(
        _Client(record, volume_total=10 * GIB), "db-1", "mc-a", "org-a"
    )

    assert current.public["root_disk"]["value_bytes"] == 10 * GIB


def test_project_quota_is_checked_after_replacing_the_instances_current_allocation():
    current = read_specification_sync(_Client(_record()), "db-1", "mc-a", "org-a")
    plan = build_preflight(
        current,
        {"cpu_count": 9},
        project_usage=_usage(cpu_limit=8),
    )
    assert plan["blockers"][0]["code"] == "PROJECT_QUOTA_EXCEEDED"
    assert plan["blockers"][0]["field"] == "cpu_count"


def test_apply_preserves_unrelated_state_and_materializes_only_the_inherited_root(monkeypatch):
    from app.services import instance_specifications as module

    monkeypatch.setattr(module, "read_project_usage", lambda client, project: _usage())
    client = _Client(_record(status="Stopped"))
    current = read_specification_sync(client, "db-1", "mc-a", "org-a")
    requested = {
        "cpu_count": 6,
        "memory_bytes": 8 * GIB,
        "root_disk_bytes": 20 * GIB,
    }
    plan = build_preflight(current, requested, project_usage=_usage())

    verified, operation_id = apply_specification_sync(
        client,
        instance_name="db-1",
        host="mc-a",
        project="org-a",
        expected_etag='"v1"',
        expected_plan_revision=plan["plan_revision"],
        requested=requested,
        acknowledgements=plan["required_acknowledgements"],
    )

    assert operation_id == "op-1"
    written = client.writes[0]
    assert written["params"] == {"project": "org-a"}
    assert written["headers"] == {"If-Match": '"v1"'}
    assert written["json"]["config"] == {
        "user.note": "preserve-me",
        "limits.cpu": "6",
        "limits.memory": f"{8 * GIB}B",
    }
    assert written["json"]["devices"]["data"]["path"] == "/srv/data"
    assert written["json"]["devices"]["root"] == {
        "type": "disk",
        "path": "/",
        "pool": "fast",
        "size": f"{20 * GIB}B",
    }
    assert written["json"]["profiles"] == ["default", "database"]
    assert written["json"]["description"] == "database node"
    assert verified.public["cpu"]["value"] == 6
    assert verified.public["memory"]["value_bytes"] == 8 * GIB
    assert verified.public["root_disk"]["value_bytes"] == 20 * GIB


def test_apply_never_reports_success_when_the_daemon_defers_the_root_quota(monkeypatch):
    from app.services import instance_specifications as module

    monkeypatch.setattr(module, "read_project_usage", lambda client, project: _usage())
    record = _record(status="Stopped")
    client = _Client(record, defer_quota_on_put=True)
    current = read_specification_sync(client, "db-1", "mc-a", "org-a")
    requested = {"root_disk_bytes": 20 * GIB}
    plan = build_preflight(current, requested, project_usage=_usage())

    with pytest.raises(SpecificationWriteUnknown):
        apply_specification_sync(
            client,
            instance_name="db-1",
            host="mc-a",
            project="org-a",
            expected_etag='"v1"',
            expected_plan_revision=plan["plan_revision"],
            requested=requested,
            acknowledgements=plan["required_acknowledgements"],
        )

    assert len(client.writes) == 1


def test_direct_root_apply_requires_a_fresh_stopped_readback(monkeypatch):
    from app.services import instance_specifications as module

    monkeypatch.setattr(module, "read_project_usage", lambda client, project: _usage())
    client = _Client(_record(status="Running"))

    with pytest.raises(SpecificationConflict, match="fresh Stopped readback"):
        apply_specification_sync(
            client,
            instance_name="db-1",
            host="mc-a",
            project="org-a",
            expected_etag='"v1"',
            expected_plan_revision="",
            requested={"root_disk_bytes": 20 * GIB},
            acknowledgements=["restart_instance", "root_disk_growth"],
        )

    assert client.writes == []


def test_quota_marker_appearing_after_preflight_refuses_the_write(monkeypatch):
    from app.services import instance_specifications as module

    monkeypatch.setattr(module, "read_project_usage", lambda client, project: _usage())
    client = _Client(_record(status="Stopped"))
    before = read_specification_sync(client, "db-1", "mc-a", "org-a")
    requested = {"root_disk_bytes": 20 * GIB}
    plan = build_preflight(before, requested, project_usage=_usage())
    client.record["config"]["volatile.root.apply_quota"] = "true"
    client.record["expanded_config"]["volatile.root.apply_quota"] = "true"

    with pytest.raises(SpecificationConflict, match="plan is stale"):
        apply_specification_sync(
            client,
            instance_name="db-1",
            host="mc-a",
            project="org-a",
            expected_etag='"v1"',
            expected_plan_revision=plan["plan_revision"],
            requested=requested,
            acknowledgements=plan["required_acknowledgements"],
        )

    assert client.writes == []


def test_stale_etag_is_refused_before_any_write(monkeypatch):
    from app.services import instance_specifications as module

    monkeypatch.setattr(module, "read_project_usage", lambda client, project: _usage())
    client = _Client(_record(), etag='"newer"')
    with pytest.raises(SpecificationStale):
        apply_specification_sync(
            client,
            instance_name="db-1",
            host="mc-a",
            project="org-a",
            expected_etag='"older"',
            expected_plan_revision="ignored",
            requested={"cpu_count": 6},
            acknowledgements=[],
        )
    assert client.writes == []


def test_an_unreadable_root_allocation_disables_resize_instead_of_guessing():
    record = _record()
    record["expanded_devices"]["root"]["size"] = "not-a-size"
    current = read_specification_sync(_Client(record), "db-1", "mc-a", "org-a")
    assert current.public["root_disk"]["editable"] is False
    plan = build_preflight(
        current,
        {"root_disk_bytes": 20 * GIB},
        project_usage=_usage(),
    )
    assert plan["blockers"][0]["code"] == "ROOT_SIZE_UNAVAILABLE"
