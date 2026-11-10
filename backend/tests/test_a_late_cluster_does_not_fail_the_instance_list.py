"""One cluster past the inventory wall must not turn the whole instance list into a 503.

When one cluster's instance list arrives past the 30 s wall, `GET /instances` answers with the
clusters that did answer, names the late one in `unreachable_nodes`, says why in `node_errors`
(Admin and Partner only), and caches that partial answer for 10 s instead of 30.

The complete-inventory contract stays for the callers that need it
(test_instance_reads_are_bounded.py).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import cache as cache_mod
from app.core import org_scope
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.cache import CacheManager
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.models.schemas import VMInfo, VMStatus
from app.routers import instances as instances_module
from app.services.lxd import instance as instance_module
from app.services.lxd.instance import InstanceManager
from app.services.lxd.lxd_service import LXDService
from app.services.lxd_service import get_lxd_service
from tests.test_a_failed_listing_is_never_an_empty_answer import _CacheSpy

FAST, LATE = "nuc2", "central-1"


class _ListCacheSpy(_CacheSpy):
    """The pool list spy, plus the generation the instance list fences its write-back with."""

    def instance_list_generation(self):
        return 0


def _vm(name, host):
    return VMInfo(name=name, status=VMStatus.STOPPED, status_code=102, host=host, host_endpoint="",
                  project="default", type="container", architecture="x86_64")


def _service(monkeypatch, late_error):
    manager = InstanceManager.__new__(InstanceManager)
    manager.cert_manager = type("Certs", (), {"list_clusters": staticmethod(lambda: [FAST, LATE])})()

    async def _bounded(fn, *args, **kwargs):
        if kwargs["label"] == "instance.inventory.registry":
            return fn(*args)
        if args == (FAST,):
            return ([_vm("web-1", FAST), _vm("web-2", FAST)], True, True)
        raise late_error

    monkeypatch.setattr(instance_module, "run_blocking", _bounded)
    svc = LXDService.__new__(LXDService)
    svc.instance_manager = manager
    svc.get_node_names = lambda: [FAST, LATE]  # type: ignore[attr-defined]
    return svc


def _client(monkeypatch, svc, cache):
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(instances_module, "get_cache_manager", lambda: cache)
    app = FastAPI()
    app.include_router(instances_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _both_clusters_in_scope():
    org_scope.set_allowed_clusters({FAST, LATE})
    yield
    org_scope.set_allowed_clusters(None)


def test_a_late_cluster_is_named_and_the_rest_of_the_fleet_answers(monkeypatch):
    svc = _service(monkeypatch, BlockingTimeout(f"instance.inventory[{LATE}]", 30.0))
    response = _client(monkeypatch, svc, CacheManager(redis_url=None)).get("/instances")

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(vm["name"] for vm in body["vms"]) == ["web-1", "web-2"]
    assert body["nodes"] == [FAST]
    assert body["unreachable_nodes"] == [LATE]
    assert body["node_errors"] == {LATE: "unreachable: did not answer within 30s"}


def test_a_full_inventory_pool_is_not_blamed_on_the_cluster(monkeypatch):
    svc = _service(monkeypatch, BlockingPoolFull(f"instance.inventory[{LATE}]", 20))
    body = _client(monkeypatch, svc, CacheManager(redis_url=None)).get("/instances").json()

    assert body["unreachable_nodes"] == [LATE]
    assert body["node_errors"] == {
        LATE: "unreachable: not read, every inventory reader on this server was busy"
    }


def test_the_partial_answer_is_kept_for_ten_seconds_and_a_complete_one_for_thirty(monkeypatch):
    late = _ListCacheSpy()
    svc = _service(monkeypatch, BlockingTimeout(f"instance.inventory[{LATE}]", 30.0))
    _client(monkeypatch, svc, late).get("/instances")
    assert [ttl for namespace, _key, ttl in late.sets if namespace == "api_responses"] == [10]

    complete = _ListCacheSpy()

    async def _all_answer(fn, *args, **kwargs):
        if kwargs["label"] == "instance.inventory.registry":
            return fn(*args)
        return ([_vm("x", args[0])], True, True)

    monkeypatch.setattr(instance_module, "run_blocking", _all_answer)
    _client(monkeypatch, svc, complete).get("/instances")
    assert [ttl for namespace, _key, ttl in complete.sets if namespace == "api_responses"] == [30]


def test_a_tenant_sees_the_late_cluster_without_the_reason(monkeypatch):
    svc = _service(monkeypatch, BlockingTimeout(f"instance.inventory[{LATE}]", 30.0))
    monkeypatch.setattr("app.services.lxd.fanout.tenant_acl_active", lambda: True)
    monkeypatch.setattr(instances_module, "check_tenant_permission", lambda *a, **k: True)
    body = _client(monkeypatch, svc, CacheManager(redis_url=None)).get("/instances").json()

    assert body["unreachable_nodes"] == [LATE]
    assert body["node_errors"] == {}


def test_the_late_cluster_picked_alone_is_unreachable_not_empty(monkeypatch):
    svc = _service(monkeypatch, BlockingTimeout(f"instance.inventory[{LATE}]", 30.0))
    monkeypatch.setattr(instances_module, "verify_cluster_access", lambda host: None)
    body = _client(monkeypatch, svc, CacheManager(redis_url=None)).get(f"/instances?host={LATE}").json()

    assert body["vms"] == []
    assert body["unreachable_nodes"] == [LATE]
    assert body["node_errors"] == {LATE: "unreachable: did not answer within 30s"}
