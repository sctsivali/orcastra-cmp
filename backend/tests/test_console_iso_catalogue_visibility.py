"""ISO discovery follows the same Tenant usage filter as storage volumes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.routers import console
from app.services.console_service import ConsoleService


class ApiNode:
    def __init__(self, reads, path="", *, fail_pool=False):
        self.reads, self.path, self.fail_pool = reads, path, fail_pool

    def __getitem__(self, segment):
        return ApiNode(self.reads, f"{self.path}/{segment}", fail_pool=self.fail_pool)

    @property
    def storage_pools(self):
        return self["storage-pools"]

    @property
    def projects(self):
        return self["projects"]

    def get(self, **kwargs):
        self.reads.append((self.path, kwargs))
        if self.path == "/storage-pools":
            rows = ["/1.0/storage-pools/local"]
        elif self.path.endswith("/volumes"):
            if self.fail_pool:
                raise RuntimeError("synthetic pool unavailable")
            rows = [
                {"name": "own.iso", "type": "custom", "content_type": "iso", "config": {"size": "1GiB"}, "used_by": ["/1.0/instances/mine?project=alpha"]},
                {"name": "other.iso", "type": "custom", "content_type": "iso", "used_by": ["/1.0/instances/neighbour?project=alpha"]},
                {"name": "unused.iso", "type": "custom", "content_type": "iso", "used_by": []},
                {"name": "regular", "type": "custom", "content_type": "block", "used_by": []},
            ]
        elif self.path == "/projects":
            rows = [{"name": "alpha", "config": {"features.storage.volumes": "true"}}]
        else:
            raise AssertionError(self.path)
        return SimpleNamespace(json=lambda: {"metadata": rows})


@pytest.fixture
def scope():
    cluster = org_scope._allowed_clusters_var.set({"cluster-a"})
    projects = org_scope._visible_projects_var.set({"cluster-a": {"default", "alpha"}})
    acl = org_scope._tenant_acl_var.set(None)
    yield
    org_scope._tenant_acl_var.reset(acl)
    org_scope._visible_projects_var.reset(projects)
    org_scope._allowed_clusters_var.reset(cluster)


def service(*, fail_pool=False):
    reads = []
    client = SimpleNamespace(api=ApiNode(reads, fail_pool=fail_pool))
    result = ConsoleService.__new__(ConsoleService)
    result._lxd_service = SimpleNamespace(ensure_connection=lambda host: True, get_client_with_reconnect=lambda host: client)
    return result, reads


def test_partner_keeps_shared_default_catalogue(scope):
    discovery, reads = service()
    rows, error = discovery.get_available_isos("cluster-a")
    assert error is None
    assert [row.name for row in rows] == ["own.iso", "other.iso", "unused.iso"]
    assert rows[0].size == 1024**3
    assert reads[-1][1]["params"]["project"] == "default"


def test_tenant_only_sees_iso_used_by_a_view_granted_workload(scope):
    org_scope._tenant_acl_var.set({"cluster-a": {("alpha", "mine"): {"view"}}})
    discovery, reads = service()
    rows, error = discovery.get_available_isos("cluster-a", "alpha")
    assert error is None
    assert [row.name for row in rows] == ["own.iso"]
    assert reads[-1][1]["params"]["project"] == "alpha"


def test_revoked_tenant_grant_cannot_see_catalogue(scope):
    org_scope._tenant_acl_var.set({})
    discovery, _reads = service()
    rows, error = discovery.get_available_isos("cluster-a")
    assert rows == [] and error is None


def test_discovery_failure_is_not_an_empty_success(scope):
    discovery, _reads = service(fail_pool=True)
    rows, error = discovery.get_available_isos("cluster-a")
    assert rows == [] and error == "ISO discovery was incomplete. Retry the request."


@pytest.mark.asyncio
async def test_project_denial_precedes_service_access(scope, monkeypatch):
    monkeypatch.setattr(console, "get_console_service", lambda: pytest.fail("service accessed before scope gate"))
    with pytest.raises(HTTPException) as caught:
        await console.list_isos(host="cluster-a", project="neighbour", _rbac=None)
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_iso_discovery_uses_bounded_worker_and_passes_normalized_project(scope, monkeypatch):
    discovery, _reads = service()
    calls = []
    monkeypatch.setattr(console, "get_console_service", lambda: discovery)

    async def offload(call, *, label):
        calls.append(label)
        return call()

    monkeypatch.setattr(console, "_console_io", offload)
    result = await console.list_isos(host="cluster-a", project="alpha", _rbac=None)
    assert result.success is True and len(result.isos) == 3
    assert calls == ["console.iso.discovery"]
