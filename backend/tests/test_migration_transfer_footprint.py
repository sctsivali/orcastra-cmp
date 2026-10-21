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
