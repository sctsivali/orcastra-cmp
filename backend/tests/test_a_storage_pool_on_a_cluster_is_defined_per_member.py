"""Creating a storage pool on a cluster is two-phase, like a bridge network.

One cluster-wide POST answered 'Pool not pending on any node (use --target <node> first)' on a
3-member MicroCloud, so no pool could be created from the console there. LXD wants the pool
defined on every member first (POST ?target=<member>, carrying only the member-specific keys)
and then created cluster-wide with the rest. A standalone host keeps the single create.
"""

from types import SimpleNamespace
from typing import Any, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import storage_pools as pools_module
from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.services.lxd_service import get_lxd_service

MEMBERS = ["mc1", "mc2", "mc3"]


@asynccontextmanager
async def _no_configuration_lock(_db, _host):
    yield


class _RawPools:
    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []

    def post(self, json: Dict[str, Any], params: Dict[str, Any] = None, timeout=None) -> None:
        self.posts.append({"json": json, "params": dict(params or {})})


class _Pool:
    def __init__(self, name: str, driver: str) -> None:
        self.name, self.driver = name, driver
        self.config, self.description, self.used_by, self.status = {}, "", [], "Created"


class _PoolsApi:
    def __init__(self) -> None:
        self.created: List[Dict[str, Any]] = []

    def create(self, body: Dict[str, Any]) -> _Pool:
        self.created.append(body)
        return _Pool(body["name"], body["driver"])

    def get(self, name: str) -> _Pool:
        return _Pool(name, "dir")


def _app(monkeypatch, clustered: bool):
    raw = _RawPools()
    pools_api = _PoolsApi()
    client = SimpleNamespace(api=SimpleNamespace(storage_pools=raw), storage_pools=pools_api)
    svc = SimpleNamespace(get_client_with_reconnect=lambda host: client)
    monkeypatch.setattr(pools_module, "is_clustered", lambda c: clustered)
    monkeypatch.setattr(pools_module, "cluster_member_names", lambda c: list(MEMBERS))
    monkeypatch.setattr(pools_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(
        pools_module,
        "serialize_cluster_configuration",
        _no_configuration_lock,
    )
    monkeypatch.setattr(pools_module.activity_logger, "log_activity", lambda **kw: None)
    monkeypatch.setattr(
        pools_module, "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *a, **k: None),
    )
    app = FastAPI()
    app.include_router(pools_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), raw, pools_api


BODY = {"name": "qa-zfs", "driver": "zfs", "description": "qa",
        "config": {"size": "5GiB", "source": "/dev/sdb", "zfs.pool_name": "qa", "volume.size": "1GiB"}}


def test_a_cluster_defines_every_member_with_only_member_keys_then_creates(monkeypatch) -> None:
    client, raw, pools_api = _app(monkeypatch, clustered=True)
    r = client.post("/storage/pools", params={"host": "mc-lab"}, json=BODY)
    assert r.status_code == 200, r.text
    assert [p["params"].get("target") for p in raw.posts] == MEMBERS + [None]
    for p in raw.posts[:-1]:
        assert p["json"]["config"] == {"size": "5GiB", "source": "/dev/sdb", "zfs.pool_name": "qa"}
        assert p["json"]["driver"] == "zfs"
    assert raw.posts[-1]["json"]["config"] == {"volume.size": "1GiB"}, "no member key cluster-wide"
    assert raw.posts[-1]["json"]["description"] == "qa"
    assert pools_api.created == [], "the single-host create is not used on a cluster"
    assert r.json()["pool"]["name"] == "qa-zfs"


def test_a_standalone_host_keeps_the_single_create(monkeypatch) -> None:
    client, raw, pools_api = _app(monkeypatch, clustered=False)
    r = client.post("/storage/pools", params={"host": "nuc2"}, json=BODY)
    assert r.status_code == 200, r.text
    assert raw.posts == []
    assert pools_api.created[0]["config"] == BODY["config"]


def test_the_member_keys_are_the_daemons_list() -> None:
    assert pools_module.MEMBER_SPECIFIC_POOL_KEYS == {
        "size", "source", "source.wipe", "volatile.initial_source",
        "zfs.pool_name", "lvm.thinpool_name", "lvm.vg_name",
    }
