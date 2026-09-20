"""`GET /instances?project=` narrows the list to one project.

The parameter was silently dropped: FastAPI ignores a query it does not declare, so a caller
asking for one project's instances got every project's. Profiles, networks and images take
the same parameter; the instance list now does too, keyed into the cache like theirs, with
the bound and the tenant ACL still last so a project the caller may not see is an empty list.
"""

import contextvars

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import cache as cache_mod
from app.core import org_scope
from app.core.cache import CacheManager
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.models.schemas import VMInfo, VMStatus
from app.routers import instances as instances_module
from app.services.lxd_service import get_lxd_service


def _vm(name, project, host="nuc2"):
    return VMInfo(name=name, status=VMStatus.STOPPED, status_code=102, host=host, host_endpoint="",
                  project=project, type="container", architecture="x86_64")


class _Service:
    def __init__(self, *, connected=None, incomplete=None):
        self.connected = ["nuc2", "nuc1"] if connected is None else connected
        self.incomplete = [] if incomplete is None else incomplete
        self.reads = 0
        self.fleet_reads = 0
        self.scoped_reads = []

    def get_node_names(self):
        return ["nuc2", "nuc1"]

    async def list_all_vms_with_failures(self):
        self.reads += 1
        self.fleet_reads += 1
        return [
            _vm("a", "default"), _vm("b", "qa-a"), _vm("c", "qa-a"), _vm("d", "qa-b"), _vm("e", "qa-a", host="nuc1"),
        ], self.connected, self.incomplete

    async def list_vms_in_project_with_failures(self, host, project):
        self.reads += 1
        self.scoped_reads.append((host, project))
        if host not in self.connected:
            return [], [], [host]
        rows = [
            _vm("a", "default"),
            _vm("b", "qa-a"),
            _vm("c", "qa-a"),
            _vm("d", "qa-b"),
            _vm("e", "qa-a", host="nuc1"),
        ]
        return (
            [row for row in rows if row.host == host and row.project == project],
            [host],
            [host] if host in self.incomplete else [],
        )


def _client(monkeypatch, service=None):
    manager = CacheManager(redis_url=None)
    service = service or _Service()
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(instances_module, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(instances_module, "verify_cluster_access", lambda host: None)
    app = FastAPI()
    app.include_router(instances_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    return TestClient(app, raise_server_exceptions=False), manager, service


def _names(r):
    return sorted(v["name"] for v in r.json()["vms"])


def test_the_project_narrows_the_list_and_composes_with_host(monkeypatch):
    client, _, service = _client(monkeypatch)
    assert _names(client.get("/instances?project=qa-a")) == ["b", "c", "e"]
    assert _names(client.get("/instances?host=nuc2&project=qa-a")) == ["b", "c"]
    assert _names(client.get("/instances?project=zz-absent")) == []
    assert _names(client.get("/instances")) == ["a", "b", "c", "d", "e"]
    assert service.scoped_reads == [("nuc2", "qa-a")]
    assert service.fleet_reads == 3


def test_an_exact_host_and_project_never_reads_the_fleet(monkeypatch):
    client, _, service = _client(monkeypatch)

    response = client.get("/instances?host=nuc2&project=qa-a")

    assert response.status_code == 200, response.text
    assert _names(response) == ["b", "c"]
    assert service.scoped_reads == [("nuc2", "qa-a")]
    assert service.fleet_reads == 0


def test_a_malformed_project_is_refused(monkeypatch):
    client, _, service = _client(monkeypatch)
    assert client.get("/instances?project=../x").status_code == 422
    assert service.reads == 0


def test_the_project_is_part_of_the_cache_key(monkeypatch):
    client, manager, _ = _client(monkeypatch)
    client.get("/instances?project=qa-a")
    client.get("/instances?project=qa-b")
    client.get("/instances")
    keys = [k for k in manager.memory_cache.cache if "instances:list" in k]
    assert any("project=qa-a" in k for k in keys) and any("project=qa-b" in k for k in keys)
    assert any("project=*" in k for k in keys)
    assert len(keys) == 3


def test_a_project_outside_the_bound_is_an_empty_list_not_a_refusal(monkeypatch):
    client, _, service = _client(monkeypatch)

    def run():
        org_scope._visible_projects_var.set({"nuc2": {"default", "qa-a"}})
        org_scope._allowed_clusters_var.set(None)
        return client.get("/instances?host=nuc2&project=qa-b")

    r = contextvars.copy_context().run(run)
    assert r.status_code == 200 and r.json()["vms"] == []
    assert service.reads == 0, "a foreign project must be rejected before LXD I/O"


def test_a_project_outside_the_tenant_grant_is_empty_before_lxd(monkeypatch):
    client, _, service = _client(monkeypatch)

    def run():
        visible = org_scope._visible_projects_var.set({"nuc2": {"qa-a", "qa-b"}})
        acl = org_scope._tenant_acl_var.set(
            {"nuc2": {("qa-b", "*"): {"view"}}}
        )
        try:
            return client.get("/instances?host=nuc2&project=qa-a")
        finally:
            org_scope._tenant_acl_var.reset(acl)
            org_scope._visible_projects_var.reset(visible)

    response = contextvars.copy_context().run(run)

    assert response.status_code == 200
    assert response.json()["vms"] == []
    assert service.reads == 0


def test_an_instance_specific_tenant_grant_reads_then_filters_the_project(monkeypatch):
    client, _, service = _client(monkeypatch)

    def run():
        visible = org_scope._visible_projects_var.set({"nuc2": {"qa-a"}})
        acl = org_scope._tenant_acl_var.set(
            {"nuc2": {("qa-a", "b"): {"view"}}}
        )
        try:
            return client.get("/instances?host=nuc2&project=qa-a")
        finally:
            org_scope._tenant_acl_var.reset(acl)
            org_scope._visible_projects_var.reset(visible)

    response = contextvars.copy_context().run(run)

    assert response.status_code == 200, response.text
    assert _names(response) == ["b"]
    assert service.scoped_reads == [("nuc2", "qa-a")]
    assert service.fleet_reads == 0


def test_an_unreachable_selected_host_is_named_and_not_cached(monkeypatch):
    client, manager, service = _client(
        monkeypatch,
        _Service(connected=["nuc1"], incomplete=["nuc2", "nuc1"]),
    )

    first = client.get("/instances?host=nuc2&project=qa-a")
    second = client.get("/instances?host=nuc2&project=qa-a")

    assert first.status_code == 200, first.text
    assert first.json()["vms"] == []
    assert first.json()["nodes"] == []
    assert first.json()["unreachable_nodes"] == ["nuc2"]
    assert second.json()["unreachable_nodes"] == ["nuc2"]
    assert service.reads == 2, "an answer from another host must not cache the selected outage"
    assert not any("host=nuc2" in key for key in manager.memory_cache.cache)


def test_a_connected_host_with_an_empty_project_is_cacheable(monkeypatch):
    client, manager, service = _client(monkeypatch)

    first = client.get("/instances?host=nuc2&project=zz-absent")
    second = client.get("/instances?host=nuc2&project=zz-absent")

    assert first.status_code == 200, first.text
    assert first.json()["vms"] == []
    assert first.json()["nodes"] == ["nuc2"]
    assert first.json()["unreachable_nodes"] == []
    assert second.json()["unreachable_nodes"] == []
    assert service.reads == 1, "a connected host proved that this project is empty"
    assert any("instances:list:v2" in key for key in manager.memory_cache.cache)


def test_a_pre_fix_scoped_cache_entry_is_ignored(monkeypatch):
    client, manager, service = _client(monkeypatch)
    old_key = org_scope.get_scope_cache_key(
        "instances:list:host=nuc2:project=qa-a:status=None:type=None:o=0:l=0"
    )
    manager.set(
        "api_responses",
        old_key,
        {
            "total": 0,
            "vms": [],
            "nodes": [],
            "unreachable_nodes": [],
        },
        ttl=30,
    )

    response = client.get("/instances?host=nuc2&project=qa-a")

    assert response.status_code == 200, response.text
    assert _names(response) == ["b", "c"]
    assert service.reads == 1


def test_a_denied_host_never_reaches_lxd(monkeypatch):
    from fastapi import HTTPException

    client, _, service = _client(monkeypatch)

    def deny(_host):
        raise HTTPException(status_code=403, detail="Cluster access denied")

    monkeypatch.setattr(instances_module, "verify_cluster_access", deny)
    response = client.get("/instances?host=nuc2&project=qa-a")

    assert response.status_code == 403
    assert service.reads == 0
