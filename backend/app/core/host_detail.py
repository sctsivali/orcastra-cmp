"""Host-level detail is for Partners and Admins; a Tenant gets the same body without it.

A Tenant reaches an instance, not the hypervisor under it. The LXD endpoint, the cluster member an
instance runs on, the instance's process id on the host and the paths behind a storage pool all
describe that hypervisor, and /nodes already withholds the endpoint from Tenants
(routers/nodes.py `_availability_for_user`).

important: every function returns a copy and never mutates its input. With Redis down the cache
manager hands back the stored object itself, so redacting in place would strip the field from the
next Partner's response too.

note: the tier comes from `caller_tier()`, which reads the org-scope context. A route has to run
under `org_scope_dependency`, or an unset scope reads as Admin; a ratchet test pins that.
"""

from typing import Any, Mapping, TypeVar

from pydantic import BaseModel

from app.core.task_scope import Tier

HOST_DETAIL_TIERS = frozenset({"admin", "partner"})
# Pool config a Tenant may read: sizes, never a device, a path or a remote pool name.
TENANT_POOL_CONFIG_KEYS = frozenset({"size", "volume.size"})

Model = TypeVar("Model", bound=BaseModel)


def sees_host_detail(tier: Tier) -> bool:
    return tier in HOST_DETAIL_TIERS


def _without(model: Model, tier: Tier, **update: Any) -> Model:
    return model if sees_host_detail(tier) else model.model_copy(update=update)


def vm_for_tier(vm: Model, tier: Tier) -> Model:
    """An instance row: no LXD endpoint and no cluster member for a Tenant."""
    return _without(vm, tier, host_endpoint=None, location=None)


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
