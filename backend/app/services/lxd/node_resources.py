"""How a node's resource picture is assembled.

Single-host resources and the aggregate of a clustered host both end up as one NodeResources,
built here so a new field cannot be added to one path and forgotten on the other.
"""

import logging
from typing import Any, Dict, List, Optional

import pylxd

from app.models.schemas import NodeResources
from app.services.lxd.cpu_allocation import allocated_cpu, allocation_ratio, clamp_available

from .gpu import GpuInventory, build_gpu_inventory
from .hardware import build_hardware_profile, merge_hardware_profiles
from .member_reads import members_read_failed, read_members, read_members_list
from .storage_model import PoolReading, summarise_cluster_storage
from app.services.lxd.project_scope import list_instance_records

logger = logging.getLogger(__name__)


def build_node_resources(resources_data: Dict[str, Any], gpu: GpuInventory) -> NodeResources:
    """Build NodeResources for one host from a resource dict and its GPU inventory.

    important: NodeResources is built only in this module, so no path can omit a field such
    as gpu_cards_total and report it as its default.
    """
    cpu_total = resources_data.get("cpu_total", 0)
    memory_total = resources_data.get("memory_total", 0)
    # cpu_used comes from the dict when the read carried it: cpu_available is clamped at zero
    # on an over-committed host, so total minus available would under-report the allocation.
    cpu_used = resources_data.get("cpu_used", cpu_total - resources_data.get("cpu_available", 0))
    return NodeResources(
        cpu_total=cpu_total,
        cpu_used=cpu_used,
        cpu_available=clamp_available(cpu_total, cpu_used),
        cpu_allocated=cpu_used,
        cpu_allocation_ratio=allocation_ratio(cpu_used, cpu_total),
        cpu_overcommitted=cpu_used > cpu_total,
        cpu_sockets=resources_data.get("cpu_sockets", 0),
        memory_total=memory_total,
        memory_used=memory_total - resources_data.get("memory_available", 0),
        memory_available=resources_data.get("memory_available", 0),
        gpu_total=gpu.gpu_total,
        gpu_used=gpu.gpu_used,
        gpu_available=gpu.gpu_available,
        gpu_cards=gpu.cards,
        gpu_cards_total=gpu.gpu_cards_total,
        gpu_integrated_total=gpu.gpu_integrated_total,
        gpu_display_total=gpu.gpu_display_total,
        gpu_unknown_total=gpu.gpu_unknown_total,
        gpu_vendors=gpu.vendors,
        gpu_models=gpu.models,
        gpu_utilization_supported=gpu.utilization_supported,
        storage_total=resources_data.get("storage_total", 0),
        # Derived when absent rather than defaulted to zero, so a hand-built dict cannot
        # report a full pool as unused; readers assume storage_used agrees with total and
        # available.
        storage_used=resources_data.get(
            "storage_used",
            resources_data.get("storage_total", 0)
            - resources_data.get("storage_available", 0),
        ),
        storage_available=resources_data.get("storage_available", 0),
        # On a standalone host usable and raw are the same number, so the fallback is the
        # usable figure rather than zero.
        storage_raw_total=resources_data.get(
            "storage_raw_total", resources_data.get("storage_total", 0)
        ),
        storage_raw_used=resources_data.get(
            "storage_raw_used",
            resources_data.get(
                "storage_used",
                resources_data.get("storage_total", 0)
                - resources_data.get("storage_available", 0),
            ),
        ),
        storage_pools=resources_data.get("storage_pools", []),
        degraded=resources_data.get("degraded", False),
        degraded_parts=resources_data.get("degraded_parts", []),
        hardware=resources_data.get("hardware"),
        # Carried, never taken here: this also runs on the cached path, where datetime.now()
        # would stamp a 60s-old body as current.
        collected_at=resources_data.get("collected_at"),
    )


def resources_for_host(
    client: pylxd.Client,
    node_name: str,
    resources_data: Dict[str, Any],
    gpu: GpuInventory,
    clustered: Optional[bool],
) -> NodeResources:
    """What this host has, whether it is one machine or a cluster of them.

    important: the one place that decision is made, so GET /nodes and
    GET /nodes/<name>/resources report the same capacity for a clustered host. A failed
    aggregation falls back to this host's own reading rather than to nothing, as
    check_node_status does.
    """
    if clustered:
        aggregated = aggregate_microcloud_resources(
            client=client, node_name=node_name, base_resources=resources_data
        )
        if aggregated is not None:
            return aggregated
        logger.warning(
            "Could not aggregate members of %s; reporting the connected member only",
            node_name,
        )
    return build_node_resources(resources_data, gpu)


def aggregate_microcloud_resources(
    client: pylxd.Client,
    node_name: str,
    base_resources: Dict[str, Any],
) -> Optional[NodeResources]:
    """Sum every online member's resources into one cached NodeResources, or None.

    The name says MicroCloud, but the gate is `server_clustered`, so this serves any clustered
    LXD or Incus host. The name and the cache prefix stay because renaming the prefix would
    invalidate every cached entry.
    """
    try:
        from app.core.cache import (
            CAPACITY_GENERATION_KIND,
            MICROCLOUD_RESOURCES_CACHE_PREFIX,
            get_cache_manager,
            get_resource_list_generation,
        )
        from app.core.config import get_settings
        
        cache = get_cache_manager()
        settings = get_settings()
        cache_key = f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:{node_name}"
        cache_ttl = settings.cache_ttl_lxd_data  # Use configured TTL (default: 60s)
        
        cached_resources = cache.get("lxd_resources", cache_key)
        if cached_resources is not None:
            logger.info(f"Cache HIT for MicroCloud resources: {node_name}")
            return NodeResources(**cached_resources)
        capacity_generation = get_resource_list_generation(
            cache,
            CAPACITY_GENERATION_KIND,
        )
        
        logger.info(f"Cache MISS for MicroCloud resources: {node_name}, aggregating from members")
        
        host_info = client.host_info
        environment = host_info.get("environment", {})
        is_clustered = environment.get("server_clustered", False)
        
        if not is_clustered:
            logger.warning(f"{node_name} is not clustered, cannot aggregate resources")
            return None
        
        try:
            if members_read_failed(cache, node_name):
                logger.debug(f"Members of {node_name} failed to answer recently; using the connected member")
                return None
            response_data = read_members_list(cache, client, node_name)
            if response_data is None:
                return None
            
            # Handle different response formats
            if isinstance(response_data, dict):
                members_data = response_data.get("metadata", [])
            elif isinstance(response_data, list):
                members_data = response_data
            else:
                logger.warning(f"Unexpected cluster members response format: {type(response_data)}")
                return None
            
            if not members_data:
                logger.warning(f"No cluster members found for {node_name}")
                return None
            
            online_members = [m for m in members_data if isinstance(m, dict) and m.get("status") == "Online"]
            
            if not online_members:
                logger.warning(f"No online members found in cluster {node_name}")
                return None
            
            logger.info(f"Found {len(online_members)} online members in MicroCloud cluster {node_name}")
            
            total_cpu_threads = 0
            total_cpu_used = 0
            total_cpu_sockets = 0
            total_memory = 0
            total_memory_used = 0
            cluster_gpu = GpuInventory.empty()
            # One profile per member, folded after the loop. A cluster row must not present
            # one member's motherboard as the cluster's.
            member_hardware = []
            # important: an aggregate is only as fresh as its stalest input, so every
            # member's read time is collected and folded with min() below.
            member_read_times = []
            # Per-member pool readings, folded once at the end so a shared backing store is
            # counted once rather than per member. See services/lxd/storage_model.py.
            pool_readings: List[PoolReading] = []
            degraded_parts: List[str] = []
            
            successful_members = 0

            # Read once for the whole cluster; the member loop below filters it by location.
            try:
                # important: recursion=1, never pylxd's default of 0. At recursion=0 the
                # daemon returns URL strings and pylxd builds Instance objects carrying only a
                # name, so the `.location`, `.status` and `.config` reads below each fall into
                # Model.__getattribute__ -> sync() -> one GET per instance. That is invisible
                # at the call site and scales with the whole cluster.
                cluster_instances, instances_complete = list_instance_records(client)
                if not instances_complete:
                    # A partial read is not a smaller cluster, so the part is marked degraded.
                    degraded_parts.append("instances")
            except Exception as inst_err:
                logger.debug(f"Could not list instances for {node_name}: {inst_err}")
                cluster_instances = []
                # important: an empty list is indistinguishable from an idle cluster, so every
                # derived number (cpu_used, gpu_used, gpu_available) would report the whole
                # cluster as free capacity. Mark the part degraded instead.
                degraded_parts.append("instances")
            
            # The member this client is connected to.
            _current_server = environment.get("server_name", "")
            
            # important: read side by side and folded here in the members' order, so totals and
            # card indices do not depend on which member answers first (see member_reads.py).
            member_names = [member.get("server_name", "unknown") for member in online_members]
            for read in read_members(client, node_name, member_names):
                member_name = read.name
                if read.error is not None:
                    logger.warning(f"Could not get state for member {member_name}: {read.error}")
                    continue
                try:
                    member_read_times.append(read.read_at)
                    member_resources = read.resources

                    cpu_info = member_resources.get("cpu", {})
                    member_cpu_total = cpu_info.get("total", 0)

                    memory_info = member_resources.get("memory", {})
                    member_memory_total = memory_info.get("total", 0)
                    member_memory_used = memory_info.get("used", 0)

                    # GPU capacity and allocation for this member, scoped by
                    # instance.location so one member's consumers are never counted
                    # against another member's cards.
                    member_gpu_inv = build_gpu_inventory(
                        member_resources, cluster_instances, member=member_name
                    )
                    member_hardware.append(
                        build_hardware_profile(member_resources, member=member_name)
                    )
                    member_gpu_total = member_gpu_inv.gpu_total
                    member_gpu_used = member_gpu_inv.gpu_used
                    # Card indices stay unique across the cluster.
                    base_index = len(cluster_gpu.cards)
                    for card in member_gpu_inv.cards:
                        card.index = base_index + card.index

                    # important: pool space is not in the /1.0/resources body. Its
                    # `storage.disks` block is physical media, which double counts a mirror
                    # and includes disks that belong to no pool. Space exists only at
                    # /1.0/storage-pools/<pool>/resources?target=<member>, read per member
                    # through the same leaves the member panel uses.
                    pool_readings.extend(read.pools)
                    degraded_parts.extend(read.degraded)

                    # Sockets are summed: each member has its own.
                    cpu_sockets_data = cpu_info.get("sockets", [])
                    cpu_sockets = len(cpu_sockets_data) if isinstance(cpu_sockets_data, list) else 0

                    # CPU allocation of the running instances placed on this member, from the
                    # cluster-wide list above, counted by cpu_allocation (profile-inherited
                    # limits included).
                    member_cpu_used = 0
                    try:
                        member_cpu_used = allocated_cpu(cluster_instances, member=member_name).cores
                    except Exception as usage_err:
                        logger.debug(f"Could not calculate usage for member {member_name}: {usage_err}")

                    cluster_gpu = cluster_gpu.merge(member_gpu_inv)
                    total_cpu_threads += member_cpu_total
                    total_cpu_used += member_cpu_used
                    total_cpu_sockets += cpu_sockets
                    total_memory += member_memory_total
                    total_memory_used += member_memory_used

                    successful_members += 1
                    logger.debug(
                        f"Member {member_name}: "
                        f"CPU capacity={member_cpu_total} hardware threads "
                        f"({member_cpu_used} vCPUs allocated), "
                        f"Memory={member_memory_total / 1024 / 1024 / 1024:.1f} GiB "
                        f"({member_memory_used / 1024 / 1024 / 1024:.1f} GiB used), "
                        f"GPU={member_gpu_total} compute "
                        f"of {member_gpu_inv.gpu_cards_total} cards (used={member_gpu_used})"
                    )

                except Exception as member_api_err:
                    logger.warning(f"Could not get state for member {member_name}: {member_api_err}")
                    continue

            if successful_members == 0:
                logger.warning(f"No member resources could be aggregated for {node_name}")
                return None
            
            storage = summarise_cluster_storage(pool_readings)
            if storage.unknown_drivers:
                logger.info(
                    "Unclassified storage driver(s) on %s: %s. Counted per member, which "
                    "understates a shared pool rather than overstating it.",
                    node_name,
                    ", ".join(storage.unknown_drivers),
                )
            
            aggregated = NodeResources(
                cpu_total=total_cpu_threads,
                cpu_used=total_cpu_used,
                cpu_available=clamp_available(total_cpu_threads, total_cpu_used),
                cpu_allocated=total_cpu_used,
                cpu_allocation_ratio=allocation_ratio(total_cpu_used, total_cpu_threads),
                cpu_overcommitted=total_cpu_used > total_cpu_threads,
                cpu_sockets=total_cpu_sockets,
                memory_total=total_memory,
                memory_used=total_memory_used,
                memory_available=total_memory - total_memory_used,
                gpu_total=cluster_gpu.gpu_total,
                gpu_used=cluster_gpu.gpu_used,
                gpu_available=cluster_gpu.gpu_available,
                gpu_cards=cluster_gpu.cards,
                gpu_cards_total=cluster_gpu.gpu_cards_total,
                gpu_integrated_total=cluster_gpu.gpu_integrated_total,
                gpu_display_total=cluster_gpu.gpu_display_total,
                gpu_unknown_total=cluster_gpu.gpu_unknown_total,
                gpu_vendors=cluster_gpu.vendors,
                gpu_models=cluster_gpu.models,
                gpu_utilization_supported=cluster_gpu.utilization_supported,
                storage_total=storage.total,
                storage_used=storage.used,
                storage_available=storage.available,
                storage_raw_total=storage.raw_total,
                storage_raw_used=storage.raw_used,
                degraded=bool(degraded_parts),
                degraded_parts=sorted(set(degraded_parts)),
                storage_pools=storage.pools,
                hardware=merge_hardware_profiles(member_hardware),
                collected_at=min(member_read_times) if member_read_times else None,
            )
            
            logger.info(
                f"Successfully aggregated resources from {successful_members}/{len(online_members)} online members in {node_name}: "
                f"CPU capacity={total_cpu_threads} hardware threads "
                f"({total_cpu_used} vCPUs allocated), "
                f"Memory={total_memory / 1024 / 1024 / 1024:.1f} GiB "
                f"({total_memory_used / 1024 / 1024 / 1024:.1f} GiB used), "
                f"GPU={cluster_gpu.gpu_total} compute of "
                f"{cluster_gpu.gpu_cards_total} cards (used={cluster_gpu.gpu_used})"
            )
            
            # Cached only when capacity did not change during the read.
            if (
                get_resource_list_generation(cache, CAPACITY_GENERATION_KIND)
                == capacity_generation
            ):
                cache.set("lxd_resources", cache_key, aggregated.model_dump(), ttl=cache_ttl)
                logger.debug(f"Cached MicroCloud resources for {node_name} (TTL: {cache_ttl}s)")
            else:
                logger.info("Not caching MicroCloud resources: capacity changed during the read")
            
            return aggregated
            
        except Exception as cluster_err:
            logger.error(f"Failed to fetch cluster members for {node_name}: {cluster_err}")
            return None
        
    except Exception as e:
        logger.error(f"Error aggregating MicroCloud resources: {e}")
        return None
