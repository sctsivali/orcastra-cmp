"""Host-level detail is for Partners and Admins; a Tenant gets the same body without it.

A Tenant reaches an instance, not the hypervisor under it, and /nodes already withheld the LXD
endpoint from Tenants while the instance list, the instance metrics, /clusters and the storage pools
still handed it out, together with the cluster member, the host process id and the pool's backing
path.
"""

import ast
import contextvars
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import cache as cache_mod
from app.core import host_detail, org_scope
from app.core.cache import CacheManager
from app.core.dependencies import require_tenant
from app.core.org_scope import get_scope_cache_key, org_scope_dependency
from app.core.rbac import get_current_user
from app.models.instance_metrics import InstanceMetrics
from app.models.schemas import ClusterInfo, VMInfo, VMStatus
from app.routers import instances as instances_module
from app.routers import storage_pools as pools_module
from app.routers import vms as vms_module
from app.services.lxd_service import get_lxd_service

HOST = "shared-cluster"
NAME = "web-1"
ENDPOINT = "https://cluster.invalid:8443"
MEMBER = "member-2"
POOL_CONFIG = {"source": "/dev/disk/by-id/lab-disk", "size": "100GiB", "zfs.pool_name": "tank"}

TIERS = {
    "admin": (None, None),
    "partner": ({HOST}, None),
    "tenant": ({HOST}, {HOST: {("default", NAME): {"view"}}}),
}


def _vm() -> VMInfo:
    return VMInfo(
        name=NAME, status=VMStatus.RUNNING, status_code=103, host=HOST, host_endpoint=ENDPOINT,
        location=MEMBER, project="default", type="container", architecture="x86_64",
    )


def _as(tier, call):
    allowed, acl = TIERS[tier]

    def run():
        org_scope._allowed_clusters_var.set(allowed)
        org_scope._tenant_acl_var.set(acl)
        org_scope._visible_projects_var.set(None if allowed is None else {HOST: {"default"}})
        return call()

    return contextvars.copy_context().run(run)


class _Service:
    def get_node_names(self):
        return [HOST]

    async def list_all_vms_with_failures(self):
        return [_vm()], [HOST], []

    async def get_instance_metrics(self, *args, **kwargs):
        return InstanceMetrics(instance_name=NAME, host=HOST, status=VMStatus.RUNNING, pid=4242)


def _client(monkeypatch, *routers):
    manager = CacheManager(redis_url=None)
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)
    for module in (instances_module, vms_module, pools_module):
        monkeypatch.setattr(module, "get_cache_manager", lambda: manager, raising=False)
    for module in (instances_module, vms_module):
        monkeypatch.setattr(module, "verify_cluster_access", lambda host: None)
        monkeypatch.setattr(
            module, "verify_instance_view_access", lambda host, project, name: project or "default"
        )
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_lxd_service] = lambda: _Service()
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    return TestClient(app, raise_server_exceptions=False), manager


@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_the_policy_returns_copies_and_never_mutates(tier):
    vm, metrics = _vm(), InstanceMetrics(instance_name=NAME, host=HOST, status=VMStatus.RUNNING, pid=7)

    shown_vm = host_detail.vm_for_tier(vm, tier)
    shown_metrics = host_detail.metrics_for_tier(metrics, tier)

    assert (vm.host_endpoint, vm.location, metrics.pid) == (ENDPOINT, MEMBER, 7), "input untouched"
    if tier == "tenant":
        assert (shown_vm.host_endpoint, shown_vm.location, shown_metrics.pid) == (None, None, None)
        assert shown_vm is not vm and shown_metrics is not metrics
    else:
        assert shown_vm is vm and shown_metrics is metrics


def test_a_tenant_pool_keeps_only_its_size_settings():
    class Pool(host_detail.BaseModel):
        name: str
        config: dict

    pool = Pool(name="p", config=dict(POOL_CONFIG))

    shown = host_detail.pool_for_tier(pool, "tenant")

    assert shown.config == {"size": "100GiB"}
    assert pool.config == POOL_CONFIG
    assert host_detail.pool_for_tier(pool, "partner") is pool


@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_the_instance_list_hides_host_detail_from_a_tenant_only(monkeypatch, tier):
    client, manager = _client(monkeypatch, instances_module.router)

    fresh = _as(tier, lambda: client.get("/instances"))
    cached = _as(tier, lambda: client.get("/instances"))

    for response in (fresh, cached):
        assert response.status_code == 200, response.text[:200]
        (row,) = response.json()["vms"]
        expected = (None, None) if tier == "tenant" else (ENDPOINT, MEMBER)
        assert (row["host_endpoint"], row["location"]) == expected
    stored = [value for key, (value, _) in manager.memory_cache.cache.items() if "instances:list" in key]
    assert stored and stored[0]["vms"][0]["host_endpoint"] == ENDPOINT, "the cache keeps the full body"


@pytest.mark.parametrize(
    "path",
    [f"/instances/{NAME}/metrics?host={HOST}&project=default", f"/vms/{HOST}/{NAME}/metrics?project=default"],
)
@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_instance_metrics_carry_the_host_pid_for_partner_and_admin_only(monkeypatch, tier, path):
    client, _ = _client(monkeypatch, instances_module.router, vms_module.router)

    response = _as(tier, lambda: client.get(path))

    assert response.status_code == 200, response.text[:200]
    assert response.json()["pid"] == (None if tier == "tenant" else 4242)


@pytest.mark.parametrize("tier", ["partner", "tenant"])
def test_a_cached_cluster_list_reaches_a_tenant_without_the_endpoint(monkeypatch, tier):
    from app.routers import cluster_registration
    from app.routers.cluster_registration import get_cert_manager

    manager = CacheManager(redis_url=None)
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)
    app = FastAPI()
    app.include_router(cluster_registration.router)
    app.dependency_overrides[get_cert_manager] = lambda: object()
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)
    body = {
        "clusters": [
            ClusterInfo(cluster_id=HOST, display_name=HOST, endpoint=ENDPOINT, status="online").model_dump()
        ],
        "total": 1,
    }

    def read():
        manager.set("api_responses", get_scope_cache_key("clusters:list:all"), body, ttl=60)
        return client.get("/clusters")

    response = _as(tier, read)

    assert response.status_code == 200, response.text[:200]
    assert response.json()["clusters"][0]["endpoint"] == (None if tier == "tenant" else ENDPOINT)
    assert body["clusters"][0]["endpoint"] == ENDPOINT


@pytest.mark.parametrize("tier", ["partner", "tenant"])
def test_a_cached_pool_list_reaches_a_tenant_without_its_backing_path(monkeypatch, tier):
    client, manager = _client(monkeypatch, pools_module.router)
    body = {
        "total": 1,
        "pools": [{"name": "default", "driver": "zfs", "config": dict(POOL_CONFIG), "used_by": [], "node": HOST}],
        "nodes": [HOST],
    }

    def read():
        manager.set("api_responses", get_scope_cache_key("storage_pools:list:host=None"), body, ttl=60)
        return client.get("/storage/pools")

    response = _as(tier, read)

    assert response.status_code == 200, response.text[:200]
    config = response.json()["pools"][0]["config"]
    assert config == ({"size": "100GiB"} if tier == "tenant" else POOL_CONFIG)


HOST_DETAIL_MODELS = {
    "VMInfo", "VMListResponse", "InstanceMetrics", "ClusterInfo", "ClusterListResponse",
    "StoragePoolInfo", "StoragePoolListResponse",
}


def _response_model(decorator: ast.expr) -> str:
    if isinstance(decorator, ast.Call):
        for keyword in decorator.keywords:
            if keyword.arg == "response_model" and isinstance(keyword.value, ast.Name):
                return keyword.value.id
    return ""


def test_every_route_returning_host_detail_applies_the_tier_policy():
    routers = Path(__file__).resolve().parents[1] / "app" / "routers"
    checked = []
    for path in sorted(routers.glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            models = {_response_model(d) for d in node.decorator_list} & HOST_DETAIL_MODELS
            if not models:
                continue
            checked.append(f"{path.name}:{node.name}")
            calls = {
                getattr(call.func, "id", "")
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            assert any(name.endswith("_for_tier") for name in calls), (
                f"{path.name}:{node.name} returns {models} without the host-detail tier policy"
            )
            assert "org_scope_dependency" in source.split("def ", 1)[0] or (
                "Depends(org_scope_dependency)" in source
            ), f"{path.name} must run org_scope_dependency, or an unset scope reads as Admin"
    assert len(checked) == 10, checked
