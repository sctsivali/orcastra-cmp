"""A cluster that did not answer must be named, not quietly left out of the list.

Drives the real handler (test_fresh_bypass_is_wired.py is the structural scan). A helper that
raises LXDConnectionError for a missing client and catches its own raise in an outer
`except Exception` returns an empty list, and the router counts the node as answered. The stubs
hold the discriminating state: a cluster the connection manager lists while `get_client` returns
None, as for a breaker-tripped host. A cluster with no cached client never enters
`nodes_to_query`, and the never-queried seeding would name it either way. Hermetic.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd.lxd_service import LXDService
from app.services.lxd.projects import ProjectManager
from app.services.lxd.instance import InstanceManager
from app.services.lxd.resources import ResourceManager


HOST = "blitz01"


class _BreakerTrippedConnectionManager:
    """Lists the cluster and refuses to hand out a client, as the tripped breaker does."""

    def get_client(self, host, *args, **kwargs):
        return None

    def get_node_names(self):
        return [HOST]

    def connect_to_node(self, *args, **kwargs):
        return None

    def sync_registered_clusters(self, *args, **kwargs):
        return None


class _OneRegisteredCluster:
    """Vault's view: the same single cluster the connection manager lists.

    important: keep this identical to nodes_to_query. The never-queried seeding is
    `registered - queried`, so here it is empty and cannot mask the defect.
    """

    def list_clusters(self):
        return [HOST]


def _service() -> LXDService:
    svc = LXDService.__new__(LXDService)
    conn = _BreakerTrippedConnectionManager()
    svc.connection_manager = conn
    svc.resource_manager = ResourceManager()
    svc.cert_manager = _OneRegisteredCluster()
    svc.project_manager = ProjectManager(conn, None)
    svc.instance_manager = InstanceManager(conn, svc.resource_manager, None)
    return svc


def _app(module) -> TestClient:
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_lxd_service] = _service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


# (module, path, the key holding the list of items). Every one of these fans out per node
# and reports unreachable_nodes.
CASES = [
    ("storage_volumes", "/storage/volumes", "volumes"),
    ("ipam", "/ipam", "allocations"),
    ("network_acls", "/network-acls", "acls"),
    ("lxd_operations", "/operations", "operations"),
    ("lxd_warnings", "/warnings", "warnings"),
    ("networks", "/networks", "networks"),
    ("profiles", "/profiles", "profiles"),
    ("storage_pools", "/storage/pools", "pools"),
]


@pytest.mark.parametrize("module_name,path,items_key", CASES)
def test_a_node_with_no_client_is_reported_unreachable(module_name, path, items_key):
    import importlib

    module = importlib.import_module(f"app.routers.{module_name}")
    r = _app(module).get(path, params={"fresh": "true"})

    assert r.status_code == 200, f"{path} answered {r.status_code}: {r.text[:200]}"
    body = r.json()

    assert body.get(items_key) == [], f"{path} produced items from a host with no client"
    assert HOST in body.get("unreachable_nodes", []), (
        f"{path} lost the host: items are empty and unreachable_nodes is "
        f"{body.get('unreachable_nodes')!r}, so the operator is told this cluster has "
        "nothing rather than that it did not answer"
    )


@pytest.mark.parametrize("module_name,path,items_key", CASES)
def test_an_unanswered_fan_out_is_not_written_to_the_cache(module_name, path, items_key):
    """Nothing answered, so there is no result worth caching.

    Caching it would pin the outage for the whole TTL, and a forced refresh would replace data
    the operator could still use with the outage's empty list.
    """
    import importlib

    module = importlib.import_module(f"app.routers.{module_name}")
    writes = []

    class _RecordingCache:
        def get(self, *args, **kwargs):
            return None

        def set(self, namespace, key, value, ttl=None):
            writes.append((namespace, key, ttl))

    import app.core.cache as cache_module

    original = cache_module.get_cache_manager
    cache_module.get_cache_manager = lambda: _RecordingCache()
    # the routers import the symbol directly, so patch the module-level name they hold
    had_local = hasattr(module, "get_cache_manager")
    if had_local:
        local_original = module.get_cache_manager
        module.get_cache_manager = lambda: _RecordingCache()
    try:
        r = _app(module).get(path, params={"fresh": "true"})
        assert r.status_code == 200, f"{path} answered {r.status_code}"
    finally:
        cache_module.get_cache_manager = original
        if had_local:
            module.get_cache_manager = local_original

    assert writes == [], (
        f"{path} cached a fan-out no cluster answered: {writes}. The next reader gets that "
        "outage served as the answer until the TTL expires."
    )


def test_a_blank_cluster_id_is_refused_before_it_reaches_vault():
    """`?node=` satisfies a required Query(...) parameter, and most declare no minimum length.

    A scoped caller's empty string fails the org-scope set, but an admin has no scope set, so
    without this guard the request would ask Vault for a cluster named "". The UI can send it:
    React Query's refetch() runs the query function even when the query is disabled.

    The guard sits in verify_cluster_access, which every cluster-scoped route calls.
    """
    import pytest
    from fastapi import HTTPException

    from app.core.org_scope import verify_cluster_access, set_allowed_clusters

    # admin: no scope set, so the scope check alone would let a blank id through
    set_allowed_clusters(None)
    for blank in ("", "   ", "\t"):
        with pytest.raises(HTTPException) as caught:
            verify_cluster_access(blank)
        assert caught.value.status_code == 422, f"{blank!r} answered {caught.value.status_code}"

    # a real id still passes for an unscoped caller
    verify_cluster_access("nuc2")

    # and the scope check still works for a scoped one
    set_allowed_clusters({"nuc2"})
    try:
        verify_cluster_access("nuc2")
        with pytest.raises(HTTPException) as caught:
            verify_cluster_access("blitz01")
        assert caught.value.status_code == 403
    finally:
        set_allowed_clusters(None)
