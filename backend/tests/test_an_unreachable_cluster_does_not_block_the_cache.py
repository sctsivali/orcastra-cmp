"""A cluster that cannot be reached at all must not stop the rest of the fleet being cached.

Measured on dev: every read of the storage volume list paid the full 25 s read budget, because
one cluster's listing timed out and `Fanout.cacheable` refused any fan-out with a failure in it.
A transport failure leaves no rows to misread and the body names the cluster, so the pool,
volume and IPAM lists remember it for their short degraded TTL. An LXD error answer or a
partial read is still never cached (test_a_failed_listing_is_never_an_empty_answer.py).
"""

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd import fanout
from tests.test_a_failed_listing_is_never_an_empty_answer import (
    HOST,
    _CacheSpy,
    _Client,
    _lxd_error,
    _Resp,
    _service,
)

DOWN = "C3"
LISTS = [
    ("storage_pools", "/storage/pools"),
    ("storage_volumes", "/storage/volumes"),
    ("ipam", "/ipam"),
]


def _empty(path, params):
    return _Resp(200, {"metadata": []})


def _timeout(path, params):
    raise requests.exceptions.ReadTimeout("read timed out")


def _refused_by_lxd(path, params):
    raise _lxd_error(400, "More than one cluster member has a volume named backups")


def _two_cluster_app(module, down_handler, monkeypatch, cache):
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.services.lxd_service import get_lxd_service

    clients = {HOST: _Client(_empty), DOWN: _Client(down_handler)}
    svc = _service(clients[HOST])
    svc.connection_manager.get_client = lambda host, *a, **k: clients[host]
    svc.connection_manager.get_node_names = lambda: [HOST, DOWN]
    svc.connection_manager.attempted = {HOST, DOWN}
    svc.get_client_with_reconnect = lambda host: clients[host]  # type: ignore[attr-defined]
    svc.get_node_names = lambda: [HOST, DOWN]  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "get_cache_manager", lambda: cache)
    if module.__name__.endswith(".ipam"):
        # IPAM joins several daemon endpoints through pylxd internals the fake client does not
        # model; its per-cluster helper is replaced, the handler's cache decision is not.
        def helper(service, node_name, network_filter):
            handler = _empty if node_name == HOST else down_handler
            handler("/1.0/networks", None)
            return fanout.NodeRead()

        monkeypatch.setattr(module, "_fetch_ipam_for_node", helper)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("module_name,path", LISTS)
def test_a_timed_out_cluster_is_named_and_the_rest_is_cached_briefly(module_name, path, monkeypatch):
    module = __import__(f"app.routers.{module_name}", fromlist=["router"])
    cache = _CacheSpy()
    body = _two_cluster_app(module, _timeout, monkeypatch, cache).get(path).json()
    assert body["unreachable_nodes"] == [DOWN], body
    assert body["node_errors"][DOWN] == "unreachable: read timed out"
    assert [ttl for _, _, ttl in cache.sets] == [module.DEGRADED_CACHE_TTL], cache.sets


@pytest.mark.parametrize("module_name,path", LISTS)
def test_an_lxd_error_answer_still_blocks_the_cache(module_name, path, monkeypatch):
    module = __import__(f"app.routers.{module_name}", fromlist=["router"])
    cache = _CacheSpy()
    body = _two_cluster_app(module, _refused_by_lxd, monkeypatch, cache).get(path).json()
    assert body["unreachable_nodes"] == [DOWN], body
    assert body["node_errors"][DOWN].startswith("error: LXD answered 400")
    assert cache.sets == [], "an LXD error looks like an answer while missing rows"


def test_the_rule_reads_the_kind_even_when_a_tenant_gets_no_reasons():
    fan = fanout.collect(
        [HOST, DOWN],
        [fanout.NodeRead(rows=[1]), requests.exceptions.ConnectTimeout("x")],
        noun="x",
        reasons_visible=False,
    )
    assert fan.node_errors == {}
    assert fan.cacheable is False
    assert fan.cacheable_despite_outages is True


def test_nobody_answering_or_a_partial_read_is_never_cacheable():
    nobody = fanout.collect([DOWN], [requests.exceptions.ConnectTimeout("x")], noun="x")
    assert nobody.cacheable_despite_outages is False

    partial = fanout.NodeRead(rows=[1])
    partial.lost("pool local: LXD answered 400")
    mixed = fanout.collect([HOST, DOWN], [partial, requests.exceptions.ConnectTimeout("x")], noun="x")
    assert mixed.cacheable_despite_outages is False

    odd = fanout.collect([HOST, DOWN], [fanout.NodeRead(rows=[1]), "not a read"], noun="x")
    assert odd.cacheable_despite_outages is False
