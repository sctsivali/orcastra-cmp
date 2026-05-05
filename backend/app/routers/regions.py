"""
API Router for Regions/Geographic Overview.
Provides endpoints for viewing nodes grouped by geographic region with hardware summaries.
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any, Optional, Set, Tuple
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import LXDService, get_lxd_service
from app.core.config import get_settings
from app.core.cache import get_cache_manager
from app.models.schemas import (
    GeoLocation,
    ExtendedHardwareInfo,
    RegionNodeInfo,
    RegionSummary,
    RegionsOverviewResponse,
    ClusterStatus,
)

logger = logging.getLogger(__name__)
from app.core.dependencies import require_tenant, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, get_scope_cache_key

router = APIRouter(
    prefix="/regions",
    tags=["Regions"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


def get_cpu_vendor_from_info(cpu_info: Dict[str, Any]) -> Optional[str]:
    """Extract CPU vendor from LXD resources CPU info."""
    sockets = cpu_info.get("sockets", [])
    if sockets:
        # Get vendor from first socket
        socket = sockets[0]
        vendor = socket.get("vendor", "")
        # Normalize vendor names
        vendor_lower = vendor.lower()
        if "intel" in vendor_lower:
            return "Intel"
        elif "amd" in vendor_lower or "advanced micro" in vendor_lower:
            return "AMD"
        elif "apple" in vendor_lower:
            return "Apple"
        elif "arm" in vendor_lower:
            return "ARM"
        elif vendor:
            return vendor
    return None


def get_cpu_model_from_info(cpu_info: Dict[str, Any]) -> Optional[str]:
    """Extract CPU model from LXD resources CPU info."""
    sockets = cpu_info.get("sockets", [])
    if sockets:
        socket = sockets[0]
        return socket.get("name", None)
    return None


def get_storage_types_from_disks(disk_info: Dict[str, Any]) -> List[str]:
    """Extract storage types (NVMe, SATA, SSD, HDD) from LXD resources disk info."""
    storage_types: Set[str] = set()
    
    disks = disk_info.get("disks", [])
    for disk in disks:
        # Check type field
        disk_type = disk.get("type", "").lower()
        
        # Check for NVMe
        if disk_type == "nvme" or "nvme" in disk.get("device", "").lower():
            storage_types.add("NVMe")
        # Check for SATA
        elif disk_type == "sata" or "sd" in disk.get("device", "").lower():
            # Check if rotational (HDD) or not (SSD)
            if disk.get("rotational", False):
                storage_types.add("HDD")
            else:
                storage_types.add("SATA SSD")
        # Check for other types
        elif disk_type == "scsi":
            storage_types.add("SCSI")
        elif disk_type == "virtio":
            storage_types.add("VirtIO")
    
    return list(storage_types) if storage_types else ["Unknown"]


def aggregate_hardware_info(nodes_hardware: List[ExtendedHardwareInfo]) -> ExtendedHardwareInfo:
    """Aggregate hardware info from multiple nodes into a summary."""
    if not nodes_hardware:
        return ExtendedHardwareInfo()
    
    # Collect unique values for lists
    all_gpu_vendors: Set[str] = set()
    all_gpu_models: Set[str] = set()
    all_storage_types: Set[str] = set()
    all_storage_drivers: Set[str] = set()
    all_network_interfaces: Set[str] = set()
    all_cpu_architectures: Set[str] = set()
    all_cpu_vendors: Set[str] = set()
    
    total_cpu = 0
    total_sockets = 0
    total_memory = 0
    total_memory_used = 0
    total_gpu = 0
    total_storage = 0
    total_storage_used = 0
    
    for hw in nodes_hardware:
        total_cpu += hw.cpu_total
        total_sockets += hw.cpu_sockets
        total_memory += hw.memory_total
        total_memory_used += hw.memory_used
        total_gpu += hw.gpu_total
        total_storage += hw.storage_total
        total_storage_used += hw.storage_used
        
        if hw.cpu_architecture:
            all_cpu_architectures.add(hw.cpu_architecture)
        if hw.cpu_vendor:
            all_cpu_vendors.add(hw.cpu_vendor)
        
        all_gpu_vendors.update(hw.gpu_vendors)
        all_gpu_models.update(hw.gpu_models)
        all_storage_types.update(hw.storage_types)
        all_storage_drivers.update(hw.storage_drivers)
        all_network_interfaces.update(hw.network_interfaces)
    
    return ExtendedHardwareInfo(
        cpu_total=total_cpu,
        cpu_sockets=total_sockets,
        cpu_architecture=", ".join(sorted(all_cpu_architectures)) if all_cpu_architectures else "x86_64",
        cpu_vendor=", ".join(sorted(all_cpu_vendors)) if all_cpu_vendors else None,
        cpu_model=None,  # Too varied to aggregate
        memory_total=total_memory,
        memory_used=total_memory_used,
        gpu_total=total_gpu,
        gpu_vendors=list(sorted(all_gpu_vendors)),
        gpu_models=list(sorted(all_gpu_models)),
        storage_total=total_storage,
        storage_used=total_storage_used,
        storage_types=list(sorted(all_storage_types)),
        storage_drivers=list(sorted(all_storage_drivers)),
        network_interfaces=list(sorted(all_network_interfaces)),
    )


@router.get(
    "",
    response_model=RegionsOverviewResponse,
    summary="Get regions overview",
    description="""
    Get an overview of all registered nodes grouped by geographic region.
    
    Returns:
    - List of regions with aggregated hardware stats
    - Total counts across all regions
    - Per-node hardware details within each region
    
    **Caching:** Results are cached for 120 seconds for performance.
    """
)
async def get_regions_overview(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get regions overview with aggregated hardware data. Clusters are queried in parallel."""
    cache = get_cache_manager()
    settings = get_settings()
    
    # Check cache first (2 minute TTL)
    cache_key = get_scope_cache_key("regions:overview")
    cached_data = cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug("Returning cached regions overview")
        return RegionsOverviewResponse(**cached_data)
    
    logger.info("Building regions overview from cluster data...")
    
    # Get all registered clusters from Vault
    try:
        from app.core.vault_client import get_vault_client
        from app.services.lxd_cert_manager import LXDCertificateManager
        
        vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
        cert_manager = LXDCertificateManager(
            vault_client=vault_client,
            clusters_path=settings.vault_clusters_path
        )
        
        # Get org-scoped cluster list
        cluster_ids = cert_manager.list_clusters()

        if not cluster_ids:
            response = RegionsOverviewResponse(
                regions=[], total_nodes=0, total_instances=0,
                total_cpu=0, total_memory=0, total_storage=0, total_gpu=0,
            )
            cache.set("api_responses", cache_key, response.model_dump(), ttl=120)
            return response

        # process single cluster (runs in thread pool for parallelism)
        def _process_cluster(cluster_id: str) -> Optional[Tuple[str, Dict, RegionNodeInfo, int, int]]:
            """Fetch metadata + resources for one cluster. Returns (region_id, region_data, node_info, inst_count, inst_running) or None."""
            try:
                cluster_info = cert_manager.get_cluster_info(cluster_id)
                metadata = cluster_info.get("metadata", {})
                location_data = metadata.get("location", {})
                
                if not location_data or not location_data.get("region_id"):
                    region_id = "unknown"
                    region_data = {
                        "region_id": "unknown", "region_name": "Unknown Location",
                        "country_code": "XX", "country_name": "Unknown",
                        "city": "Unknown", "latitude": 0, "longitude": 0, "continent": "XX",
                    }
                else:
                    region_id = location_data.get("region_id")
                    region_data = location_data
                
                status = ClusterStatus.PENDING
                node_type = metadata.get("type", "LXD")
                node_hardware = ExtendedHardwareInfo()
                instance_count = 0
                instance_running = 0
                
                try:
                    client = lxd_service.connection_manager.get_client(cluster_id)
                    if client is None:
                        try:
                            cert_data = cert_manager.get_cluster_certificate(cluster_id)
                            client = lxd_service.connection_manager.connect_with_vault_cert(
                                cluster_id=cluster_id,
                                endpoint=metadata.get("endpoint", ""),
                                cert_content=cert_data["client.crt"],
                                key_content=cert_data["client.key"],
                                verify=False,
                            )
                        except Exception:
                            pass
                    
                    if client:
                        status = ClusterStatus.ONLINE
                        try:
                            resources = client.resources
                            cpu_info = resources.get("cpu", {})
                            cpu_total = cpu_info.get("total", 0)
                            cpu_sockets = len(cpu_info.get("sockets", []))
                            cpu_architecture = cpu_info.get("architecture", "x86_64")
                            cpu_vendor = get_cpu_vendor_from_info(cpu_info)
                            cpu_model = get_cpu_model_from_info(cpu_info)
                            
                            memory_info = resources.get("memory", {})
                            memory_total = memory_info.get("total", 0)
                            memory_used = memory_info.get("used", 0)
                            
                            gpu_info = resources.get("gpu", {})
                            gpu_cards = gpu_info.get("cards", [])
                            gpu_total = len(gpu_cards)
                            gpu_vendors: Set[str] = set()
                            gpu_models: Set[str] = set()
                            for card in gpu_cards:
                                vendor = card.get("vendor", "")
                                product = card.get("product", "")
                                if vendor:
                                    if "nvidia" in vendor.lower():
                                        gpu_vendors.add("NVIDIA")
                                    elif "amd" in vendor.lower():
                                        gpu_vendors.add("AMD")
                                    elif "intel" in vendor.lower():
                                        gpu_vendors.add("Intel")
                                    else:
                                        gpu_vendors.add(vendor)
                                if product:
                                    gpu_models.add(product)
                            
                            disk_info = resources.get("storage", {})
                            storage_types = get_storage_types_from_disks(disk_info)
                            
                            storage_total = 0
                            storage_used = 0
                            storage_drivers: Set[str] = set()
                            try:
                                pools = client.storage_pools.all()
                                for pool in pools:
                                    storage_drivers.add(pool.driver)
                                    try:
                                        pool_resources = client.api.storage_pools[pool.name].resources.get()
                                        pool_data = pool_resources.json().get("metadata", {})
                                        space = pool_data.get("space", {})
                                        storage_total += space.get("total", 0)
                                        storage_used += space.get("used", 0)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            
                            try:
                                instances = client.instances.all()
                                instance_count = len(instances)
                                instance_running = sum(1 for i in instances if i.status == "Running")
                            except Exception:
                                pass
                            
                            network_info = resources.get("network", {})
                            network_cards = network_info.get("cards", [])
                            network_interfaces = [
                                card.get("ports", [{}])[0].get("id", "")
                                for card in network_cards if card.get("ports")
                            ]
                            
                            node_hardware = ExtendedHardwareInfo(
                                cpu_total=cpu_total, cpu_sockets=cpu_sockets,
                                cpu_architecture=cpu_architecture, cpu_vendor=cpu_vendor, cpu_model=cpu_model,
                                memory_total=memory_total, memory_used=memory_used,
                                gpu_total=gpu_total, gpu_vendors=list(gpu_vendors), gpu_models=list(gpu_models),
                                storage_total=storage_total, storage_used=storage_used,
                                storage_types=storage_types, storage_drivers=list(storage_drivers),
                                network_interfaces=network_interfaces,
                            )
                        except Exception as e:
                            logger.warning(f"Could not get resources for {cluster_id}: {e}")
                    else:
                        status = ClusterStatus.OFFLINE
                except Exception as e:
                    logger.debug(f"Could not connect to cluster {cluster_id}: {e}")
                    status = ClusterStatus.OFFLINE
                
                node_info = RegionNodeInfo(
                    cluster_id=cluster_id,
                    display_name=metadata.get("display_name", cluster_id),
                    status=status, node_type=node_type, hardware=node_hardware,
                )
                return (region_id, region_data, node_info, instance_count, instance_running)
            except Exception as e:
                logger.warning(f"Error processing cluster {cluster_id}: {e}")
                return None
        
        # run all cluster introspections in parallel
        loop = asyncio.get_event_loop()
        executor = ThreadPoolExecutor(max_workers=min(len(cluster_ids), 10))
        tasks = [loop.run_in_executor(executor, _process_cluster, cid) for cid in cluster_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        executor.shutdown(wait=False)
        
        regions_map: Dict[str, RegionSummary] = {}
        total_nodes = 0
        total_instances = 0
        total_cpu = 0
        total_memory = 0
        total_storage = 0
        total_gpu = 0
        
        for result in results:
            if isinstance(result, Exception) or result is None:
                continue
            region_id, region_data, node_info, instance_count, instance_running = result
            
            if region_id not in regions_map:
                regions_map[region_id] = RegionSummary(
                    region_id=region_data.get("region_id", "unknown"),
                    region_name=region_data.get("region_name", "Unknown"),
                    country_code=region_data.get("country_code", "XX"),
                    country_name=region_data.get("country_name", "Unknown"),
                    city=region_data.get("city", "Unknown"),
                    latitude=region_data.get("latitude", 0),
                    longitude=region_data.get("longitude", 0),
                    continent=region_data.get("continent", "XX"),
                    facility_name=region_data.get("facility_name"),
                    facility_provider=region_data.get("facility_provider"),
                    node_count=0, instance_count=0, instance_running=0,
                    hardware=ExtendedHardwareInfo(), nodes=[],
                )
            
            region = regions_map[region_id]
            region.nodes.append(node_info)
            region.node_count += 1
            region.instance_count += instance_count
            region.instance_running += instance_running
            
            total_nodes += 1
            total_instances += instance_count
            total_cpu += node_info.hardware.cpu_total
            total_memory += node_info.hardware.memory_total
            total_storage += node_info.hardware.storage_total
            total_gpu += node_info.hardware.gpu_total
        
        # aggregate hardware per region
        for region in regions_map.values():
            region.hardware = aggregate_hardware_info([n.hardware for n in region.nodes])
        
        response = RegionsOverviewResponse(
            regions=list(regions_map.values()),
            total_nodes=total_nodes, total_instances=total_instances,
            total_cpu=total_cpu, total_memory=total_memory,
            total_storage=total_storage, total_gpu=total_gpu,
        )
        
        cache.set("api_responses", cache_key, response.model_dump(), ttl=120)
        logger.info(f"Built regions overview: {len(regions_map)} regions, {total_nodes} nodes")
        
        return response
        
    except Exception as e:
        logger.error(f"Error building regions overview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{region_id}",
    response_model=RegionSummary,
    summary="Get region details",
    description="Get detailed information about a specific region."
)
async def get_region_details(
    region_id: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific region."""
    # Get full overview and filter
    overview = await get_regions_overview(lxd_service)
    
    for region in overview.regions:
        if region.region_id == region_id:
            return region
    
    raise HTTPException(status_code=404, detail=f"Region '{region_id}' not found")
