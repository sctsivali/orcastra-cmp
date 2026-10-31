"""Host kernels are read member by member, and a member that cannot be read says so.

A cluster patched on one member and not another is the case one endpoint read would hide, so
each Online member is asked with `?target=`. An Offline member is never asked (that request
hangs until the read timeout), a daemon that ignores `target` is caught by the name it answers
with, and only facts are cached: ten minutes when every member answered, one when not.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from app.core.blocking import BlockingTimeout
from app.services.lxd import host_kernels
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.host_kernels import (
    HOST_KERNELS_DEGRADED_TTL,
    HOST_KERNELS_TTL,
    fleet_kernels,
    read_cluster_kernels,
)


def _env(name: str, kernel: str, clustered: bool = False) -> Dict[str, Any]:
    return {"server_name": name, "kernel_version": kernel, "os_name": "Ubuntu", "os_version": "22.04",
            "instance_types": ["container", "virtual-machine"], "server_clustered": clustered,
            "certificate": "-----BEGIN CERTIFICATE-----"}


class _Resp:
    def __init__(self, body: Any):
        self._body = body
        self.closed = False

    def json(self) -> Any:
        return self._body

    def iter_content(self, _size: int):
        yield json.dumps(self._body).encode()

    def close(self) -> None:
        self.closed = True


class _Node:
    def __init__(self, path: str, handler):
        self._path, self._handler = path, handler

    def __getattr__(self, name: str) -> "_Node":
        if name.startswith("_"):
            raise AttributeError(name)
        return _Node(f"{self._path}/{name}", self._handler)

    def get(self, params=None, timeout=None, stream=False):
        return self._handler(self._path, dict(params or {}), timeout)


class _Client:
    def __init__(self, environment: Dict[str, Any], handler):
        self.host_info = {"environment": environment}
        self.api = _Node("/1.0", handler)


class _Service:
    def __init__(self, client: Optional[_Client]):
        self._client = client
        self.connection_manager = self

    def get_client(self, _cluster_id: str) -> Optional[_Client]:
        return self._client

    def ensure_connection(self, _cluster_id: str) -> bool:
        return False


class _Cache:
    def __init__(self):
        self.store: Dict[str, Any] = {}
        self.ttls: Dict[str, int] = {}

    def get(self, namespace: str, key: str) -> Any:
        return self.store.get(f"{namespace}|{key}")

    def set(self, namespace: str, key: str, value: Any, ttl: int = 0) -> None:
        self.store[f"{namespace}|{key}"] = value
        self.ttls[f"{namespace}|{key}"] = ttl


MEMBERS = [
    {"server_name": "m1", "status": "Online", "message": "Fully operational"},
    {"server_name": "m2", "status": "Online", "message": "Fully operational"},
    {"server_name": "m3", "status": "Offline", "message": "No heartbeat for 3m"},
]


def _cluster_handler(calls: List[Any], per_member: Dict[str, Any]):
    def handler(path, params, timeout):
        calls.append((path, params))
        if path == "/1.0/cluster/members":
            return _Resp({"metadata": MEMBERS})
        assert path == "/1.0" and "target" in params and timeout is not None
        answer = per_member[params["target"]]
        if isinstance(answer, Exception):
            raise answer
        return _Resp({"metadata": {"environment": answer}})
    return handler


def _ttl(cache: _Cache, cluster_id: str) -> int:
    return cache.ttls[f"api_responses|{host_kernels._cache_key(cluster_id)}"]


def test_a_standalone_host_is_one_member_from_host_info():
    calls: List[Any] = []
    cache = _Cache()
    client = _Client(_env("none", "5.15.0-185-generic"), _cluster_handler(calls, {}))
    result = read_cluster_kernels(_Service(client), "nuc2", cache)
    assert calls == []
    assert [(m.member, m.kernel_version, m.error) for m in result.members] == [("nuc2", "5.15.0-185-generic", None)]
    assert result.complete and not result.clustered
    assert _ttl(cache, "nuc2") == HOST_KERNELS_TTL
    assert "certificate" not in str(cache.store)


def test_every_online_member_is_read_and_an_offline_one_is_not_asked():
    calls: List[Any] = []
    cache = _Cache()
    handler = _cluster_handler(calls, {"m2": _env("m2", "6.12.113")})
    client = _Client(_env("m1", "5.15.0-185-generic", clustered=True), handler)
    result = read_cluster_kernels(_Service(client), "c1", cache)

    targets = [params.get("target") for path, params in calls if path == "/1.0"]
    assert targets == ["m2"]
    by_name = {m.member: m for m in result.members}
    assert by_name["m1"].kernel_version == "5.15.0-185-generic"
    assert by_name["m2"].kernel_version == "6.12.113"
    assert by_name["m3"].kernel_version is None and by_name["m3"].error.startswith("offline: No heartbeat")
    assert not result.complete
    assert _ttl(cache, "c1") == HOST_KERNELS_DEGRADED_TTL


def test_a_member_that_answers_as_another_is_not_trusted():
    calls: List[Any] = []
    handler = _cluster_handler(calls, {"m2": _env("m1", "6.12.113")})
    client = _Client(_env("m1", "5.15.0-185-generic", clustered=True), handler)
    result = read_cluster_kernels(_Service(client), "c1", _Cache())
    m2 = next(m for m in result.members if m.member == "m2")
    assert m2.kernel_version is None and m2.error == "mismatch: answered as m1"


def test_a_member_read_failure_is_recorded_on_that_member():
    import requests

    calls: List[Any] = []
    handler = _cluster_handler(calls, {"m2": requests.exceptions.ReadTimeout("read timed out")})
    client = _Client(_env("m1", "5.15.0-185-generic", clustered=True), handler)
    result = read_cluster_kernels(_Service(client), "c1", _Cache())
    m2 = next(m for m in result.members if m.member == "m2")
    assert m2.kernel_version is None and m2.error.startswith("unreachable")


def test_an_unreadable_members_list_reports_only_the_endpoint():
    def handler(path, params, timeout):
        raise RuntimeError("members list refused")

    client = _Client(_env("m1", "5.15.0-185-generic", clustered=True), handler)
    result = read_cluster_kernels(_Service(client), "c1", _Cache())
    assert not result.members_listed
    assert [m.member for m in result.members] == ["m1"]


def test_no_client_raises_a_connection_error():
    with pytest.raises(LXDConnectionError):
        read_cluster_kernels(_Service(None), "dead", _Cache())


def test_the_fleet_serves_the_cache_unless_fresh_and_names_what_it_could_not_read(monkeypatch):
    cache = _Cache()
    reads: List[str] = []

    def fake_read(_service, cluster_id, _cache):
        reads.append(cluster_id)
        if cluster_id == "dead":
            raise LXDConnectionError("Could not connect to dead")
        return read_cluster_kernels(_Service(_Client(_env("none", "6.12.113"), None)), cluster_id, _cache)

    monkeypatch.setattr(host_kernels, "read_cluster_kernels", fake_read)
    first = asyncio.run(fleet_kernels(object(), ["a", "dead"], cache, fresh=False, wall=5))
    assert [c.cluster_id for c in first.clusters] == ["a"]
    assert first.unreachable_nodes == ["dead"] and first.node_errors["dead"].startswith("unreachable")

    reads.clear()
    asyncio.run(fleet_kernels(object(), ["a"], cache, fresh=False, wall=5))
    assert reads == []
    asyncio.run(fleet_kernels(object(), ["a"], cache, fresh=True, wall=5))
    assert reads == ["a"]


def test_a_cluster_past_the_wall_is_named_with_the_wall(monkeypatch):
    async def timing_out(*_a, **_k):
        raise BlockingTimeout("host_kernels[slow]", 1.0)

    monkeypatch.setattr(host_kernels, "run_blocking", timing_out)
    fleet = asyncio.run(fleet_kernels(object(), ["slow"], _Cache(), fresh=True, wall=12))
    assert fleet.unreachable_nodes == ["slow"]
    assert "within 12s" in fleet.node_errors["slow"]


def test_a_host_answering_non_text_facts_reads_as_invalid_not_as_a_crash():
    cache = _Cache()
    env = dict(_env("none", "x"), kernel_version=5, os_name=["Ubuntu"], instance_types="virtual-machine")
    result = read_cluster_kernels(_Service(_Client(env, None)), "evil", cache)
    member = result.members[0]
    assert member.kernel_version is None and member.os_name is None and member.instance_types == []
    assert member.error.startswith("invalid: kernel_version, os_name, instance_types")


def test_a_poisoned_cache_entry_is_sanitised_on_the_way_out():
    cache = _Cache()
    cache.set("api_responses", host_kernels._cache_key("c1"), {
        "cluster_id": "c1", "clustered": False, "members_listed": True, "collected_at": "x",
        "members": [{"member": "m1", "kernel_version": {"a": 1}, "os_name": None}, "garbage"],
    })
    fleet = asyncio.run(fleet_kernels(object(), ["c1"], cache, fresh=False, wall=5))
    [member] = fleet.clusters[0].members
    assert member.kernel_version is None and member.error.startswith("invalid")


def test_a_cluster_listing_more_members_than_the_cap_reads_only_the_cap(monkeypatch):
    many = [{"server_name": f"m{i}", "status": "Online", "message": ""} for i in range(host_kernels.MEMBER_CAP + 5)]
    asked = []

    def handler(path, params, timeout):
        if path == "/1.0/cluster/members":
            return _Resp({"metadata": many})
        asked.append(params["target"])
        return _Resp({"metadata": {"environment": _env(params["target"], "6.12.113")}})

    client = _Client(_env("m0", "6.12.113", clustered=True), handler)
    result = read_cluster_kernels(_Service(client), "big", _Cache())
    assert len(result.members) == host_kernels.MEMBER_CAP + 5
    assert len(asked) == host_kernels.MEMBER_CAP - 1
    assert [m.error for m in result.members[-5:]] == [f"not read: more than {host_kernels.MEMBER_CAP} members"] * 5


def test_a_member_that_never_answers_is_bounded_by_the_phase_deadline(monkeypatch):
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(host_kernels, "MEMBER_PHASE_SECONDS", 0.5)

    def handler(path, params, timeout):
        if path == "/1.0/cluster/members":
            return _Resp({"metadata": MEMBERS[:2]})
        release.wait(5)
        return _Resp({"metadata": {"environment": _env(params["target"], "6.12.113")}})

    client = _Client(_env("m1", "5.15.0-185-generic", clustered=True), handler)
    started = time.monotonic()
    result = read_cluster_kernels(_Service(client), "slow", _Cache())
    release.set()
    assert time.monotonic() - started < 3
    m2 = next(m for m in result.members if m.member == "m2")
    assert m2.error.startswith("unreachable: did not answer within")


def test_an_oversized_member_answer_is_refused(monkeypatch):
    monkeypatch.setattr(host_kernels, "MEMBER_BODY_LIMIT", 100)

    def handler(path, params, timeout):
        if path == "/1.0/cluster/members":
            return _Resp({"metadata": MEMBERS[:2]})
        return _Resp({"metadata": {"environment": dict(_env("m2", "6.12.113"), padding="x" * 500)}})

    client = _Client(_env("m1", "5.15.0-185-generic", clustered=True), handler)
    result = read_cluster_kernels(_Service(client), "fat", _Cache())
    m2 = next(m for m in result.members if m.member == "m2")
    assert m2.kernel_version is None and "larger than the member read limit" in m2.error


def test_deleting_a_cluster_clears_its_host_kernels():
    from app.core.cache import CacheManager

    deleted = []
    manager = CacheManager.__new__(CacheManager)
    manager.delete = lambda namespace, key: deleted.append((namespace, key))
    manager.delete_pattern = lambda *_a, **_k: None
    manager._advance_resource_generation = lambda *_a, **_k: None
    manager.invalidate_cluster_cache("c1")
    assert ("api_responses", host_kernels._cache_key("c1")) in deleted
