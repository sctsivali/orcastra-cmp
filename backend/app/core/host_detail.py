"""Host-level detail is for Partners and Admins; a Tenant gets the same body without it.

A Tenant reaches an instance, not the hypervisor under it. The LXD endpoint, the cluster member an
instance runs on, the instance's process id on the host, the paths behind a storage pool and the
host side of an instance's or a profile's config and devices (a disk's host path, a NIC's host
bridge, raw.* and idmap settings) all describe that hypervisor, and /nodes already withholds the
endpoint from Tenants (routers/nodes.py `_availability_for_user`).

important: every function returns a copy and never mutates its input. With Redis down the cache
manager hands back the stored object itself, so redacting in place would strip the field from the
next Partner's response too.

note: the tier comes from `caller_tier()`, which reads the org-scope context. A route has to run
under `org_scope_dependency`, or an unset scope reads as Admin; a ratchet test pins that.
"""

import re
from typing import Any, Mapping, Optional, TypeVar

from pydantic import BaseModel

from app.core.task_scope import Tier

HOST_DETAIL_TIERS = frozenset({"admin", "partner"})
# Instance and profile config a Tenant may read: limits, the image's identity, boot and snapshot
# settings, the base image and the instance's own MAC addresses. Everything else is dropped: raw.*,
# idmap and other volatile host state describe the host, and cloud-init, user.* and environment can
# carry another party's secrets. note: image.* is free-form like user.*, so only the properties an
# image itself carries pass, never a prefix; limits., boot. and snapshots. are keys LXD validates.
TENANT_CONFIG_PREFIXES = ("limits.", "boot.", "snapshots.")
TENANT_CONFIG_KEYS = frozenset({
    "security.secureboot", "volatile.base_image",
    "image.architecture", "image.description", "image.label", "image.os", "image.release",
    "image.serial", "image.type", "image.variant", "image.version",
})
_INSTANCE_MAC = re.compile(r"volatile\.[^.]+\.hwaddr")
# Device fields a Tenant may read, by device type; any other type keeps only its type. A device keeps
# its name either way, since the UI tells an instance's own devices from inherited ones by name.
TENANT_DEVICE_FIELDS = {
    "disk": frozenset({"type", "path", "pool", "size", "size.state", "boot.priority", "readonly"}),
    "nic": frozenset({
        "type", "name", "network", "nictype", "hwaddr", "ipv4.address", "ipv6.address", "boot.priority",
    }),
}
_TYPE_ONLY = frozenset({"type"})

Model = TypeVar("Model", bound=BaseModel)


def sees_host_detail(tier: Tier) -> bool:
    return tier in HOST_DETAIL_TIERS


def _without(model: Model, tier: Tier, **update: Any) -> Model:
    return model if sees_host_detail(tier) else model.model_copy(update=update)


def tenant_config(config: Mapping[str, Any]) -> dict:
    return {
        key: value for key, value in config.items()
        if key in TENANT_CONFIG_KEYS or key.startswith(TENANT_CONFIG_PREFIXES) or _INSTANCE_MAC.fullmatch(key)
    }


def tenant_device(device: Mapping[str, Any]) -> dict:
    kind = device.get("type")
    kept = {key: value for key, value in device.items() if key in TENANT_DEVICE_FIELDS.get(kind, _TYPE_ONLY)}
    # note: a disk's source names a volume when the disk sits on a pool, and a host path otherwise;
    # an absolute source is dropped even beside a pool, whatever LXD would make of it.
    source = device.get("source")
    if kind == "disk" and device.get("pool") and isinstance(source, str) and not source.startswith("/"):
        kept["source"] = source
    return kept


def tenant_devices(devices: Optional[Mapping[str, Mapping[str, Any]]]) -> Optional[dict]:
    if devices is None:
        return None
    return {name: tenant_device(device or {}) for name, device in devices.items()}


def vm_for_tier(vm: Model, tier: Tier) -> Model:
    """An instance row for a Tenant: no LXD endpoint, no cluster member, and config and devices
    cut to what describes the instance rather than its host."""
    if sees_host_detail(tier):
        return vm
    config = getattr(vm, "config", None)
    return vm.model_copy(update={
        "host_endpoint": None,
        "location": None,
        "config": None if config is None else tenant_config(config),
        "devices": tenant_devices(getattr(vm, "devices", None)),
        "expanded_devices": tenant_devices(getattr(vm, "expanded_devices", None)),
    })


def vm_list_for_tier(response: Model, tier: Tier) -> Model:
    if sees_host_detail(tier):
        return response
    rows = [vm_for_tier(vm, tier) for vm in getattr(response, "vms")]
    return response.model_copy(update={"vms": rows})


def metrics_for_tier(metrics: Model, tier: Tier) -> Model:
    """An instance's live sample: no host process id or root pool driver for a Tenant."""
    return _without(metrics, tier, pid=None, disk_io_storage_driver=None)


def cluster_for_tier(cluster: Model, tier: Tier) -> Model:
    """A registered cluster as a Tenant may read it: its name, status and place.

    The endpoint, the hypervisor family, whether it is clustered, the certificate's name and the
    raw status error all describe the hypervisor; the error can carry the endpoint itself.
    """
    return _without(
        cluster,
        tier,
        endpoint=None,
        cluster_type=None,
        clustered=None,
        certificate_common_name=None,
        error_message=None,
    )


def cluster_list_for_tier(response: Model, tier: Tier) -> Model:
    if sees_host_detail(tier):
        return response
    rows = [cluster_for_tier(cluster, tier) for cluster in getattr(response, "clusters")]
    return response.model_copy(update={"clusters": rows})


def pool_for_tier(pool: Model, tier: Tier) -> Model:
    """A storage pool as a Tenant may read it: its name and what uses it.

    A pool is shared by every party on the cluster, so its config (size, source, remote names),
    driver and state describe the provider's storage, not the Tenant's (owner decision
    2026-10-26). The capacity routes refuse a Tenant for the same reason.
    """
    if sees_host_detail(tier):
        return pool
    # note: the description is the operator's note about the pool and often names its backend.
    return pool.model_copy(update={"config": {}, "driver": None, "status": None, "description": ""})


def pool_list_for_tier(response: Model, tier: Tier) -> Model:
    if sees_host_detail(tier):
        return response
    rows = [pool_for_tier(pool, tier) for pool in getattr(response, "pools")]
    return response.model_copy(update={"pools": rows})


def profile_for_tier(profile: Model, tier: Tier) -> Model:
    """A profile for a Tenant: the same config and device allowlist as an instance."""
    if sees_host_detail(tier):
        return profile
    return profile.model_copy(update={
        "config": tenant_config(getattr(profile, "config") or {}),
        "devices": tenant_devices(getattr(profile, "devices") or {}),
    })


def profile_list_for_tier(response: Model, tier: Tier) -> Model:
    if sees_host_detail(tier):
        return response
    rows = [profile_for_tier(profile, tier) for profile in getattr(response, "profiles")]
    return response.model_copy(update={"profiles": rows})


# What a Tenant keeps of one cluster on the monitoring dashboard: whether it answers and how many
# of the Tenant's own instances run there. Owner decision 2026-10-26: a Tenant monitors its
# instances and the clusters' status, not the hosts, whose load counts every other party's
# instances too.
TENANT_CLUSTER_MONITORING_FIELDS = frozenset({
    "node_name", "is_online", "cluster_status", "collection_timed_out", "cpu_reporting_instances",
    "instance_count", "running_instances", "container_count", "vm_count",
})

# Fleet figures the dashboard derives from the hosts' capacity and load.
FLEET_HOST_FIGURES = (
    "total_cpu", "used_cpu", "cpu_usage_percent", "realtime_cpu_percent", "cpu_busy_threads",
    "cpu_busy_percent", "cpu_reporting_threads", "total_memory_bytes", "used_memory_bytes",
    "memory_usage_percent", "realtime_memory_bytes", "realtime_memory_percent",
    "total_storage_bytes", "used_storage_bytes", "storage_usage_percent", "total_gpu",
    "total_gpu_cards", "used_gpu",
)


def cluster_monitoring_for_tenant(node: Model) -> Model:
    """One cluster's monitoring row with every host figure null, not zero."""
    update: dict = {
        field: None for field in type(node).model_fields if field not in TENANT_CLUSTER_MONITORING_FIELDS
    }
    update.update(cpu_status="unavailable", gpu_models=[])
    return node.model_copy(update=update)


def monitoring_dashboard_for_tier(response: Model, tier: Tier) -> Model:
    """The monitoring dashboard as this tier may read it.

    A Tenant keeps cluster counts and status, its own instance counts and its own instances'
    usage; the hosts' capacity and load, storage pools, cluster warnings and the raw status error
    are withheld. Null rather than 0, so a client cannot read an idle or empty fleet into them.
    """
    if sees_host_detail(tier):
        return response
    update: dict = dict.fromkeys(FLEET_HOST_FIGURES)
    update.update(
        cpu_status="unavailable",
        storage_pools=[],
        warnings=None,
        nodes=[cluster_monitoring_for_tenant(node) for node in getattr(response, "nodes")],
    )
    return response.model_copy(update=update)
