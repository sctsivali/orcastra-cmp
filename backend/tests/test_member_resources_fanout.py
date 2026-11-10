"""The member resource read must be shallow, concurrent, and honest about what it lost.

At pylxd's default `recursion=0` an instance list builds objects carrying only a name, and the
first read of a declared attribute such as `location` calls `sync()`, one sequential GET per
instance in the cluster. The fake below reproduces that, so the test can fail before it
passes.

`recursion=1` on the pool list makes the per-pool detail call unnecessary. A storage or
instance read that fails is reported as degraded, so the response never shows zeroes as a fact.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.services.lxd import member_resources as mr


RESOURCES = {
    "cpu": {"total": 8},
    "memory": {"total": 34359738368, "used": 8589934592},
    "gpu": {"cards": []},
    "system": {},
}


class _LazyInstance:
    """A pylxd Instance as it comes back from recursion=0: a name and nothing else.

    Touching any other attribute is a round trip in the real client, so here it is an error.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(
            f"reading .{item} on a recursion=0 instance would have been an HTTP request"
        )


class _Instance:
    def __init__(self, name: str, location: str, status: str, limits_cpu: Optional[str]) -> None:
        self.name = name
        self.location = location
        self.status = status
        self.config = {"limits.cpu": limits_cpu} if limits_cpu else {}
        self.expanded_config = dict(self.config)
        self.devices: Dict[str, Any] = {}
        self.expanded_devices: Dict[str, Any] = {}


INSTANCES = [
    _Instance("qa-a", "microcloud20", "Running", "2"),
    _Instance("qa-b", "microcloud20", "Running", "0-3"),
    _Instance("qa-c", "microcloud20", "Stopped", "1"),
    _Instance("qa-d", "microcloud30", "Running", "4"),
]


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> Dict[str, Any]:
        return {"metadata": self._payload}


class _Node:
    """One addressable path under client.api, recording the params it was called with."""

    def __init__(self, client: "FakeClient", parts: List[str]) -> None:
        self._client = client
        self._parts = parts

    def __getitem__(self, item: str) -> "_Node":
        return _Node(self._client, self._parts + [item])

    def __getattr__(self, item: str) -> "_Node":
        return _Node(self._client, self._parts + [item])

    def get(self, params: Optional[Dict[str, Any]] = None, timeout: Any = None) -> _Response:
        # The real _APINode takes a timeout, and the all-projects read passes one explicitly.
        return self._client.handle(self._parts, params or {})


class FakeClient:
    """The subset of pylxd.Client this fan-out touches."""

    def __init__(
        self,
        pools: Optional[Dict[str, Dict[str, Any]]] = None,
        recursion_on_pools: bool = True,
        fail_pool: Optional[str] = None,
        fail_pool_list: bool = False,
        fail_instances: bool = False,
        pool_delay: float = 0.0,
    ) -> None:
        self.pools = pools if pools is not None else {
            "local": {"driver": "zfs", "total": 200, "used": 50},
            "remote": {"driver": "ceph", "total": 400, "used": 100},
        }
        self.recursion_on_pools = recursion_on_pools
        self.fail_pool = fail_pool
        self.fail_pool_list = fail_pool_list
        self.fail_instances = fail_instances
        self.pool_delay = pool_delay
        self.calls: List[str] = []
        # Lets the member read cover every project in one request.
        self.host_info = {"api_extensions": ["instance_all_projects"]}
        self.project = None
        self.instance_recursions: List[int] = []
        self.instance_all_projects: List[Any] = []
        self.api = _Node(self, [])

        def _all(recursion: int = 0) -> List[Any]:
            self.instance_recursions.append(recursion)
            if self.fail_instances:
                raise RuntimeError("instance list refused")
            if recursion == 0:
                return [_LazyInstance(i.name) for i in INSTANCES]
            return list(INSTANCES)

        self.instances = SimpleNamespace(all=_all)

    def handle(self, parts: List[str], params: Dict[str, Any]) -> _Response:
        key = "/".join(parts) + (f"?{sorted(params.items())}" if params else "")
        self.calls.append(key)
        if parts == ["instances"]:
            # The member read asks the raw API for every project at recursion=1.
            # client.instances.all() is bound to the default project and would miss the rest.
            self.instance_recursions.append(int(params.get("recursion", 0)))
            self.instance_all_projects.append(params.get("all-projects"))
            if self.fail_instances:
                raise RuntimeError("instance list refused")
            if int(params.get("recursion", 0)) == 0:
                return _Response([{"name": i.name} for i in INSTANCES])
            return _Response([
                {
                    "name": i.name, "project": "default", "location": i.location,
                    "status": i.status, "config": i.config, "devices": i.devices,
                    "expanded_config": i.expanded_config,
                    "expanded_devices": i.expanded_devices,
                }
                for i in INSTANCES
            ])
        if parts == ["resources"]:
            return _Response(RESOURCES)
        if parts == ["storage-pools"]:
            if self.fail_pool_list:
                raise RuntimeError("pool list refused")
            if params.get("recursion") and self.recursion_on_pools:
                return _Response(
                    [{"name": n, "driver": p["driver"]} for n, p in self.pools.items()]
                )
            return _Response([f"/1.0/storage-pools/{n}" for n in self.pools])
        if len(parts) == 2 and parts[0] == "storage-pools":
            return _Response({"name": parts[1], "driver": self.pools[parts[1]]["driver"]})
        if len(parts) == 3 and parts[0] == "storage-pools" and parts[2] == "resources":
            name = parts[1]
            if self.pool_delay:
                time.sleep(self.pool_delay)
            if name == self.fail_pool:
                raise RuntimeError(f"pool {name} refused")
            pool = self.pools[name]
            return _Response({"space": {"total": pool["total"], "used": pool["used"]}})
        raise AssertionError(f"unexpected path {parts}")

    def pool_detail_calls(self) -> List[str]:
        return [c for c in self.calls if c.count("/") == 1 and c.startswith("storage-pools/")]


def _collect(client: FakeClient, member: str = "microcloud20") -> mr.MemberFanout:
    return asyncio.run(mr.collect_member_fanout(client, "central2", member))


def test_the_fake_reproduces_the_lazy_sync_defect():
    """The fake must be able to fail before a pass can be trusted."""
    client = FakeClient()
    lazy = client.instances.all()
    with pytest.raises(AssertionError):
        _ = lazy[0].location


def test_a_member_read_never_lazily_syncs_an_instance():
    client = FakeClient()
    fanout = _collect(client)
    # recursion=1 carries everything the readers duck-type; recursion=2 adds state, snapshots
    # and backups at several times the cost. Without recursion each attribute read is a GET.
    assert client.instance_recursions == [1], (
        f"the instance list must ask for recursion, got {client.instance_recursions}"
    )
    assert client.instance_all_projects == ["true"], (
        "the member read must span every project, not just the default one"
    )
    # Every object handed on must already carry its attributes.
    for instance in fanout.instances:
        assert instance.location


def test_the_pool_list_carries_the_driver_so_there_is_no_detail_call():
    client = FakeClient()
    fanout = _collect(client)
    assert client.pool_detail_calls() == [], (
        f"recursion=1 on the pool list makes the detail read redundant, saw "
        f"{client.pool_detail_calls()}"
    )
    assert {p.name: p.driver for p in fanout.pools} == {"local": "zfs", "remote": "ceph"}


def test_a_daemon_that_ignores_recursion_still_reports_a_driver():
    """Without recursion the driver comes from the detail read, never 'unknown'."""
    client = FakeClient(recursion_on_pools=False)
    fanout = _collect(client)
    assert {p.name: p.driver for p in fanout.pools} == {"local": "zfs", "remote": "ceph"}
    assert client.pool_detail_calls(), "the fallback must actually read the detail"
    assert not fanout.degraded


def test_pool_space_reads_run_concurrently():
    client = FakeClient(pool_delay=0.25)
    started = time.monotonic()
    _collect(client)
    elapsed = time.monotonic() - started
    assert elapsed < 0.45, (
        f"two 0.25s pool reads took {elapsed:.2f}s, so they ran one after the other"
    )


def test_a_pool_that_fails_is_named_rather_than_counted_as_zero():
    client = FakeClient(fail_pool="remote")
    fanout = _collect(client)
    assert fanout.degraded
    assert "storage:remote" in fanout.degraded_parts
    # The surviving pool is still counted, so the number is a floor and says so.
    assert [p.name for p in fanout.pools] == ["local"]


def test_a_pool_list_failure_marks_storage_degraded():
    client = FakeClient(fail_pool_list=True)
    fanout = _collect(client)
    assert fanout.degraded_parts == [mr.PART_STORAGE]
    assert fanout.pools == []


def test_an_instance_scan_failure_is_not_silently_zero_instances():
    """Swallowed, this reports a busy member as idle with all its GPUs free."""
    client = FakeClient(fail_instances=True)
    fanout = _collect(client)
    assert fanout.degraded_parts == [mr.PART_INSTANCES]
    assert fanout.instances == []
    # Storage still came back, so the failure is scoped rather than total.
    assert len(fanout.pools) == 2


def test_a_healthy_read_is_not_marked_degraded():
    fanout = _collect(FakeClient())
    assert not fanout.degraded
    assert fanout.degraded_parts == []
    assert fanout.resources_body == RESOURCES
    assert fanout.collected_at is not None


def test_instance_counts_are_scoped_to_the_member():
    usage = mr.count_member_instances(INSTANCES, "microcloud20")
    assert usage.count == 3
    assert usage.running == 2
    # 2 from "2", 4 from the range "0-3". The stopped one is not charged.
    assert usage.cpu_used == 6

    other = mr.count_member_instances(INSTANCES, "microcloud30")
    assert (other.count, other.running, other.cpu_used) == (1, 1, 4)


@pytest.mark.parametrize(
    "value,expected",
    [("4", 4), ("0-3", 4), ("0,1,2", 3), (None, 1), ("", 1), ("garbage", 1), ("3-", 1)],
)
def test_cpu_limits_are_parsed_the_way_lxd_writes_them(value, expected):
    assert mr.parse_cpu_limit(value) == expected
