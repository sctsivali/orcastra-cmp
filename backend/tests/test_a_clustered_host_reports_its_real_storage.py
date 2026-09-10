"""The clustered aggregate must report storage, and must not report it twice.

Two defects in one line. `node_resources.py` read `member_resources["storage"]["pools"]`, a key
/1.0/resources does not send, so every clustered host reported zero and no test looked: the GPU
aggregation fake stubs storage-pools out entirely, and the per-member fan-out test never crosses
members, so nothing in the suite could see either the zero or the double count waiting behind it.

The real block is `storage.disks`, physical media rather than pool space, so correcting the key
would have produced a different wrong number. Space exists only per pool per member, and a
shared backing store reports the whole thing to every member that can see it.

The fixture is the three-member QA stub's own POOLS table, so this file and the browser suite
assert the same arithmetic against the same numbers.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import unittest

import pytest

from app.services.lxd import node_resources as nr


GB = 10 ** 9

MEMBERS = [
    {"server_name": "microcloud20", "status": "Online", "url": "https://h:8443"},
    {"server_name": "microcloud30", "status": "Online", "url": "https://h:8443"},
    {"server_name": "microcloud40", "status": "Online", "url": "https://h:8443"},
]

# tools/clustered-lxd-stub.py POOLS. `remote` is one Ceph store, seen from two members.
POOLS: Dict[str, Dict[str, tuple]] = {
    "microcloud20": {"local": ("zfs", 200 * GB, 50 * GB), "remote": ("ceph", 400 * GB, 100 * GB)},
    "microcloud30": {"local": ("zfs", 300 * GB, 30 * GB)},
    "microcloud40": {
        "local": ("zfs", 100 * GB, 10 * GB),
        "remote": ("ceph", 400 * GB, 120 * GB),
        "scratch": ("dir", 50 * GB, 5 * GB),
    },
}

RESOURCES = {
    m["server_name"]: {
        "cpu": {"total": 8, "sockets": [{"cores": []}]},
        "memory": {"total": 32 * GB, "used": 8 * GB},
        # The key the old code read. Present, and deliberately holding media rather than space,
        # so a regression that goes back to it reports something visibly wrong rather than zero.
        "storage": {"disks": [{"id": "nvme0n1", "size": 1920 * GB, "type": "nvme"}]},
        "gpu": {"cards": []},
        "system": {},
    }
    for m in MEMBERS
}


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return {"metadata": self._payload}


class _Node:
    """One addressable path under client.api, recording what it was asked."""

    def __init__(self, client: "FakeClient", parts: List[str]) -> None:
        self._client = client
        self._parts = parts

    def __getitem__(self, item: str) -> "_Node":
        return _Node(self._client, self._parts + [item])

    def __getattr__(self, item: str) -> "_Node":
        return _Node(self._client, self._parts + [item])

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        # timeout: project_scope passes one on every read, and a fake that refuses it makes
        # the read raise, which used to be swallowed into an empty instance list.
        return self._client.handle(self._parts, params or {})


class FakeClient:
    def __init__(self, fail_pool: str = None, fail_list_on: str = None) -> None:
        # api_extensions matters: project_scope asks for it before choosing the fast
        # all-projects read or the per-project walk, and a client that reports none takes the
        # walk. Either way the walk must be able to answer, or the instance read fails and the
        # aggregate reports the whole cluster as idle.
        self.host_info = {
            "environment": {"server_clustered": True, "server_name": "microcloud30"},
            "api_extensions": [],
        }
        self.fail_pool = fail_pool
        self.fail_list_on = fail_list_on
        self.calls: List[str] = []
        self.api = _Node(self, [])
        self.instances = SimpleNamespace(all=lambda recursion=0: [])

    def handle(self, parts: List[str], params: Dict[str, Any]) -> _Response:
        member = params.get("target")
        self.calls.append("/".join(parts) + (f"?target={member}" if member else ""))

        if parts == ["cluster", "members"]:
            return _Response(MEMBERS)
        # The instance read this aggregate depends on. Without these two the read raises, and
        # before the degraded signal was wired up that silently became "no instances", so this
        # test asserted a healthy cluster while its instance read was failing every time.
        if parts == ["projects"]:
            return _Response([{"name": "default", "config": {}}])
        if parts == ["instances"]:
            return _Response([])
        if parts == ["resources"]:
            return _Response(RESOURCES[member])
        if parts == ["storage-pools"]:
            if member == self.fail_list_on:
                raise RuntimeError("pool list refused")
            return _Response(
                [{"name": n, "driver": d} for n, (d, _t, _u) in POOLS[member].items()]
            )
        if len(parts) == 3 and parts[0] == "storage-pools" and parts[2] == "resources":
            name = parts[1]
            if name == self.fail_pool:
                raise RuntimeError(f"pool {name} refused")
            _driver, total, used = POOLS[member][name]
            return _Response({"space": {"total": total, "used": used}})
        raise AssertionError(f"unexpected path {parts} {params}")


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    class _NullCache:
        def get(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    monkeypatch.setattr(nr, "get_cache_manager", lambda: _NullCache(), raising=False)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _NullCache())


def _aggregate(client: FakeClient):
    return nr.aggregate_microcloud_resources(client, "central2", {})


def test_a_clustered_host_no_longer_reports_zero_storage():
    result = _aggregate(FakeClient())
    assert result is not None
    assert result.storage_total > 0, "the whole defect: every clustered host reported nothing"


def test_the_shared_pool_is_counted_once():
    result = _aggregate(FakeClient())
    assert result.storage_total == 1050 * GB


def test_the_raw_sum_is_still_available():
    result = _aggregate(FakeClient())
    assert result.storage_raw_total == 1450 * GB
    assert result.storage_raw_total - result.storage_total == 400 * GB


def test_the_pool_rows_carry_their_scope_and_their_members():
    result = _aggregate(FakeClient())
    rows = {p["name"]: p for p in result.storage_pools}
    assert rows["remote"]["scope"] == "shared"
    assert rows["remote"]["members"] == ["microcloud20", "microcloud40"]
    assert rows["local"]["scope"] == "local"
    assert rows["local"]["members"] == ["microcloud20", "microcloud30", "microcloud40"]
    assert rows["scratch"]["space_total"] == 50 * GB


def test_the_media_block_is_not_mistaken_for_pool_space():
    """`storage.disks` holds a 1920 GB device per member. If a regression reads it again the
    total moves to something no pool accounts for, and this catches it by value."""
    result = _aggregate(FakeClient())
    assert result.storage_total != 3 * 1920 * GB
    assert result.storage_total == sum(p["space_total"] for p in result.storage_pools)


def test_space_is_read_per_member_by_target():
    client = FakeClient()
    _aggregate(client)
    for member in ("microcloud20", "microcloud30", "microcloud40"):
        assert f"storage-pools?target={member}" in client.calls
    assert "storage-pools" not in client.calls, "an untargeted read would be one member's view"


def test_a_pool_that_cannot_be_read_is_named_not_silently_zero():
    result = _aggregate(FakeClient(fail_pool="remote"))
    assert result.degraded is True
    assert "storage:remote" in result.degraded_parts
    # the local pools still count, so the number is a floor rather than a loss
    assert result.storage_total == 650 * GB


def test_a_pool_list_that_fails_marks_only_that_member():
    result = _aggregate(FakeClient(fail_list_on="microcloud30"))
    assert result.degraded is True
    assert "storage" in result.degraded_parts
    # microcloud30 contributed nothing; the rest still did
    assert result.storage_total == 750 * GB


def test_a_healthy_read_is_not_marked_degraded():
    result = _aggregate(FakeClient())
    assert result.degraded is False
    assert result.degraded_parts == []


class OneAnswerPerHost(unittest.TestCase):
    """GET /nodes and GET /nodes/<name>/resources must not disagree about the same host.

    The list path aggregated members and the per-node path did not, so the node detail dialog
    painted the cluster's capacity from the list row and then replaced it with one member's
    share as soon as a tab was opened. `resources_for_host` is now the only place that choice
    is made.
    """

    def _own_reading(self):
        return {
            "cpu_total": 8,
            "cpu_available": 2,
            "memory_total": 32 * GB,
            "memory_available": 24 * GB,
            "storage_total": 600 * GB,
            "storage_available": 500 * GB,
            "storage_pools": [{"name": "local", "driver": "zfs"}],
        }

    def test_a_clustered_host_answers_with_the_cluster(self):
        from app.services.lxd.gpu import GpuInventory

        result = nr.resources_for_host(
            FakeClient(), "central2", self._own_reading(), GpuInventory.empty(), True
        )
        self.assertEqual(1050 * GB, result.storage_total)
        self.assertEqual(24, result.cpu_total, "three members of 8, not the one we connected to")

    def test_a_standalone_host_answers_with_itself(self):
        from app.services.lxd.gpu import GpuInventory

        result = nr.resources_for_host(
            FakeClient(), "nuc2", self._own_reading(), GpuInventory.empty(), False
        )
        self.assertEqual(600 * GB, result.storage_total)
        self.assertEqual(8, result.cpu_total)
        self.assertEqual(100 * GB, result.storage_used, "derived from total minus available")

    def test_a_failed_aggregation_falls_back_rather_than_answering_nothing(self):
        from app.services.lxd.gpu import GpuInventory

        class _NoMembers(FakeClient):
            def handle(self, parts, params):
                if parts == ["cluster", "members"]:
                    raise RuntimeError("members unavailable")
                return super().handle(parts, params)

        result = nr.resources_for_host(
            _NoMembers(), "central2", self._own_reading(), GpuInventory.empty(), True
        )
        self.assertEqual(600 * GB, result.storage_total)
