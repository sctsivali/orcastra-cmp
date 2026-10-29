"""A source's transfer footprint is never guessed low, and an unmeasurable one says why.

LXD reports a root disk usage of 0 for an instance that was created and never started: on zfs the
volume is a clone of its image with no bytes of its own, yet a transfer still writes the whole
root. Found live on the migration lab with a stopped VM that had never booted, which preflight
refused with "restore authoritative size reporting", a remedy for a different problem.

A reported 0 is now kept as a measurement by `instance_footprint`, but a transfer sized from it
alone could fill a target pool, so without an explicit root size the source stays unmeasured and
the blocker names the instance and the fix (start it once, or give the root an explicit size).
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from app.services.lxd import mobility
from app.services.migrations import preflight

GiB = 1024**3


class _Response:
    def __init__(self, metadata: Any) -> None:
        self.status_code = 200
        self._metadata = metadata

    def json(self) -> dict:
        return {"metadata": self._metadata}


class _Node:
    def __init__(self, metadata: Any) -> None:
        self.metadata = metadata

    def get(self, **_kwargs: Any) -> _Response:
        return _Response(self.metadata)


class _Instance(_Node):
    def __init__(self, record: dict, state: dict, snapshots: list) -> None:
        super().__init__(record)
        self.state = _Node(state)
        self.snapshots = _Node(snapshots)


class _Client:
    def __init__(self, *, kind: str, usage: Optional[int], root: dict, pool_config: Optional[dict] = None,
                 snapshots: Optional[list] = None) -> None:
        disk = {} if usage is None else {"root": {"usage": usage, "total": 0}}
        record = {
            "name": "workload",
            "type": kind,
            "config": {},
            "devices": {},
            "expanded_config": {},
            "expanded_devices": {"root": {"type": "disk", "path": "/", **root}},
        }
        self.api = type("Api", (), {})()
        self.api.instances = {"workload": _Instance(record, {"disk": disk}, snapshots or [])}
        self.api.storage_pools = {"default": _Node({"name": "default", "config": pool_config or {}})}


def _bytes(client: _Client) -> Optional[int]:
    return preflight._instance_transfer_bytes(client, "qa", "workload")


def test_a_reported_zero_is_kept_as_a_measurement() -> None:
    client = _Client(kind="container", usage=0, root={"pool": "default"})
    assert mobility.instance_footprint(client, "workload", "qa") == 0


def test_a_disk_without_any_usage_is_unmeasured() -> None:
    client = _Client(kind="container", usage=None, root={"pool": "default"})
    assert mobility.instance_footprint(client, "workload", "qa") is None
    assert _bytes(client) is None


def test_an_instance_that_never_ran_is_not_sized_at_zero() -> None:
    for kind in ("container", "virtual-machine"):
        assert _bytes(_Client(kind=kind, usage=0, root={"pool": "default"})) is None, kind


def test_an_explicit_root_size_sizes_an_instance_that_never_ran() -> None:
    client = _Client(kind="virtual-machine", usage=0, root={"pool": "default", "size": "4GiB"})
    assert _bytes(client) == 4 * GiB


def test_usage_beyond_the_size_and_every_snapshot_are_counted() -> None:
    client = _Client(kind="virtual-machine", usage=12 * GiB, root={"pool": "default", "size": "10GiB"},
                     snapshots=[{"name": "s0", "size": 1 * GiB}])
    assert _bytes(client) == 13 * GiB


def test_the_blocker_names_the_unstarted_instance_and_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(kind="container", usage=0, root={"pool": "default"})
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["default"])
    monkeypatch.setattr(preflight.mobility, "pool_headroom", lambda *_a: {"total": 100 * GiB, "used": 0, "free": 100 * GiB})
    result = preflight.PreflightResult(strategy="same_cluster_copy", downtime="cold")

    preflight._check_capacity(result, source_client=client, target_client=client, source_project="qa",
                              instance_names=["workload"], target_pool="default")

    blocker = next(b for b in result.blockers if b["code"] == "source_footprint_unverified")
    assert "workload" in blocker["message"] and "never started" in blocker["message"]
    assert "Start and stop the instance once" in (blocker.get("remediation") or "")


# A dir pool reports a usage of 0 for every instance, data or not: without project quotas LXD has
# no per-volume figure for it. Found live on the rebuilt lab (2026-10-29) with a stopped container
# holding an 8 MiB payload, and Pandora's pool is dir, so every instance there was refused with
# "never started". The owner chose to bound such an instance by everything its pool holds.


def _dir_client(*, usage: int = 0, location: str = "none", names=("workload",)) -> _Client:
    client = _Client(kind="container", usage=usage, root={"pool": "default"})
    record = client.api.instances["workload"].metadata
    record["location"] = location
    for name in names:
        client.api.instances[name] = client.api.instances["workload"]
    client.api.storage_pools = {"default": _Node({"name": "default", "driver": "dir", "config": {}})}
    return client


def _capacity(monkeypatch, client, *, source_used: int, target_free: int, names=("workload",)):
    target = object()

    def headroom(which, _pool):
        if which is target:
            return {"total": target_free, "used": 0, "free": target_free}
        return {"total": source_used * 2, "used": source_used, "free": source_used}

    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight.mobility, "pool_headroom", headroom)
    result = preflight.PreflightResult(strategy="cross_cluster_copy", downtime="cold")
    preflight._check_capacity(result, source_client=client, target_client=target, source_project="qa",
                              instance_names=list(names), target_pool="fast")
    return result


def test_a_dir_instance_is_bounded_by_its_pool_and_can_move(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _capacity(monkeypatch, _dir_client(), source_used=5 * GiB, target_free=50 * GiB)
    assert result.blockers == []
    warning = next(w for w in result.warnings if w["code"] == "source_footprint_bounded_by_pool")
    assert "workload" in warning["message"] and "dir" in warning["message"]
    assert "5.0 GiB" in warning["message"]


def test_a_dir_instance_is_refused_when_the_target_cannot_hold_its_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _capacity(monkeypatch, _dir_client(), source_used=60 * GiB, target_free=50 * GiB)
    blocker = next(b for b in result.blockers if b["code"] == "target_pool_insufficient_capacity")
    assert "60.0 GiB" in blocker["message"] and "50.0 GiB" in blocker["message"]
    assert not any(b["code"] == "source_footprint_unverified" for b in result.blockers)


def test_one_dir_pool_is_counted_once_for_several_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ("web", "db", "cache")
    result = _capacity(
        monkeypatch, _dir_client(names=names), source_used=20 * GiB, target_free=30 * GiB, names=names
    )
    assert result.blockers == [], "three instances on one 20 GiB pool cannot need 60 GiB"


def test_a_clustered_dir_instance_is_bounded_by_its_member_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _dir_client(location="mig-a2")
    seen = []

    def member_headroom(_client, pool, member):
        seen.append((pool, member))
        return {"total": 40 * GiB, "used": 4 * GiB, "free": 36 * GiB}

    monkeypatch.setattr(preflight, "_member_pool_headroom", member_headroom)
    result = _capacity(monkeypatch, client, source_used=999 * GiB, target_free=10 * GiB)
    assert seen == [("default", "mig-a2")]
    assert result.blockers == []


def test_a_never_started_instance_on_zfs_is_still_refused_with_its_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(kind="container", usage=0, root={"pool": "default"})
    client.api.storage_pools = {"default": _Node({"name": "default", "driver": "zfs", "config": {}})}
    result = _capacity(monkeypatch, client, source_used=1 * GiB, target_free=50 * GiB)
    blocker = next(b for b in result.blockers if b["code"] == "source_footprint_unverified")
    assert "never started" in blocker["message"]
