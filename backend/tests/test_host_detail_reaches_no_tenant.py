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
from app.routers import storage_volumes as volumes_module
from app.routers import vms as vms_module
from app.services.lxd_service import get_lxd_service
from app.core.cache_envelope import unwrap

HOST = "shared-cluster"
NAME = "web-1"
ENDPOINT = "https://cluster.invalid:8443"
MEMBER = "member-2"
POOL_CONFIG = {"source": "/dev/disk/by-id/lab-disk", "size": "100GiB", "zfs.pool_name": "tank"}
VM_CONFIG = {
    "limits.cpu": "2", "image.os": "Ubuntu", "boot.autostart": "true", "snapshots.schedule": "@daily",
    "security.secureboot": "false", "volatile.base_image": "abc123", "volatile.eth0.hwaddr": "00:16:3e:aa:bb:cc",
    "raw.lxc": "lxc.apparmor.profile=unconfined", "volatile.idmap.base": "1000000",
    "cloud-init.user-data": "#cloud-config\npassword: hunter2", "user.user-data": "secret",
    "environment.API_TOKEN": "t0ken", "security.privileged": "true", "image.secret_token": "s3cret",
}
TENANT_VM_CONFIG = {
    "limits.cpu": "2", "image.os": "Ubuntu", "boot.autostart": "true", "snapshots.schedule": "@daily",
    "security.secureboot": "false", "volatile.base_image": "abc123", "volatile.eth0.hwaddr": "00:16:3e:aa:bb:cc",
}
VM_DEVICES = {
    "root": {"type": "disk", "path": "/", "pool": "default", "size": "10GiB"},
    "eth0": {
        "type": "nic", "nictype": "bridged", "parent": "br-host0", "host_name": "veth9f2",
        "hwaddr": "00:16:3e:aa:bb:cc", "ipv4.address": "10.0.0.5",
    },
    "data": {"type": "disk", "pool": "default", "source": "data-vol", "path": "/data"},
    "logs": {"type": "disk", "source": "/srv/hostlogs", "path": "/var/log/host"},
    "gpu0": {"type": "gpu", "pci": "0000:01:00.0", "vendorid": "10de"},
    "odd": {"type": "disk", "pool": "default", "source": "/etc", "path": "/mnt/odd"},
}
TENANT_VM_DEVICES = {
    "root": {"type": "disk", "path": "/", "pool": "default", "size": "10GiB"},
    "eth0": {"type": "nic", "nictype": "bridged", "hwaddr": "00:16:3e:aa:bb:cc", "ipv4.address": "10.0.0.5"},
    "data": {"type": "disk", "pool": "default", "source": "data-vol", "path": "/data"},
    "logs": {"type": "disk", "path": "/var/log/host"},
    "gpu0": {"type": "gpu"},
    "odd": {"type": "disk", "pool": "default", "path": "/mnt/odd"},
}

TIERS = {
    "admin": (None, None),
    "partner": ({HOST}, None),
    "tenant": ({HOST}, {HOST: {("default", NAME): {"view"}}}),
}


def _vm() -> VMInfo:
    return VMInfo(
        name=NAME, status=VMStatus.RUNNING, status_code=103, host=HOST, host_endpoint=ENDPOINT,
        location=MEMBER, project="default", type="container", architecture="x86_64",
        config=dict(VM_CONFIG), devices={"eth0": dict(VM_DEVICES["eth0"])},
        expanded_devices={name: dict(device) for name, device in VM_DEVICES.items()},
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
    for module in (instances_module, vms_module, pools_module, volumes_module):
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
    vm, metrics = _vm(), InstanceMetrics(
        instance_name=NAME, host=HOST, status=VMStatus.RUNNING, pid=7, disk_io_storage_driver="zfs"
    )

    shown_vm = host_detail.vm_for_tier(vm, tier)
    shown_metrics = host_detail.metrics_for_tier(metrics, tier)

    assert (vm.host_endpoint, vm.location, metrics.pid) == (ENDPOINT, MEMBER, 7), "input untouched"
    assert vm.config == VM_CONFIG and vm.expanded_devices == VM_DEVICES, "input untouched"
    if tier == "tenant":
        assert (shown_vm.host_endpoint, shown_vm.location, shown_metrics.pid) == (None, None, None)
        # the root pool's driver is the provider's storage (owner decision 2026-10-26)
        assert shown_metrics.disk_io_storage_driver is None and metrics.disk_io_storage_driver == "zfs"
        assert shown_vm.config == TENANT_VM_CONFIG and shown_vm.expanded_devices == TENANT_VM_DEVICES
        assert shown_vm.devices == {"eth0": TENANT_VM_DEVICES["eth0"]}
        assert shown_vm is not vm and shown_metrics is not metrics
    else:
        assert shown_vm is vm and shown_metrics is metrics


def test_a_tenant_pool_keeps_only_its_name():
    # Owner decision 2026-10-26: no size, driver or state; it used to keep the size settings.
    class Pool(host_detail.BaseModel):
        name: str
        config: dict
        driver: str | None = "zfs"
        status: str | None = "Created"
        description: str = ""

    pool = Pool(name="p", config=dict(POOL_CONFIG), description="10 TiB NVMe ZFS mirror")

    shown = host_detail.pool_for_tier(pool, "tenant")

    assert (shown.name, shown.config, shown.driver, shown.status) == ("p", {}, None, None)
    assert shown.description == "", "an operator's note about the pool often names its backend"
    assert pool.config == POOL_CONFIG and pool.driver == "zfs"
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
        assert row["config"] == (TENANT_VM_CONFIG if tier == "tenant" else VM_CONFIG)
        assert row["expanded_devices"] == (TENANT_VM_DEVICES if tier == "tenant" else VM_DEVICES)
    stored = [
        unwrap(value).value
        for key, (value, _) in manager.memory_cache.cache.items()
        if "instances:list" in key
    ]
    assert stored and stored[0]["vms"][0]["host_endpoint"] == ENDPOINT, "the cache keeps the full body"
    assert stored[0]["vms"][0]["config"] == VM_CONFIG


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
        manager.set("api_responses", get_scope_cache_key("clusters:list:all:e1"), body, ttl=60)
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
        manager.set("api_responses", get_scope_cache_key("storage_pools:list:e1:host=None"), body, ttl=60)
        return client.get("/storage/pools")

    response = _as(tier, read)

    assert response.status_code == 200, response.text[:200]
    pool = response.json()["pools"][0]
    # Owner decision 2026-10-26: a tenant reads a pool's name and what uses it, not its size,
    # driver or state; a pool is shared by every party on the cluster.
    if tier == "tenant":
        assert (pool["config"], pool["driver"], pool["status"]) == ({}, None, None)
    else:
        assert (pool["config"], pool["driver"]) == (POOL_CONFIG, "zfs")


HOST_DETAIL_MODELS = {
    "VMInfo", "VMListResponse", "InstanceMetrics", "ClusterInfo", "ClusterListResponse",
    "StoragePoolInfo", "StoragePoolListResponse", "ProfileListResponse", "ProfileDetail",
    "StorageVolumeInfo", "StorageVolumeListResponse",
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
    # 14 since the two storage volume routes joined (owner decision 2026-10-26)
    assert len(checked) == 14, checked


def test_a_tenant_device_keeps_its_name_and_loses_the_host_side():
    shown = host_detail.tenant_devices(VM_DEVICES)

    assert shown == TENANT_VM_DEVICES
    assert set(shown) == set(VM_DEVICES), "every device keeps its name, so own and inherited still match"
    assert host_detail.tenant_devices(None) is None


@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_a_cached_profile_list_reaches_a_tenant_without_the_host_side(monkeypatch, tier):
    from app.routers import profiles as profiles_module

    manager = CacheManager(redis_url=None)
    monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(profiles_module, "get_cache_manager", lambda: manager)
    app = FastAPI()
    app.include_router(profiles_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: _Service()
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)
    body = {
        "total": 1,
        "profiles": [{
            "name": "default", "config": dict(VM_CONFIG),
            "devices": {name: dict(device) for name, device in VM_DEVICES.items()},
            "used_by": [], "node": HOST, "project": "default",
        }],
        "nodes": [HOST],
    }

    def read():
        manager.set("api_responses", get_scope_cache_key("profiles:list:host=None:project=*"), body, ttl=60)
        return client.get("/profiles")

    response = _as(tier, read)

    assert response.status_code == 200, response.text[:200]
    profile = response.json()["profiles"][0]
    assert profile["config"] == (TENANT_VM_CONFIG if tier == "tenant" else VM_CONFIG)
    assert profile["devices"] == (TENANT_VM_DEVICES if tier == "tenant" else VM_DEVICES)
    assert body["profiles"][0]["config"] == VM_CONFIG, "the cached body is never redacted in place"



@pytest.mark.parametrize("tier", ["partner", "tenant"])
def test_a_cluster_row_reaches_a_tenant_without_hypervisor_metadata(monkeypatch, tier):
    # The hypervisor family, whether it is clustered, its certificate's name and the raw status
    # error (which can carry the endpoint) all describe the hypervisor. /nodes/availability and
    # /regions already withhold the family, and no tenant view reads any of them.
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
    row = ClusterInfo(
        cluster_id=HOST, display_name=HOST, endpoint=ENDPOINT, status="error",
        cluster_type="Incus", clustered=True, certificate_common_name="orcastra-shared-cluster",
        error_message=f"Could not connect to {ENDPOINT}: connection refused",
    ).model_dump()
    body = {"clusters": [row], "total": 1}

    def read():
        manager.set("api_responses", get_scope_cache_key("clusters:list:all:e1"), body, ttl=60)
        return client.get("/clusters")

    response = _as(tier, read)

    assert response.status_code == 200, response.text[:200]
    seen = response.json()["clusters"][0]
    hidden = ("endpoint", "cluster_type", "clustered", "certificate_common_name", "error_message")
    if tier == "tenant":
        assert {field: seen[field] for field in hidden} == dict.fromkeys(hidden)
        assert (seen["cluster_id"], seen["display_name"], seen["status"]) == (HOST, HOST, "error")
    else:
        assert {field: seen[field] for field in hidden} == {field: row[field] for field in hidden}
    assert body["clusters"][0]["cluster_type"] == "Incus", "the cached row was edited"



@pytest.mark.parametrize(
    "path",
    ["/storage/pools/resources-all", "/storage/pools-resources", "/storage/pools/default/resources?host=nuc2"],
)
def test_pool_capacity_is_refused_to_a_tenant(path):
    # The whole pool's size and usage count every party's volumes, the figure the monitoring
    # dashboard also withholds from a tenant (owner decision 2026-10-26).
    from app.core.auth import AuthenticatedUser

    app = FastAPI()
    app.include_router(pools_module.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="t", email="t@example.com", groups=["role_tenant"]
    )
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[get_lxd_service] = lambda: object()
    response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code == 403, response.text[:200]



VOLUME_CONFIG = {
    "size": "10GiB", "snapshots.expiry": "7d", "snapshots.schedule": "@daily",
    "zfs.blocksize": "16KiB", "block.filesystem": "ext4", "block.mount_options": "discard",
    "volatile.uuid": "abc", "security.shifted": "true",
}


@pytest.mark.parametrize("tier", ["partner", "tenant"])
def test_a_cached_volume_list_reaches_a_tenant_with_size_and_snapshots_only(monkeypatch, tier):
    # Owner decision 2026-10-26: a tenant reads its volumes' size and snapshot settings. The
    # other keys name the pool's backend (zfs.*, block.*) and location names the cluster member,
    # which vm_for_tier already withholds for an instance.
    client, manager = _client(monkeypatch, volumes_module.router)
    body = {
        "total": 1,
        "volumes": [{
            "name": "web-1", "type": "container", "pool": "default", "config": dict(VOLUME_CONFIG),
            "used_by": [], "node": HOST, "location": MEMBER, "description": "nvme mirror, rack 2",
        }],
        "nodes": [HOST],
    }

    def read():
        manager.set(
            "api_responses", get_scope_cache_key("storage_volumes:list:e1:host=None:pool=None"), body, ttl=60
        )
        return client.get("/storage/volumes")

    response = _as(tier, read)

    assert response.status_code == 200, response.text[:200]
    volume = response.json()["volumes"][0]
    if tier == "tenant":
        assert volume["config"] == {"size": "10GiB", "snapshots.expiry": "7d", "snapshots.schedule": "@daily"}
        assert volume["location"] is None
        assert volume["description"] == "", "a volume's description is the operator's note"
    else:
        assert (volume["config"], volume["location"]) == (VOLUME_CONFIG, MEMBER)
    assert volume["name"] == "web-1" and volume["pool"] == "default"
    assert body["volumes"][0]["location"] == MEMBER, "the cached row was edited"



def test_a_tenant_reads_only_instance_references_and_no_cluster_member():
    # LXD appends ?target=<member> to a volume or image reference on a clustered pool, so a
    # used_by list would name the member the location field no longer does. And a reference that
    # names no instance passes on any rule in its project, so it named other parties' custom
    # volumes and images: a tenant keeps instance references only (owner decision 2026-10-26).
    class Volume(host_detail.BaseModel):
        name: str
        config: dict = {}
        location: str | None = MEMBER
        description: str = ""
        used_by: list = []

    refs = [
        f"/1.0/storage-pools/local/volumes/custom/data?project=p&target={MEMBER}",
        f"/1.0/images/abc?target={MEMBER}&project=p",
        "/1.0/instances/web-1?project=p",
        f"/1.0/storage-pools/local/volumes/container/web-1?project=p&target={MEMBER}",
        "/1.0/profiles/default?project=p",
    ]
    volume = Volume(name="data", used_by=list(refs))

    shown = host_detail.volume_for_tier(volume, "tenant")

    assert shown.used_by == [
        "/1.0/instances/web-1?project=p",
        "/1.0/storage-pools/local/volumes/container/web-1?project=p",
    ]
    assert MEMBER not in " ".join(host_detail.pool_for_tier(volume, "tenant").used_by)
    assert host_detail.volume_for_tier(volume, "partner").used_by == refs
    assert volume.used_by == refs, "input untouched"


def test_the_volume_detail_ignores_a_tenant_member_probe(monkeypatch):
    # A per-member pool answers only from the member that holds the volume, so honouring a
    # tenant's ?location= turned 200 against 404 into a way to find the member.
    seen = {}

    class _Node:
        def get(self, params=None):
            seen["params"] = params
            return type("R", (), {"json": lambda self: {"metadata": {"name": "data", "used_by": ["/1.0/instances/web-1?project=default"]}}})()

    monkeypatch.setattr(volumes_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(volumes_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(volumes_module, "filter_visible_lxd_references", lambda host, refs: refs)

    async def client_for(_service, _host):
        return object()

    monkeypatch.setattr(volumes_module, "client_for", client_for)
    monkeypatch.setattr(volumes_module, "require_client", lambda client, host: client)
    monkeypatch.setattr(volumes_module, "_volume_node", lambda *args: _Node())
    import asyncio

    for tier, expected in (("tenant", None), ("partner", MEMBER)):
        monkeypatch.setattr(volumes_module, "caller_tier", lambda tier=tier: tier)
        seen.clear()
        asyncio.run(volumes_module.get_storage_volume(
            volume_type="custom", volume_name="data", host=HOST, pool="local", project=None,
            location=MEMBER, lxd_service=None,
        ))
        target = (seen["params"] or {}).get("target")
        assert target == expected, f"{tier} sent target={target!r}"
