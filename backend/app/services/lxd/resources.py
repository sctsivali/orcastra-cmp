"""
Resource management and metrics operations.
Handles resource usage tracking, node resources, and instance metrics.
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import time
import pylxd

from app.models.schemas import (
    ResourceUsage,
    NodeResources,
    NodeStatus,
    InstanceMetrics,
    GPUCardInfo,
    GPUDRMInfo,
    GPUNvidiaInfo,
    GPUSRIOVInfo,
    GPUMdevProfile,
)
from app.core.node_metadata import get_node_metadata_store
from app.core.type_utils import detect_cluster_type_from_metadata, normalize_node_type
from .utils import parse_vm_status, find_instance_in_projects
from .exceptions import LXDOperationError

logger = logging.getLogger(__name__)


class ResourceManager:
    """
    Manages resource tracking and metrics for LXD instances and nodes.
    """
    
    def __init__(self):
        # CPU usage tracking: {"host:instance": {"usage_ns": int, "timestamp": float}}
        self._cpu_history: Dict[str, Dict[str, any]] = {}
    
    def _parse_gpu_cards(
        self, 
        gpu_cards_raw: list, 
        gpu_usage_map: Dict[str, str] = None
    ) -> list[GPUCardInfo]:
        """
        Parse detailed GPU card information from LXD resources API.
        """
        if not gpu_cards_raw:
            return []
        
        if gpu_usage_map is None:
            gpu_usage_map = {}
        
        gpu_cards = []
        for index, card in enumerate(gpu_cards_raw):
            try:
                # Parse DRM info
                drm_info = None
                drm_raw = card.get("drm")
                if drm_raw:
                    drm_info = GPUDRMInfo(
                        id=drm_raw.get("id"),
                        card_name=drm_raw.get("card_name"),
                        card_device=drm_raw.get("card_device"),
                        control_name=drm_raw.get("control_name"),
                        control_device=drm_raw.get("control_device"),
                        render_name=drm_raw.get("render_name"),
                        render_device=drm_raw.get("render_device"),
                    )
                
                # Parse NVIDIA-specific info
                nvidia_info = None
                nvidia_raw = card.get("nvidia")
                if nvidia_raw:
                    nvidia_info = GPUNvidiaInfo(
                        cuda_version=nvidia_raw.get("cuda_version"),
                        nvrm_version=nvidia_raw.get("nvrm_version"),
                        brand=nvidia_raw.get("brand"),
                        model=nvidia_raw.get("model"),
                        uuid=nvidia_raw.get("uuid"),
                        architecture=nvidia_raw.get("architecture"),
                        card_name=nvidia_raw.get("card_name"),
                        card_device=nvidia_raw.get("card_device"),
                    )
                
                # Parse SR-IOV info
                sriov_info = None
                sriov_raw = card.get("sriov")
                if sriov_raw:
                    vfs = sriov_raw.get("vfs", [])
                    sriov_info = GPUSRIOVInfo(
                        current_vfs=sriov_raw.get("current_vfs", 0),
                        maximum_vfs=sriov_raw.get("maximum_vfs", 0),
                        vfs_count=len(vfs) if isinstance(vfs, list) else 0,
                    )
                
                # Parse mdev profiles
                mdev_profiles = []
                mdev_raw = card.get("mdev")
                if mdev_raw and isinstance(mdev_raw, dict):
                    for profile_name, profile_data in mdev_raw.items():
                        if isinstance(profile_data, dict):
                            mdev_profiles.append(GPUMdevProfile(
                                name=profile_data.get("name", profile_name),
                                api=profile_data.get("api"),
                                available=profile_data.get("available", 0),
                                description=profile_data.get("description"),
                                devices=profile_data.get("devices", []),
                            ))
                
                # Determine if GPU is in use
                pci_address = card.get("pci_address", "")
                usb_address = card.get("usb_address", "")
                nvidia_uuid = nvidia_raw.get("uuid") if nvidia_raw else None
                
                # Check usage by various identifiers
                in_use = False
                used_by = None
                for identifier in [pci_address, usb_address, nvidia_uuid, str(index)]:
                    if identifier and identifier in gpu_usage_map:
                        in_use = True
                        used_by = gpu_usage_map[identifier]
                        break
                
                gpu_card = GPUCardInfo(
                    index=index,
                    driver=card.get("driver"),
                    driver_version=card.get("driver_version"),
                    pci_address=pci_address or None,
                    usb_address=usb_address or None,
                    vendor=card.get("vendor"),
                    vendor_id=card.get("vendor_id"),
                    product=card.get("product"),
                    product_id=card.get("product_id"),
                    numa_node=card.get("numa_node"),
                    in_use=in_use,
                    used_by=used_by,
                    drm=drm_info,
                    nvidia=nvidia_info,
                    sriov=sriov_info,
                    mdev_profiles=mdev_profiles,
                )
                gpu_cards.append(gpu_card)
                
            except Exception as e:
                logger.warning(f"Failed to parse GPU card {index}: {e}")
                # Add a minimal entry for failed cards
                gpu_cards.append(GPUCardInfo(
                    index=index,
                    vendor=card.get("vendor", "Unknown"),
                    product=card.get("product", "Unknown GPU"),
                ))
        
        return gpu_cards
    
    def _get_gpu_usage_map(self, client: pylxd.Client) -> Dict[str, str]:
        """
        Build a mapping of GPU identifiers to instance names.
        """
        usage_map = {}
        try:
            instances = client.instances.all()
            for instance in instances:
                if instance.status == "Running":
                    devices = getattr(instance, 'devices', {}) or {}
                    for device_name, device_config in devices.items():
                        if device_config.get('type') == 'gpu':
                            # Map various GPU identifiers to this instance
                            pci_address = device_config.get('pci')
                            gpu_id = device_config.get('id')
                            gpu_vendorid = device_config.get('vendorid')
                            gpu_productid = device_config.get('productid')
                            
                            if pci_address:
                                usage_map[pci_address] = instance.name
                            if gpu_id:
                                usage_map[gpu_id] = instance.name
                            # Also track by vendor:product combo
                            if gpu_vendorid and gpu_productid:
                                usage_map[f"{gpu_vendorid}:{gpu_productid}"] = instance.name
        except Exception as e:
            logger.debug(f"Could not build GPU usage map: {e}")
        
        return usage_map

    def get_resource_usage(self, instance) -> Optional[ResourceUsage]:
        """Get resource usage for a running instance."""
        try:
            if instance.status != "Running":
                return None
            
            state = instance.state()
            if not state:
                return None
            
            resources = ResourceUsage()
            
            # CPU usage
            if state.cpu:
                resources.cpu_usage = state.cpu.get("usage")
            
            # Memory usage
            if state.memory:
                resources.memory_usage = state.memory.get("usage")
                resources.memory_limit = state.memory.get("usage_peak")
            
            # Disk usage
            if state.disk:
                resources.disk_usage = {
                    k: v.get("usage", 0) for k, v in state.disk.items()
                }
            
            # Network counters
            if state.network:
                resources.network_counters = {}
                for iface, data in state.network.items():
                    if iface != "lo":
                        resources.network_counters[iface] = {
                            "bytes_received": data.get("counters", {}).get(
                                "bytes_received", 0
                            ),
                            "bytes_sent": data.get("counters", {}).get(
                                "bytes_sent", 0
                            ),
                            "packets_received": data.get("counters", {}).get(
                                "packets_received", 0
                            ),
                            "packets_sent": data.get("counters", {}).get(
                                "packets_sent", 0
                            ),
                        }
            
            return resources
            
        except Exception as e:
            logger.debug(f"Could not get resources for {instance.name}: {e}")
            return None
    
    def get_node_resources(self, client: pylxd.Client, node_name: str) -> Dict[str, Any]:
        """Get available (unused) resources for a specific node."""
        try:
            # get system resources
            resources_data = client.resources
            
            # extract CPU info
            cpu_info = resources_data.get("cpu", {})
            cpu_total = cpu_info.get("total", 0)
            cpu_sockets = len(cpu_info.get("sockets", []))
            
            # extract memory info
            memory_info = resources_data.get("memory", {})
            memory_total = memory_info.get("total", 0)
            memory_used = memory_info.get("used", 0)
            memory_available = memory_total - memory_used
            
            # get all running instances to calculate CPU usage
            try:
                instances = client.instances.all()
                cpu_used = 0
                for instance in instances:
                    if instance.status == "Running":
                        # count CPU limits from instance config
                        config = instance.config or {}
                        limits_cpu = config.get("limits.cpu")
                        if limits_cpu:
                            try:
                                cpu_used += int(limits_cpu)
                            except:
                                pass
                cpu_available = cpu_total - cpu_used
                logger.debug(f"CPU on {node_name}: {cpu_total} total, {cpu_used} used, {cpu_available} available")
            except Exception as e:
                logger.warning(f"Could not calculate CPU usage for {node_name}: {e}")
                cpu_available = cpu_total
            
            # get storage pools
            storage_pools = []
            try:
                pools = client.storage_pools.all()
                logger.debug(f"Found {len(pools)} storage pools on {node_name}")
                for pool in pools:
                    try:
                        # use API directly to get pool resources
                        pool_resources_response = client.api.storage_pools[pool.name].resources.get()
                        pool_resources = pool_resources_response.json().get("metadata", {})
                        space_info = pool_resources.get("space", {})
                        space_total = space_info.get("total", 0)
                        space_used = space_info.get("used", 0)
                        space_available = space_total - space_used
                        logger.debug(f"Pool {pool.name}: {space_total} bytes total, {space_used} bytes used, {space_available} available")
                        storage_pools.append({
                            "name": pool.name,
                            "driver": pool.driver,
                            "space_total": space_total,
                            "space_used": space_used,
                            "space_available": space_available,
                        })
                    except Exception as pool_err:
                        logger.warning(f"Could not get resources for pool {pool.name}: {pool_err}")
            except Exception as e:
                logger.warning(f"Could not get storage pools for {node_name}: {e}")
            
            # calculate available storage
            storage_available = sum(p.get("space_available", 0) for p in storage_pools)
            logger.debug(f"Storage for {node_name}: {storage_available} bytes available ({len(storage_pools)} pools)")
            
            return {
                "cpu_total": cpu_total,
                "cpu_available": cpu_available,
                "cpu_sockets": cpu_sockets,
                "memory_total": memory_total,
                "memory_available": memory_available,
                "storage_total": sum(p.get("space_total", 0) for p in storage_pools),
                "storage_available": storage_available,
                "storage_pools": storage_pools,
            }
            
        except Exception as e:
            logger.error(f"Error getting resources for {node_name}: {e}")
            raise LXDOperationError(f"Failed to get node resources: {str(e)}")
    
    def check_node_status(self, client: pylxd.Client, node_name: str, endpoint: str) -> NodeStatus:
        """Check connection status and resources for a specific node."""
        # Get display name from metadata store
        metadata_store = get_node_metadata_store()
        display_name = metadata_store.get_node_display_name(node_name)
        
        status = NodeStatus(
            name=node_name,
            endpoint=endpoint,
            connected=False,
            display_name=display_name,
        )
        
        try:
            host_info = client.host_info
            
            # Don't set connected=True yet, need to verify we can actually access resources
            # Connection successful but need to check if authenticated
            status.server_version = host_info.get("environment", {}).get(
                "server_version"
            )
            status.api_version = host_info.get("api_version")
            status.auth = host_info.get("auth")
            status.environment = host_info.get("environment")
            
            # Parse tags from server config
            # Use direct API call to get fresh data (avoid pylxd caching)
            try:
                response = client.api.get()
                server_data = response.json()["metadata"]
                server_config = server_data.get("config", {})
                tags_str = server_config.get("user.tags", "")
                tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
                status.tags = tags
                logger.debug(f"Loaded tags for {node_name}: {tags}")
            except Exception as tag_err:
                logger.debug(f"Could not get tags for {node_name}: {tag_err}")
                status.tags = []
            
            # Detect node type (microcloud vs lxd)
            # Only detect if authenticated (trusted), otherwise leave as None
            auth_status = host_info.get("auth", "")
            if auth_status == "trusted":
                try:
                    # Get metadata to check if type was already determined during registration
                    metadata_store = get_node_metadata_store()
                    all_metadata = metadata_store.get_all_node_metadata()
                    node_metadata = all_metadata.get(node_name, {})
                    
                    # If type was stored in metadata during registration, use it
                    if "type" in node_metadata:
                        status.node_type = normalize_node_type(node_metadata["type"])
                        logger.debug(f"Node type from metadata for {node_name}: {status.node_type}")
                    else:
                        # Fallback to environment-based detection using centralized logic
                        detected_type = detect_cluster_type_from_metadata(host_info)
                        status.node_type = normalize_node_type(detected_type)
                        environment = host_info.get('environment', {})
                        logger.info(
                            f"Node type detected for {node_name}: {status.node_type} "
                            f"(server_clustered={environment.get('server_clustered', False)}, "
                            f"server={environment.get('server', 'N/A')}, "
                            f"server_name={environment.get('server_name', 'N/A')})"
                        )
                except Exception as type_err:
                    logger.warning(f"Could not determine node type for {node_name}: {type_err}")
                    status.node_type = None  # Don't fallback to "lxd", keep as None
            else:
                # Not trusted/authenticated - don't detect type
                logger.info(f"Node {node_name} not trusted (auth={auth_status}), skipping type detection")
                status.node_type = None
            
            # Get node resources
            try:
                resources_data = self.get_node_resources(client, node_name)
                
                # Get GPU information with detailed card info
                gpu_total = 0
                gpu_used = 0
                gpu_cards_detailed = []
                try:
                    raw_resources = client.resources
                    gpu_info = raw_resources.get("gpu", {})
                    gpu_cards_raw = gpu_info.get("cards", [])
                    gpu_total = len(gpu_cards_raw)
                    
                    # Build GPU usage map from running instances
                    gpu_usage_map = self._get_gpu_usage_map(client)
                    gpu_used = len(gpu_usage_map)
                    
                    # Parse detailed GPU card information
                    gpu_cards_detailed = self._parse_gpu_cards(gpu_cards_raw, gpu_usage_map)
                    
                except Exception as gpu_err:
                    logger.debug(f"Could not get GPU info for {node_name}: {gpu_err}")
                
                # If this is a MicroCloud cluster, aggregate resources from all members
                if status.node_type == "microcloud":
                    logger.info(f"Detected MicroCloud cluster {node_name}, aggregating resources from all members...")
                    try:
                        aggregated_resources = self._aggregate_microcloud_resources(
                            client=client,
                            node_name=node_name,
                            base_resources=resources_data,
                            base_gpu_total=gpu_total,
                            base_gpu_used=gpu_used
                        )
                        if aggregated_resources:
                            status.resources = aggregated_resources
                            logger.info(f"MicroCloud {node_name} aggregated resources: "
                                      f"CPU={aggregated_resources.cpu_total} cores, "
                                      f"RAM={aggregated_resources.memory_total / 1024 / 1024 / 1024:.1f} GB, "
                                      f"GPU={aggregated_resources.gpu_total} cards")
                        else:
                            # Fallback to single node resources if aggregation fails
                            status.resources = NodeResources(
                                cpu_total=resources_data.get("cpu_total", 0),
                                cpu_used=resources_data.get("cpu_total", 0) - resources_data.get("cpu_available", 0),
                                cpu_available=resources_data.get("cpu_available", 0),
                                cpu_sockets=resources_data.get("cpu_sockets", 0),
                                memory_total=resources_data.get("memory_total", 0),
                                memory_used=resources_data.get("memory_total", 0) - resources_data.get("memory_available", 0),
                                memory_available=resources_data.get("memory_available", 0),
                                gpu_total=gpu_total,
                                gpu_used=gpu_used,
                                gpu_available=gpu_total - gpu_used,
                                gpu_cards=gpu_cards_detailed,
                                storage_total=resources_data.get("storage_total", 0),
                                storage_available=resources_data.get("storage_available", 0),
                                storage_pools=resources_data.get("storage_pools", []),
                            )
                    except Exception as agg_err:
                        logger.warning(f"Failed to aggregate MicroCloud resources: {agg_err}")
                        # Fallback to single node resources
                        status.resources = NodeResources(
                            cpu_total=resources_data.get("cpu_total", 0),
                            cpu_used=resources_data.get("cpu_total", 0) - resources_data.get("cpu_available", 0),
                            cpu_available=resources_data.get("cpu_available", 0),
                            cpu_sockets=resources_data.get("cpu_sockets", 0),
                            memory_total=resources_data.get("memory_total", 0),
                            memory_used=resources_data.get("memory_total", 0) - resources_data.get("memory_available", 0),
                            memory_available=resources_data.get("memory_available", 0),
                            gpu_total=gpu_total,
                            gpu_used=gpu_used,
                            gpu_available=gpu_total - gpu_used,
                            gpu_cards=gpu_cards_detailed,
                            storage_total=resources_data.get("storage_total", 0),
                            storage_available=resources_data.get("storage_available", 0),
                            storage_pools=resources_data.get("storage_pools", []),
                        )
                else:
                    # Standard LXD node - use single node resources
                    status.resources = NodeResources(
                        cpu_total=resources_data.get("cpu_total", 0),
                        cpu_used=resources_data.get("cpu_total", 0) - resources_data.get("cpu_available", 0),
                        cpu_available=resources_data.get("cpu_available", 0),
                        cpu_sockets=resources_data.get("cpu_sockets", 0),
                        memory_total=resources_data.get("memory_total", 0),
                        memory_used=resources_data.get("memory_total", 0) - resources_data.get("memory_available", 0),
                        memory_available=resources_data.get("memory_available", 0),
                        gpu_total=gpu_total,
                        gpu_used=gpu_used,
                        gpu_available=gpu_total - gpu_used,
                        gpu_cards=gpu_cards_detailed,
                        storage_total=resources_data.get("storage_total", 0),
                        storage_available=resources_data.get("storage_available", 0),
                        storage_pools=resources_data.get("storage_pools", []),
                    )
                # Only set connected=True if we successfully got resources (authenticated)
                status.connected = True
            except Exception as res_err:
                # Failed to get resources, likely not authenticated (Forbidden)
                logger.warning(f"Could not get resources for {node_name}: {res_err}")
                status.connected = False
                status.error = f"Not authorized: {str(res_err)}"
        
        except Exception as e:
            status.error = str(e)
            status.connected = False
            logger.warning(f"Node {node_name} health check failed: {e}")
        
        return status
    
    def _aggregate_microcloud_resources(
        self,
        client: pylxd.Client,
        node_name: str,
        base_resources: Dict[str, Any],
        base_gpu_total: int,
        base_gpu_used: int
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
            from app.core.cache import get_cache_manager
            from app.core.config import get_settings
            
            cache = get_cache_manager()
            settings = get_settings()
            cache_key = f"microcloud_resources:{node_name}"
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
                total_gpu = 0
                total_gpu_used = 0
                total_storage = 0
                total_storage_available = 0
                storage_pools_map = {}
                all_gpu_cards = []  # Collect all GPU cards from all members
                
                successful_members = 0
                
                # Get current server name to identify which member we're connected to
                current_server = environment.get("server_name", "")
                
                # Iterate through each online member and aggregate resources
                for member_data in online_members:
                    member_name = member_data.get("server_name", "unknown")
                    member_url = member_data.get("url", "")
                    
                    try:
                        # Query member-specific resources via cluster API
                        # In LXD clusters, we need to target specific members using `?target=parameter`
                        # Or we can use `/1.0/resources?target=<member_name>`
                        try:
                            # Get resources for this specific member by targeting it
                            # The LXD API allows targeting specific cluster members
                            member_resources_response = client.api.resources.get(params={"target": member_name})
                            member_resources = member_resources_response.json().get("metadata", {})
                            
                            # Extract CPU resources
                            cpu_info = member_resources.get("cpu", {})
                            member_cpu_total = cpu_info.get("total", 0)
                            
                            # Extract memory resources
                            memory_info = member_resources.get("memory", {})
                            member_memory_total = memory_info.get("total", 0)
                            member_memory_used = memory_info.get("used", 0)
                            
                            # Extract GPU resources
                            gpu_info = member_resources.get("gpu", {})
                            gpu_cards_raw = gpu_info.get("cards", [])
                            member_gpu_total = len(gpu_cards_raw)
                            
                            # Parse detailed GPU cards for this member
                            # Build member-specific GPU usage map
                            member_gpu_usage = {}
                            try:
                                instances = client.instances.all()
                                for inst in instances:
                                    inst_location = getattr(inst, 'location', '')
                                    if inst_location == member_name and inst.status == "Running":
                                        devices = getattr(inst, 'devices', {}) or {}
                                        for dev_name, dev_config in devices.items():
                                            if dev_config.get('type') == 'gpu':
                                                pci = dev_config.get('pci', '')
                                                if pci:
                                                    member_gpu_usage[pci] = inst.name
                            except Exception:
                                pass
                            
                            member_gpu_cards = self._parse_gpu_cards(gpu_cards_raw, member_gpu_usage)
                            # Adjust indices to be unique across cluster
                            base_index = len(all_gpu_cards)
                            for card in member_gpu_cards:
                                card.index = base_index + card.index
                            all_gpu_cards.extend(member_gpu_cards)
                            
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
                            member_gpu_used = 0
                            try:
                                # Get all instances and filter by location (member)
                                instances = client.instances.all()
                                for instance in instances:
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
                                        
                                        # Count GPU
                                        devices = getattr(instance, 'devices', {}) or {}
                                        for device_name, device_config in devices.items():
                                            if device_config.get('type') == 'gpu':
                                                member_gpu_used += 1
                                                break
                            except Exception as usage_err:
                                logger.debug(f"Could not calculate usage for member {member_name}: {usage_err}")
                            
                            # Aggregate totals
                            total_cpu_cores += member_cpu_total
                            total_cpu_used += member_cpu_used
                            total_cpu_sockets += cpu_sockets
                            total_memory += member_memory_total
                            total_memory_used += member_memory_used
                            total_gpu += member_gpu_total
                            total_gpu_used += member_gpu_used
                            total_storage += member_storage_total
                            total_storage_available += member_storage_available
                            
                            successful_members += 1
                            logger.debug(
                                f"Member {member_name}: "
                                f"CPU={member_cpu_total} cores (used={member_cpu_used}), "
                                f"RAM={member_memory_total / 1024 / 1024 / 1024:.1f} GB (used={member_memory_used / 1024 / 1024 / 1024:.1f} GB), "
                                f"GPU={member_gpu_total} cards (used={member_gpu_used})"
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
                    gpu_total=total_gpu,
                    gpu_used=total_gpu_used,
                    gpu_available=total_gpu - total_gpu_used,
                    gpu_cards=all_gpu_cards,
                    storage_total=total_storage,
                    storage_available=total_storage_available,
                    storage_pools=storage_pools_list,
                )
                
                logger.info(
                    f"Successfully aggregated resources from {successful_members}/{len(online_members)} online members in {node_name}: "
                    f"Total CPU={total_cpu_cores} cores (used={total_cpu_used}), "
                    f"RAM={total_memory / 1024 / 1024 / 1024:.1f} GB (used={total_memory_used / 1024 / 1024 / 1024:.1f} GB), "
                    f"GPU={total_gpu} cards (used={total_gpu_used})"
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
    
    def get_instance_metrics(
        self, 
        client: pylxd.Client,
        host: str, 
        instance_name: str,
        project: str = "default"
    ) -> InstanceMetrics:
        """
        Get real-time metrics for a specific instance.
        Returns CPU, memory, disk, network usage with timestamps.
        """
        try:
            # Find instance across all projects
            instance, found_project = find_instance_in_projects(client, instance_name)
            
            if not instance:
                raise LXDOperationError(f"Instance '{instance_name}' not found on {host}")
            
            # Get current state
            state = instance.state()
            
            # Parse status
            status = parse_vm_status(instance.status, instance.status_code)
            
            # Initialize metrics
            metrics = InstanceMetrics(
                instance_name=instance_name,
                host=host,
                status=status,
            )
            
            # Only get metrics for running instances
            if instance.status != "Running" or not state:
                return metrics
            
            # CPU usage - calculate from cumulative nanoseconds delta
            if state.cpu and state.cpu.get("usage"):
                current_usage_ns = state.cpu.get("usage")
                current_time = time.time()
                
                cache_key = f"{host}:{instance_name}"
                
                if cache_key in self._cpu_history:
                    # Calculate delta
                    prev_data = self._cpu_history[cache_key]
                    prev_usage_ns = prev_data["usage_ns"]
                    prev_time = prev_data["timestamp"]
                    
                    time_delta = current_time - prev_time
                    usage_delta_ns = current_usage_ns - prev_usage_ns
                    
                    # Calculate percentage
                    if time_delta > 0:
                        cpu_percent = (usage_delta_ns / (time_delta * 1e9)) * 100
                        metrics.cpu_usage_percent = round(min(cpu_percent, 800.0), 2)
                
                # Store current values for next calculation
                self._cpu_history[cache_key] = {
                    "usage_ns": current_usage_ns,
                    "timestamp": current_time
                }
            
            # Memory usage
            if state.memory:
                metrics.memory_used_bytes = state.memory.get("usage", 0)
                metrics.memory_total_bytes = state.memory.get("total", 0)
                if metrics.memory_total_bytes > 0:
                    metrics.memory_usage_percent = round(
                        (metrics.memory_used_bytes / metrics.memory_total_bytes) * 100, 2
                    )
            
            # Disk usage
            if state.disk:
                metrics.disk_usage_bytes = {
                    device: data.get("usage", 0)
                    for device, data in state.disk.items()
                }
            
            # Network counters
            if state.network:
                for iface_name, iface_data in state.network.items():
                    if iface_name == "lo":
                        continue
                    counters = iface_data.get("counters", {})
                    metrics.network_rx_bytes[iface_name] = counters.get("bytes_received", 0)
                    metrics.network_tx_bytes[iface_name] = counters.get("bytes_sent", 0)
            
            # Process count
            if state.processes:
                metrics.processes = state.processes
            
            # PID
            if hasattr(state, 'pid') and state.pid:
                metrics.pid = state.pid
            
            # Uptime
            try:
                if hasattr(state, 'started_at') and state.started_at:
                    started_at_str = state.started_at
                    if started_at_str.endswith('Z'):
                        started_at_str = started_at_str[:-1] + '+00:00'
                    started_at = datetime.fromisoformat(started_at_str)
                    now = datetime.now(timezone.utc)
                    uptime_delta = now - started_at
                    metrics.uptime_seconds = int(uptime_delta.total_seconds())
                else:
                    if instance.last_used_at and instance.status == "Running":
                        last_used_str = instance.last_used_at
                        if last_used_str.endswith('Z'):
                            last_used_str = last_used_str[:-1] + '+00:00'
                        last_used = datetime.fromisoformat(last_used_str)
                        now = datetime.now(timezone.utc)
                        uptime_delta = now - last_used
                        metrics.uptime_seconds = int(uptime_delta.total_seconds())
            except Exception as e:
                logger.warning(f"Could not calculate uptime for {instance_name}: {e}")
            
            # Base image
            if instance.config and instance.config.get('image.description'):
                metrics.base_image = instance.config.get('image.description')
            elif instance.config and instance.config.get('volatile.base_image'):
                metrics.base_image = instance.config.get('volatile.base_image')
            
            # IP addresses
            if state.network:
                for iface_name, iface_data in state.network.items():
                    if iface_name == "lo":
                        continue
                    for addr in iface_data.get("addresses", []):
                        ip = addr.get("address")
                        family = addr.get("family")
                        if family == "inet":
                            metrics.ipv4_addresses.append(ip)
                        elif family == "inet6" and not ip.startswith("fe80"):
                            metrics.ipv6_addresses.append(ip)
            
            return metrics
            
        except LXDOperationError:
            raise
        except Exception as e:
            logger.error(f"Error getting metrics for '{instance_name}' on {host}: {e}")
            raise LXDOperationError(f"Failed to get metrics: {str(e)}")
