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
from app.core.cache import (
    INSTANCE_LIST_GENERATION_KEY,
    SNAPSHOT_FLEET_GENERATION_KEY,
    CacheManager,
)
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import instances as instances_module
from app.routers import snapshots_fleet as snapshots_fleet_module
from app.services.lxd_service import get_lxd_service
from app.services.lxd.instance import FleetInventory


def _manager() -> CacheManager:
    """A manager with no Redis: the in-process tier alone, which is enough for the token."""
    return CacheManager(redis_url=None)


class _Service:
    """The detailed inventory answers a Stopped instance and can invalidate mid-read."""

    def __init__(self, manager: CacheManager, invalidate_during_read: bool) -> None:
        self.manager = manager
        self.invalidate_during_read = invalidate_during_read
        self.reads = 0

    def get_node_names(self):
        return ["mc-lab"]

    def _read(self):
        self.reads += 1
        if self.invalidate_during_read:
            self.manager.invalidate_instance_cache()
        vm = VMInfo(name="web01", status=VMStatus.STOPPED, status_code=102, host="mc-lab",
                    host_endpoint="https://lab:9444", project="UAT-Hermes", type="container", architecture="x86_64")
        return [vm], ["mc-lab"], []

    async def read_fleet_inventory(self):
        # The instance list reads the fleet through this since 2026-10-29; it names a late
        # cluster instead of raising, which these doubles never produce.
        vms, connected, incomplete = await self.list_all_vms_with_failures()
        return FleetInventory(vms, connected, incomplete, {})

    async def list_all_vms_with_failures(self):
        return self._read()

    async def list_vms_in_project_with_failures(self, host, project):
        assert (host, project) == ("mc-lab", "UAT-Hermes")
        return self._read()


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


class _SnapshotService:
    def __init__(self, manager: CacheManager, invalidate_during_read: bool) -> None:
        self.manager = manager
        self.invalidate_during_read = invalidate_during_read
        self.reads = 0

    def get_node_names(self):
        return ["mc-lab"]

    def read_cluster(self, _service, _host, _project):
        self.reads += 1
        if self.invalidate_during_read:
            self.manager.invalidate_resource_cache("snapshots_fleet")
        return [], True


def _snapshot_client(
    manager: CacheManager, service: _SnapshotService, monkeypatch
) -> TestClient:
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(snapshots_fleet_module, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(
        snapshots_fleet_module, "_assert_snapshot_surface_enabled", lambda: None
    )
    monkeypatch.setattr(
        snapshots_fleet_module, "_read_one_cluster", service.read_cluster
    )
    app = FastAPI()
    app.include_router(snapshots_fleet_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _cached_snapshot_lists(manager: CacheManager) -> int:
    return sum(1 for key in manager.memory_cache.cache if "snapshots_fleet:list" in key)


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


def test_a_snapshot_invalidation_leaves_its_generation_token():
    manager = _manager()
    assert manager.snapshot_fleet_generation() is None
    manager.invalidate_resource_cache("snapshots_fleet")
    first = manager.snapshot_fleet_generation()
    assert first is not None
    manager.invalidate_resource_cache("snapshots_fleet")
    assert manager.snapshot_fleet_generation() != first
    assert not SNAPSHOT_FLEET_GENERATION_KEY.startswith("snapshots_fleet:")


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


def test_a_scoped_read_that_raced_an_invalidation_is_not_cached(monkeypatch):
    manager = _manager()
    service = _Service(manager, invalidate_during_read=True)
    client = _client(manager, service, monkeypatch)

    response = client.get("/instances?host=mc-lab&project=UAT-Hermes")

    assert response.status_code == 200, response.text
    assert _cached_lists(manager) == 0
    assert client.get("/instances?host=mc-lab&project=UAT-Hermes").status_code == 200
    assert service.reads == 2, "the next scoped read must not use a stale cache entry"


def test_an_undisturbed_read_is_cached_as_before(monkeypatch):
    manager = _manager()
    service = _Service(manager, invalidate_during_read=False)
    client = _client(manager, service, monkeypatch)
    assert client.get("/instances").status_code == 200
    assert _cached_lists(manager) == 1
    assert client.get("/instances").status_code == 200
    assert service.reads == 1, "the second read is served from cache"


def test_a_snapshot_read_that_raced_an_invalidation_is_not_cached(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=True)
    client = _snapshot_client(manager, service, monkeypatch)

    response = client.get("/snapshots")

    assert response.status_code == 200, response.text
    assert _cached_snapshot_lists(manager) == 0
    assert client.get("/snapshots").status_code == 200
    assert service.reads == 2, "the next read reaches the cluster instead of stale cache"


def test_an_undisturbed_snapshot_read_is_cached(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=False)
    client = _snapshot_client(manager, service, monkeypatch)

    assert client.get("/snapshots").status_code == 200
    assert _cached_snapshot_lists(manager) == 1
    assert client.get("/snapshots").status_code == 200
    assert service.reads == 1
