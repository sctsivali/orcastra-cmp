"""A list read in flight when an instance changed must not write the old picture back.

The instance list is cached for 30 s and cleared when a create is accepted and again when its
background start lands. A fleet read takes about ten seconds on a deployment with a dead
cluster registered, so the console's own re-read was already running when the start landed:
it read Stopped, the clearance ran, and then the read wrote Stopped back for a full TTL. The
row read Stopped for 39 s after the cluster had the instance Running, with the clearance in
place. Every instance invalidation now leaves a generation token, and a list read caches its
answer only if the token it saw before reading is still the one after.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import cache as cache_mod
from app.models.schemas import VMInfo, VMStatus
from app.core.cache import CacheManager, INSTANCE_LIST_GENERATION_KEY
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import instances as instances_module
from app.services.lxd_service import get_lxd_service


def _manager() -> CacheManager:
    """A manager with no Redis: the in-process tier alone, which is enough for the token."""
    return CacheManager(redis_url=None)


class _Service:
    """list_all_vms answers a Stopped instance and, when asked, invalidates mid-read."""

    def __init__(self, manager: CacheManager, invalidate_during_read: bool) -> None:
        self.manager = manager
        self.invalidate_during_read = invalidate_during_read
        self.reads = 0

    def get_node_names(self):
        return ["mc-lab"]

    async def list_all_vms(self):
        self.reads += 1
        if self.invalidate_during_read:
            self.manager.invalidate_instance_cache()
        vm = VMInfo(name="web01", status=VMStatus.STOPPED, status_code=102, host="mc-lab",
                    host_endpoint="https://lab:9444", project="UAT-Hermes", type="container", architecture="x86_64")
        return [vm], ["mc-lab"]


def _client(manager: CacheManager, service: _Service, monkeypatch) -> TestClient:
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(instances_module, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(instances_module, "verify_cluster_access", lambda host: None)
    app = FastAPI()
    app.include_router(instances_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _cached_lists(manager: CacheManager) -> int:
    return sum(1 for k in manager.memory_cache.cache if "instances:list" in k)


def test_an_invalidation_leaves_a_generation_token():
    manager = _manager()
    assert manager.instance_list_generation() is None
    manager.invalidate_instance_cache()
    first = manager.instance_list_generation()
    assert first is not None
    manager.invalidate_instance_cache()
    assert manager.instance_list_generation() != first
    assert INSTANCE_LIST_GENERATION_KEY not in ("instances:list",), "the token must not match the instances:* pattern"
    assert not INSTANCE_LIST_GENERATION_KEY.startswith("instances:")


def test_a_read_that_raced_an_invalidation_is_not_cached(monkeypatch):
    manager = _manager()
    service = _Service(manager, invalidate_during_read=True)
    client = _client(manager, service, monkeypatch)
    r = client.get("/instances")
    assert r.status_code == 200, r.text
    assert _cached_lists(manager) == 0, "the old picture must not be written back over the clearance"
    r = client.get("/instances")
    assert r.status_code == 200
    assert service.reads == 2, "the next read goes to the cluster, not to a stale entry"


def test_an_undisturbed_read_is_cached_as_before(monkeypatch):
    manager = _manager()
    service = _Service(manager, invalidate_during_read=False)
    client = _client(manager, service, monkeypatch)
    assert client.get("/instances").status_code == 200
    assert _cached_lists(manager) == 1
    assert client.get("/instances").status_code == 200
    assert service.reads == 1, "the second read is served from cache"
