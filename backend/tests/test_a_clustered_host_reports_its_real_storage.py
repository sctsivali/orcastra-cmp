"""A clustered host's storage comes from each member's pool space, folded once.

Measured against a real three-member MicroCloud (2 CPU / 4 GiB / 18 GiB zfs `local` per
member): /nodes reported CPU 6, RAM 12 GiB and storage 0. The aggregate read
member_resources["storage"]["pools"], a key /1.0/resources does not send; the block it does
carry is storage.disks, the physical media. Pool space lives at
/1.0/storage-pools/<pool>/resources?target=<member>, the read the single-host path and the
member panel already make.

Summing that per member is right for a per-member pool and wrong for a shared one, because
every member reports the whole store. services/lxd/storage_model.py classifies by driver.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.services.lxd import node_resources as nr


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Endpoint:
    def __init__(self, handler) -> None:
        self._handler = handler

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        return _Response(self._handler(params or {}))


GIB = 1024 ** 3


class _Pools:
    """client.api.storage_pools: the list, and [name].resources per member."""

    def __init__(self, pools: List[Dict[str, Any]], space: Dict[str, Dict[str, Dict[str, int]]]) -> None:
        self._pools = pools
        self._space = space  # pool -> member -> {total, used}
        self.reads: List[str] = []

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        assert (params or {}).get("recursion") == 1, "the pool list must carry the driver"
        return _Response({"metadata": self._pools})

    def __getitem__(self, name: str):
        def _space(params: Dict[str, Any]) -> Dict[str, Any]:
            member = params["target"]
            self.reads.append(f"{name}@{member}")
            if member not in self._space[name]:
                raise RuntimeError(f"{member} did not answer")
            return {"metadata": {"space": self._space[name][member]}}

        return SimpleNamespace(resources=_Endpoint(_space))


class FakeClient:
    def __init__(self, members: List[str], pools, space) -> None:
        self.host_info = {
            "environment": {"server_clustered": True, "server_name": members[0]},
            "api_extensions": ["instance_all_projects"],
        }
        rows = [{"server_name": m, "status": "Online", "url": f"https://{m}:8443"} for m in members]
        self.pools = _Pools(pools, space)
        # /1.0/resources per member: real daemons send storage.disks and storage.total, no pools
        per_member = {
            "cpu": {"total": 2, "sockets": [{"socket": 0, "cores": 2}]},
            "memory": {"total": 4 * GIB, "used": GIB},
            "storage": {"disks": [{"id": "sda"}, {"id": "sdb"}], "total": 2},
        }
        self.api = SimpleNamespace(
            cluster=SimpleNamespace(members=_Endpoint(lambda p: {"metadata": rows})),
            resources=_Endpoint(lambda p: {"metadata": per_member}),
            storage_pools=self.pools,
        )



@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    class _NullCache:
        def get(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _NullCache())


@pytest.fixture(autouse=True)
def _no_instances(monkeypatch):
    monkeypatch.setattr(nr, "list_instance_records", lambda client: ([], True))


MEMBERS = ["mc1", "mc2", "mc3"]
LOCAL = {"mc1": {"total": 18 * GIB, "used": 1 * GIB},
         "mc2": {"total": 18 * GIB, "used": 2 * GIB},
         "mc3": {"total": 18 * GIB, "used": 3 * GIB}}


def test_a_per_member_pool_is_summed_across_members() -> None:
    client = FakeClient(MEMBERS, [{"name": "local", "driver": "zfs"}], {"local": LOCAL})
    got = nr.aggregate_microcloud_resources(client, "mc-lab", {})
    assert got is not None
    assert got.cpu_total == 6 and got.memory_total == 12 * GIB, "compute is unchanged"
    assert got.storage_total == 54 * GIB, "three 18 GiB zfs pools are three disks"
    assert got.storage_available == 54 * GIB - 6 * GIB
    assert [p["name"] for p in got.storage_pools] == ["local"]
    assert got.storage_pools[0]["scope"] == "local"
    assert got.storage_pools[0]["members"] == MEMBERS
    assert sorted(client.pools.reads) == sorted(f"local@{m}" for m in MEMBERS)


def test_a_shared_pool_is_counted_once() -> None:
    remote = {m: {"total": 400 * GIB, "used": 50 * GIB} for m in MEMBERS}
    client = FakeClient(
        MEMBERS,
        [{"name": "local", "driver": "zfs"}, {"name": "remote", "driver": "ceph"}],
        {"local": LOCAL, "remote": remote},
    )
    got = nr.aggregate_microcloud_resources(client, "mc-lab", {})
    assert got.storage_total == 54 * GIB + 400 * GIB, "ceph seen from three members is one store"
    by_name = {p["name"]: p for p in got.storage_pools}
    assert by_name["remote"]["scope"] == "shared"
    assert by_name["remote"]["space_total"] == 400 * GIB
    assert by_name["remote"]["members"] == MEMBERS


def test_a_member_that_does_not_answer_understates_rather_than_fails() -> None:
    partial = {"mc1": LOCAL["mc1"], "mc2": LOCAL["mc2"]}  # mc3 raises
    client = FakeClient(MEMBERS, [{"name": "local", "driver": "zfs"}], {"local": partial})
    got = nr.aggregate_microcloud_resources(client, "mc-lab", {})
    assert got is not None, "one unreadable pool must not drop the whole aggregate"
    assert got.storage_total == 36 * GIB
    assert got.cpu_total == 6, "the member's compute still counts"


def test_the_resources_body_is_not_read_for_storage() -> None:
    """The bug: /1.0/resources has no pools key, and its storage.total is a disk count."""
    client = FakeClient(MEMBERS, [], {})
    got = nr.aggregate_microcloud_resources(client, "mc-lab", {})
    assert got.storage_total == 0 and got.storage_pools == []
