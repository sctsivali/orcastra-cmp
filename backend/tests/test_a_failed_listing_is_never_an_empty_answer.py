"""A cluster whose listing failed is named, never reported as a cluster with nothing.

Seen on the first production deployment on a real MicroCloud: Storage Pools, Volumes, ISO
Images, Identities and Groups rendered zero rows for a cluster holding dozens of each, with
the banner naming only the one host that was genuinely offline. Every helper caught
`Exception`, returned `[]`, the handler counted the cluster as answered, and pools and
volumes then cached that emptiness for 180 seconds. The only failure that ever reached the
banner was "no client at all".

The rule: a listing that failed puts the cluster in `unreachable_nodes` with a reason in
`node_errors`; a listing that lost some rows puts it in `partial_nodes`; nothing degraded is
cached; Tenants get the lists but not the reasons.

Hermetic: a fake pylxd client whose API node answers or raises per path, the real routers,
the real fan-out code, no Redis, no network.
"""

from typing import Any, Callable, Dict, Optional

import pytest
import requests
import urllib3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pylxd.exceptions import LXDAPIException

from app.core.cache_envelope import is_envelope
from app.services.lxd import fanout
from app.services.lxd.exceptions import (
    LXDAuthorizationError,
    LXDConnectionError,
    LXDLocalStorageError,
)
from app.services.lxd.lxd_service import LXDService

HOST = "C2"


class _Resp:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body
        self.content = str(body).encode()

    def json(self):
        return self._body


def _lxd_error(status: int, text: str) -> LXDAPIException:
    return LXDAPIException(_Resp(status, {"error": text, "type": "error", "error_code": status}))


class _APINode:
    """Enough of pylxd's _APINode: attribute and item access build a path, get() answers."""

    def __init__(self, path: str, handler: Callable[[str, Optional[dict]], Any]):
        self._path = path
        self._handler = handler

    def __getattr__(self, name: str) -> "_APINode":
        if name.startswith("_"):
            raise AttributeError(name)
        return _APINode(f"{self._path}/{name.replace('_', '-')}", self._handler)

    def __getitem__(self, item: str) -> "_APINode":
        return _APINode(f"{self._path}/{item}", self._handler)

    def get(self, params=None, timeout=None):
        return self._handler(self._path, params)


class _Client:
    project = None

    def __init__(self, handler, extensions=("storage_volumes_all_projects", "network_acls_all_projects", "warnings")):
        self.api = _APINode("/1.0", handler)
        self.host_info = {
            "api_version": "1.0",
            "auth": "trusted",
            "config": {},
            "environment": {"server_clustered": True, "server_name": "m1"},
            "api_extensions": list(extensions),
            "public": False,
        }


class _Manager:
    def __init__(self, client, attempted=()):
        self._client = client
        self.attempted = set(attempted)

    def has_attempted(self, host):
        return host in self.attempted

    def get_client(self, host, *args, **kwargs):
        if isinstance(self._client, BaseException):
            raise self._client
        return self._client

    def get_node_names(self):
        return [HOST]

    def sync_registered_clusters(self, *args, **kwargs):
        return None


class _CacheSpy:
    def __init__(self):
        self.sets = []

    def get(self, namespace, key):
        return None

    def set(self, namespace, key, value, ttl=None):
        # The soft TTL: a stale-while-revalidate entry is kept longer than it is fresh.
        self.sets.append((namespace, key, value.get("s", ttl) if is_envelope(value) else ttl))


def _service(client) -> LXDService:
    svc = LXDService.__new__(LXDService)
    svc.connection_manager = _Manager(client)
    svc.cert_manager = None
    svc.get_client_with_reconnect = lambda host: svc.connection_manager.get_client(host)  # type: ignore[attr-defined]
    svc.get_node_names = lambda: [HOST]  # type: ignore[attr-defined]
    return svc


def _app(module, client, monkeypatch, cache: Optional[_CacheSpy] = None) -> TestClient:
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    if cache is not None and hasattr(module, "get_cache_manager"):
        monkeypatch.setattr(module, "get_cache_manager", lambda: cache)
    app = FastAPI()
    app.include_router(module.router if hasattr(module, "router") else module.identities_router)
    app.dependency_overrides[get_lxd_service] = lambda: _service(client)
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _always_400(path, params):
    raise _lxd_error(400, "More than one cluster member has a volume named backups")


# --- classify() -----------------------------------------------------------------------------

def test_classify_names_the_kind_and_never_the_host():
    read_timeout = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(
            None, "https://cluster.example:443/1.0/x",
            reason=urllib3.exceptions.ReadTimeoutError(None, "https://cluster.example:443/1.0/x", "Read timed out"),
        )
    )
    cases = [
        (LXDAuthorizationError("cluster.example refused"), fanout.KIND_FORBIDDEN),
        (LXDConnectionError("Could not connect to cluster.example"), fanout.KIND_UNREACHABLE),
        (LXDLocalStorageError("/tmp full"), fanout.KIND_FAILED),
        (_lxd_error(403, "not authorized"), fanout.KIND_FORBIDDEN),
        (_lxd_error(400, "More than one cluster member has a volume named backups"), fanout.KIND_FAILED),
        (read_timeout, fanout.KIND_UNREACHABLE),
        (requests.exceptions.ReadTimeout("https://cluster.example:443 timed out"), fanout.KIND_UNREACHABLE),
        (KeyError("metadata"), fanout.KIND_FAILED),
    ]
    for exc, expected_kind in cases:
        kind, reason = fanout.classify(exc)
        assert kind == expected_kind, f"{type(exc).__name__}: {kind} != {expected_kind}"
        assert "cluster.example" not in reason and "example" not in reason, f"reason leaks the host: {reason!r}"
        assert reason, f"{type(exc).__name__} has an empty reason"
    assert fanout.classify(read_timeout)[1] == "read timed out"
    assert fanout.classify(_lxd_error(400, "x" * 500))[1].endswith("x" * 50)
    assert len(fanout.classify(_lxd_error(400, "x" * 500))[1]) < 230


def test_a_partial_read_keeps_its_rows_and_names_what_was_lost():
    read = fanout.NodeRead(rows=[1, 2])
    read.lost("pool local: LXD answered 400: target member")
    fan = fanout.collect([HOST], [read], noun="volumes")
    assert fan.rows == [1, 2]
    assert fan.partial_nodes == [HOST]
    assert fan.unreachable_nodes == []
    assert fan.node_errors[HOST].startswith("partial: pool local")
    assert fan.cacheable is False


def test_a_tenant_gets_the_lists_but_not_the_reasons():
    fan = fanout.collect([HOST], [_lxd_error(403, "not authorized")], noun="pools", reasons_visible=False)
    assert fan.unreachable_nodes == [HOST]
    assert fan.node_errors == {}


def test_only_a_complete_fanout_is_cacheable():
    assert fanout.collect([HOST], [fanout.NodeRead(rows=[1])], noun="x").cacheable is True
    assert fanout.collect([HOST], [_lxd_error(500, "boom")], noun="x").cacheable is False
    assert fanout.collect([], [], noun="x").cacheable is False


# --- the seven routers, driven for real ----------------------------------------------------

ROUTERS = [
    ("storage_pools", "/storage/pools", "pools"),
    ("storage_volumes", "/storage/volumes", "volumes"),
    ("lxd_warnings", "/warnings", "warnings"),
    ("network_acls", "/network-acls", "acls"),
    ("permissions", "/identities", "identities"),
]


@pytest.mark.parametrize("module_name,path,rows_key", ROUTERS)
def test_a_cluster_whose_listing_fails_is_named_not_emptied(module_name, path, rows_key, monkeypatch):
    module = __import__(f"app.routers.{module_name}", fromlist=["router"])
    cache = _CacheSpy()
    client = _Client(_always_400)
    app = _app(module, client, monkeypatch, cache)
    response = app.get(path)
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body[rows_key] == []
    assert body["unreachable_nodes"] == [HOST], body
    assert body["node_errors"][HOST].startswith("error: LXD answered 400"), body["node_errors"]
    assert "backups" in body["node_errors"][HOST]
    assert body.get("partial_nodes", []) == []
    assert cache.sets == [], "a failed listing must never be cached as the answer"


def test_groups_route_names_a_refused_cluster(monkeypatch):
    from app.routers import permissions
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    def refuse(path, params):
        raise _lxd_error(403, "not authorized")

    app = FastAPI()
    app.include_router(permissions.groups_router)
    app.dependency_overrides[get_lxd_service] = lambda: _service(_Client(refuse))
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    response = TestClient(app, raise_server_exceptions=False).get("/groups")
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["groups"] == []
    assert body["unreachable_nodes"] == [HOST]
    assert body["node_errors"][HOST].startswith("forbidden: LXD answered 403")


def test_server_route_names_a_cluster_whose_credentials_could_not_be_prepared(monkeypatch):
    from app.routers import lxd_server

    app = _app(lxd_server, LXDLocalStorageError("/tmp is full"), monkeypatch)
    response = app.get("/server")
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["servers"] == []
    assert body["unreachable_nodes"] == [HOST]
    assert body["node_errors"][HOST].startswith("error: backend could not prepare TLS")


def test_server_route_answers_for_an_identity_whose_config_is_null(monkeypatch):
    """LXD renders `config: null` for an identity without can_edit. That is an answer."""
    from app.routers import lxd_server

    client = _Client(lambda path, params: _Resp(200, {"metadata": []}))
    client.host_info["config"] = None
    app = _app(lxd_server, client, monkeypatch)
    body = app.get("/server").json()
    assert [s["node"] for s in body["servers"]] == [HOST]
    assert body["servers"][0]["config"] == {}
    assert body["unreachable_nodes"] == []


def test_one_malformed_row_is_a_partial_read_not_an_empty_one(monkeypatch):
    from app.routers import storage_pools

    def two_pools_one_broken(path, params):
        assert path == "/1.0/storage-pools" and params == {"recursion": 1}
        return _Resp(200, {"metadata": [
            {"name": "local", "driver": "zfs", "status": "Created", "config": {}, "used_by": []},
            {"name": "remote", "driver": "ceph", "status": "Created", "config": {"a": ["not", "a", "string"]}, "used_by": []},
        ]})

    cache = _CacheSpy()
    app = _app(storage_pools, _Client(two_pools_one_broken), monkeypatch, cache)
    body = app.get("/storage/pools").json()
    assert [p["name"] for p in body["pools"]] == ["local"]
    assert body["partial_nodes"] == [HOST]
    assert body["unreachable_nodes"] == []
    assert body["node_errors"][HOST].startswith("partial: pool remote")
    assert cache.sets == []


def test_a_complete_listing_is_cached_and_carries_no_errors(monkeypatch):
    from app.routers import storage_pools

    def pools(path, params):
        return _Resp(200, {"metadata": [
            {"name": "local", "driver": "zfs", "status": "Created", "config": {}, "used_by": []},
        ]})

    cache = _CacheSpy()
    app = _app(storage_pools, _Client(pools), monkeypatch, cache)
    body = app.get("/storage/pools").json()
    assert [p["name"] for p in body["pools"]] == ["local"]
    assert body["unreachable_nodes"] == [] and body["partial_nodes"] == [] and body["node_errors"] == {}
    assert [ns for ns, _, _ in cache.sets] == ["api_responses"]


# --- the seeded set and the project bound ---------------------------------------------------

def _seeded_app(module, attempted):
    """A fleet of HOST (answers) and C3 (this worker holds no client for it)."""
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    def pools(path, params):
        return _Resp(200, {"metadata": [{"name": "local", "driver": "zfs", "status": "Created", "config": {}, "used_by": []}]})

    svc = _service(_Client(lambda p, q: _Resp(200, {"metadata": []}) if "volumes" in p or "storage-pools" == p.rsplit("/", 1)[-1] and q.get("recursion") == 0 else pools(p, q)))
    svc.connection_manager.attempted = set(attempted)
    svc.get_node_names = lambda: [HOST, "C3"]  # type: ignore[attr-defined]
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_a_cluster_this_worker_has_not_tried_yet_blocks_the_cache(monkeypatch):
    """One worker's missing client must not become every worker's answer for 180 s.

    The cache is shared by the workers. A cluster registered after this worker booted is
    named unreachable by the seeding until the next healer tick connects it; a body written
    meanwhile would tell every other worker's users the cluster is down.
    """
    from app.routers import storage_pools, storage_volumes

    for module, path in ((storage_pools, "/storage/pools"), (storage_volumes, "/storage/volumes")):
        cache = _CacheSpy()
        monkeypatch.setattr(module, "get_cache_manager", lambda: cache)
        client = _seeded_app(module, attempted=[HOST])
        body = client.get(path).json()
        assert body["unreachable_nodes"] == ["C3"], body
        assert cache.sets == [], f"{path}: a body naming a never-tried cluster unreachable was cached"


def test_a_cluster_this_worker_tried_and_found_down_is_cached_for_one_healer_cadence(monkeypatch):
    """A fleet with a dead cluster is the normal case (production runs 2 of 5 connected).

    Refusing to cache whenever any cluster is unreachable made every page open a live read
    of every cluster that did answer, and five concurrent readers queued to ten seconds.
    A body naming a cluster this worker tried is a correct answer; it is remembered for 60 s
    rather than 180 so a recovery shows within the healer's cadence.
    """
    from app.routers import storage_pools, storage_volumes

    for module, path in ((storage_pools, "/storage/pools"), (storage_volumes, "/storage/volumes")):
        cache = _CacheSpy()
        monkeypatch.setattr(module, "get_cache_manager", lambda: cache)
        client = _seeded_app(module, attempted=[HOST, "C3"])
        body = client.get(path).json()
        assert body["unreachable_nodes"] == ["C3"], body
        assert [ttl for _, _, ttl in cache.sets] == [module.DEGRADED_CACHE_TTL], f"{path}: {cache.sets}"
        assert module.DEGRADED_CACHE_TTL < 180


def test_volumes_and_warnings_hold_a_partner_to_the_project_bound(monkeypatch):
    """On a cluster bound to two organizations a Partner sees default plus their own projects."""
    import contextvars

    from app.core import org_scope
    from app.routers import lxd_warnings, storage_volumes

    def cluster(path, params):
        if path == "/1.0/storage-pools":
            return _Resp(200, {"metadata": ["/1.0/storage-pools/local"]})
        if path == "/1.0/storage-pools/local/volumes":
            return _Resp(200, {"metadata": [
                {"name": "mine", "type": "custom", "content_type": "filesystem", "project": "sivali-prod", "location": "none", "config": {}, "used_by": []},
                {"name": "theirs", "type": "custom", "content_type": "filesystem", "project": "other-org", "location": "none", "config": {}, "used_by": []},
                {"name": "shared", "type": "custom", "content_type": "filesystem", "project": "default", "location": "none", "config": {}, "used_by": []},
            ]})
        if path == "/1.0/warnings":
            return _Resp(200, {"metadata": [
                {"uuid": "w-mine", "project": "sivali-prod", "location": "m1", "type": "t", "count": 1},
                {"uuid": "w-theirs", "project": "other-org", "location": "m1", "type": "t", "count": 1},
                {"uuid": "w-default", "project": "default", "location": "m1", "type": "t", "count": 1},
            ]})
        raise AssertionError(path)

    def run():
        org_scope._visible_projects_var.set({HOST: {"default", "sivali-prod"}})
        out = {}
        for module, path, key, name in ((storage_volumes, "/storage/volumes", "volumes", "name"), (lxd_warnings, "/warnings", "warnings", "uuid")):
            app = _app(module, _Client(cluster, extensions=["storage_volumes_all_projects", "warnings"]), monkeypatch, _CacheSpy())
            body = app.get(path).json()
            out[key] = sorted(r[name] for r in body[key])
        return out

    out = contextvars.copy_context().run(run)
    assert out["volumes"] == ["mine", "shared"], out
    assert out["warnings"] == ["w-default", "w-mine"], out
