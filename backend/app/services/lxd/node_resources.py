"""
How a node's resource picture is assembled.

Single-host resources and MicroCloud cluster aggregation both end up as one NodeResources,
built here so a new field can never be added to one path and forgotten on another.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pylxd

from app.models.schemas import NodeResources

from .gpu import GpuInventory, build_gpu_inventory
from .hardware import build_hardware_profile, merge_hardware_profiles

from app.services.lxd.project_scope import list_instance_records

logger = logging.getLogger(__name__)


def build_node_resources(resources_data: Dict[str, Any], gpu: GpuInventory) -> NodeResources:
    """Build NodeResources for one host from a resource dict and its GPU inventory.

    important: the only place NodeResources is constructed for a node. Three separate call
    sites used to build it by hand and two of them quietly omitted gpu_cards_total, so the
    node view reported zero detected cards on hardware that had two.
    """
    cpu_total = resources_data.get("cpu_total", 0)
    memory_total = resources_data.get("memory_total", 0)
    return NodeResources(
        cpu_total=cpu_total,
        cpu_used=cpu_total - resources_data.get("cpu_available", 0),
        cpu_available=resources_data.get("cpu_available", 0),
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
        storage_available=resources_data.get("storage_available", 0),
        storage_pools=resources_data.get("storage_pools", []),
        hardware=resources_data.get("hardware"),
        # note: carried, never minted here. This function runs on the cached path too, and a
        # datetime.now() at this line would stamp a 60s-old body as current.
        collected_at=resources_data.get("collected_at"),
    )


def aggregate_microcloud_resources(
    client: pylxd.Client,
    node_name: str,
    base_resources: Dict[str, Any],
) -> Optional[NodeResources]:
    """
    Aggregate resources from all MicroCloud cluster members.
    Returns NodeResources with total resources from all members.
    
    This method queries each cluster member and sums up their individual resources
    to give a complete picture of the entire MicroCloud cluster capacity.
    
    Results are cached to minimize API calls to cluster members.
    """
    try:
        # Try to get from cache first
        from app.core.cache import get_cache_manager, MICROCLOUD_RESOURCES_CACHE_PREFIX
        from app.core.config import get_settings
        
        cache = get_cache_manager()
        settings = get_settings()
        cache_key = f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:{node_name}"
        cache_ttl = settings.cache_ttl_lxd_data  # Use configured TTL (default: 60s)
        
        cached_resources = cache.get("lxd_resources", cache_key)
        if cached_resources is not None:
            logger.info(f"Cache HIT for MicroCloud resources: {node_name}")
            return NodeResources(**cached_resources)
        
        logger.info(f"Cache MISS for MicroCloud resources: {node_name}, aggregating from members")
        
        # Check if node is clustered
        host_info = client.host_info
        environment = host_info.get("environment", {})
        is_clustered = environment.get("server_clustered", False)
        
        if not is_clustered:
            logger.warning(f"{node_name} is not clustered, cannot aggregate resources")
            return None
        
        # Get cluster members using LXD API
        try:
            response = client.api.cluster.members.get(params={"recursion": 1})
            response_data = response.json()
            
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
            
            # Filter only online members
            online_members = [m for m in members_data if isinstance(m, dict) and m.get("status") == "Online"]
            
            if not online_members:
                logger.warning(f"No online members found in cluster {node_name}")
                return None
            
            logger.info(f"Found {len(online_members)} online members in MicroCloud cluster {node_name}")
            
            # Initialize aggregated counters
            total_cpu_cores = 0
            total_cpu_used = 0
            total_cpu_sockets = 0
            total_memory = 0
            total_memory_used = 0
            cluster_gpu = GpuInventory.empty()
            # One profile per member, folded after the loop. A cluster row must not present
            # one member's motherboard as the cluster's.
            member_hardware = []
            # important: an aggregate is only as fresh as its stalest input, so this
            # collects every member's read time and folds with min() below. Stamping
            # now() here would report a cluster as current on the strength of whichever
            # member happened to answer last.
            member_read_times = []
            total_storage = 0
            total_storage_available = 0
            storage_pools_map = {}
            
            successful_members = 0

            # important: fetched once for the whole cluster. The per-member loop used to
            # call instances.all() twice per member, so a ten-member cluster made twenty
            # identical round trips to build one number.
            # note: every project, not just default. The pooled client is bound to default,
            # so this counted a fraction of a busy cluster and fed that fraction to the GPU
            # allocation below it.
            try:
                cluster_instances, instances_complete = list_instance_records(client)
                if not instances_complete:
                    # This branch has no field to carry a partial read, so say it loudly here.
                    # The numbers below are built from fewer instances than the cluster holds.
                    logger.warning(
                        "Instance read for %s was partial; allocation figures understate usage",
                        node_name,
                    )
            except Exception as inst_err:
                logger.warning(f"Could not list instances for {node_name}: {inst_err}")
                # important: do NOT carry on with an empty list. Every figure derived below
                # comes from this read, so an empty one reports cpu_used 0, gpu_used 0 and
                # every card available: a cluster whose read failed looks like a cluster with
                # nothing running on it, and that is the one direction a capacity figure must
                # never be wrong in. Returning None makes the caller fall back to this member's
                # real resources, which under-reports the total and is visibly conservative
                # rather than invisibly wrong.
                return None
            
            # Get current server name to identify which member we're connected to
            _current_server = environment.get("server_name", "")
            
            # Iterate through each online member and aggregate resources
            for member_data in online_members:
                member_name = member_data.get("server_name", "unknown")
                _member_url = member_data.get("url", "")
                
                try:
                    # Query member-specific resources via cluster API
                    # In LXD clusters, we need to target specific members using `?target=parameter`
                    # Or we can use `/1.0/resources?target=<member_name>`
                    try:
                        # Get resources for this specific member by targeting it
                        # The LXD API allows targeting specific cluster members
                        member_resources_response = client.api.resources.get(params={"target": member_name})
                        member_read_times.append(datetime.now(timezone.utc))
                        member_resources = member_resources_response.json().get("metadata", {})
                        
                        # Extract CPU resources
                        cpu_info = member_resources.get("cpu", {})
                        member_cpu_total = cpu_info.get("total", 0)
                        
                        # Extract memory resources
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
                        # Card indices stay unique across the cluster
                        base_index = len(cluster_gpu.cards)
                        for card in member_gpu_inv.cards:
                            card.index = base_index + card.index
                        
                        # Extract storage resources from storage pools
                        storage_info = member_resources.get("storage", {})
                        storage_pools_raw = storage_info.get("pools", [])
                        
                        member_storage_total = 0
                        member_storage_available = 0
                        
                        for pool in storage_pools_raw:
                            pool_name = pool.get("name", "")
                            pool_driver = pool.get("driver", "unknown")
                            space = pool.get("space", {})
                            space_total = space.get("total", 0)
                            space_used = space.get("used", 0)
                            space_available = space_total - space_used
                            
                            member_storage_total += space_total
                            member_storage_available += space_available
                            
                            # Aggregate storage pools
                            pool_key = f"{pool_name}_{pool_driver}"
                            if pool_key not in storage_pools_map:
                                storage_pools_map[pool_key] = {
                                    "name": pool_name,
                                    "driver": pool_driver,
                                    "space_total": 0,
                                    "space_used": 0,
                                    "space_available": 0,
                                }
                            storage_pools_map[pool_key]["space_total"] += space_total
                            storage_pools_map[pool_key]["space_used"] += space_used
                            storage_pools_map[pool_key]["space_available"] += space_available
                        
                        # For CPU sockets, we'll sum them up (each member has its own sockets)
                        cpu_sockets_data = cpu_info.get("sockets", [])
                        cpu_sockets = len(cpu_sockets_data) if isinstance(cpu_sockets_data, list) else 0
                        
                        # Get CPU usage by counting instances on this member
                        member_cpu_used = 0
                        try:
                            # Reuse the cluster-wide list fetched once above rather than
                            # re-listing every instance for every member.
                            for instance in cluster_instances:
                                # Check if instance is on this specific member
                                instance_location = getattr(instance, 'location', '')
                                if instance_location == member_name and instance.status == "Running":
                                    # Count CPU
                                    config = getattr(instance, 'config', {}) or {}
                                    limits_cpu = config.get('limits.cpu')
                                    if limits_cpu:
                                        try:
                                            if '-' in str(limits_cpu):
                                                # Range like "0-3"
                                                parts = str(limits_cpu).split('-')
                                                member_cpu_used += int(parts[1]) - int(parts[0]) + 1
                                            elif ',' in str(limits_cpu):
                                                # List like "0,1,2"
                                                member_cpu_used += len(str(limits_cpu).split(','))
                                            else:
                                                # Single number
                                                member_cpu_used += int(limits_cpu)
                                        except:
                                            member_cpu_used += 1
                                    else:
                                        member_cpu_used += 1
                                    
                        except Exception as usage_err:
                            logger.debug(f"Could not calculate usage for member {member_name}: {usage_err}")
                        
                        # Aggregate totals
                        cluster_gpu = cluster_gpu.merge(member_gpu_inv)
                        total_cpu_cores += member_cpu_total
                        total_cpu_used += member_cpu_used
                        total_cpu_sockets += cpu_sockets
                        total_memory += member_memory_total
                        total_memory_used += member_memory_used
                        total_storage += member_storage_total
                        total_storage_available += member_storage_available
                        
                        successful_members += 1
                        logger.debug(
                            f"Member {member_name}: "
                            f"CPU={member_cpu_total} cores (used={member_cpu_used}), "
                            f"RAM={member_memory_total / 1024 / 1024 / 1024:.1f} GB (used={member_memory_used / 1024 / 1024 / 1024:.1f} GB), "
                            f"GPU={member_gpu_total} compute "
                            f"of {member_gpu_inv.gpu_cards_total} cards (used={member_gpu_used})"
                        )
                        
                    except Exception as member_api_err:
                        logger.warning(f"Could not get state for member {member_name}: {member_api_err}")
                        continue
                    
                except Exception as member_err:
                    logger.warning(f"Failed to process member {member_name}: {member_err}")
                    continue
            
            if successful_members == 0:
                logger.warning(f"No member resources could be aggregated for {node_name}")
                return None
            
            # Convert storage pools map to list
            storage_pools_list = list(storage_pools_map.values())
            
            # Create aggregated NodeResources
            aggregated = NodeResources(
                cpu_total=total_cpu_cores,
                cpu_used=total_cpu_used,
                cpu_available=total_cpu_cores - total_cpu_used,
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
                storage_total=total_storage,
                storage_available=total_storage_available,
                storage_pools=storage_pools_list,
                hardware=merge_hardware_profiles(member_hardware),
                collected_at=min(member_read_times) if member_read_times else None,
            )
            
            logger.info(
                f"Successfully aggregated resources from {successful_members}/{len(online_members)} online members in {node_name}: "
                f"Total CPU={total_cpu_cores} cores (used={total_cpu_used}), "
                f"RAM={total_memory / 1024 / 1024 / 1024:.1f} GB (used={total_memory_used / 1024 / 1024 / 1024:.1f} GB), "
                f"GPU={cluster_gpu.gpu_total} compute of "
                f"{cluster_gpu.gpu_cards_total} cards (used={cluster_gpu.gpu_used})"
            )
            
            # Cache the aggregated result
            cache.set("lxd_resources", cache_key, aggregated.model_dump(), ttl=cache_ttl)
            logger.debug(f"Cached MicroCloud resources for {node_name} (TTL: {cache_ttl}s)")
            
            return aggregated
            
        except Exception as cluster_err:
            logger.error(f"Failed to fetch cluster members for {node_name}: {cluster_err}")
            return None
        
    except Exception as e:
        logger.error(f"Error aggregating MicroCloud resources: {e}")
        return None
