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
# Pool config a Tenant may read: sizes, never a device, a path or a remote pool name.
TENANT_POOL_CONFIG_KEYS = frozenset({"size", "volume.size"})
# Instance and profile config a Tenant may read: limits, the image, boot and snapshot settings, the
# base image and the instance's own MAC addresses. Everything else is dropped: raw.*, idmap and other
# volatile host state describe the host, and cloud-init, user.* and environment can carry another
# party's secrets.
TENANT_CONFIG_PREFIXES = ("limits.", "image.", "boot.", "snapshots.")
TENANT_CONFIG_KEYS = frozenset({"security.secureboot", "volatile.base_image"})
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
    # note: a disk's source names a volume when the disk sits on a pool, and a host path otherwise.
    if kind == "disk" and device.get("pool") and "source" in device:
        kept["source"] = device["source"]
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
    """An instance's live sample: no host process id for a Tenant."""
    return _without(metrics, tier, pid=None)


def cluster_for_tier(cluster: Model, tier: Tier) -> Model:
    """A registered cluster: no LXD endpoint for a Tenant."""
    return _without(cluster, tier, endpoint=None)


def cluster_list_for_tier(response: Model, tier: Tier) -> Model:
    if sees_host_detail(tier):
        return response
    rows = [cluster_for_tier(cluster, tier) for cluster in getattr(response, "clusters")]
    return response.model_copy(update={"clusters": rows})


def tenant_pool_config(config: Mapping[str, Any]) -> dict:
    return {key: value for key, value in config.items() if key in TENANT_POOL_CONFIG_KEYS}


def pool_for_tier(pool: Model, tier: Tier) -> Model:
    """A storage pool: only its size settings for a Tenant, never `source` or a remote name."""
    if sees_host_detail(tier):
        return pool
    return pool.model_copy(update={"config": tenant_pool_config(getattr(pool, "config") or {})})


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
