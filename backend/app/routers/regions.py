"""
API Router for Regions/Geographic Overview.
Provides endpoints for viewing nodes grouped by geographic region with hardware summaries.
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Optional, Set, Tuple
import logging
import asyncio
import contextvars
from dataclasses import replace
from functools import partial
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import LXDService, get_lxd_service
from app.core.config import get_settings
from app.core.cache import (
    CAPACITY_GENERATION_KIND,
    REGIONS_OVERVIEW_CACHE_KEY,
    get_cache_manager,
    get_resource_list_generation,
)
from app.models.schemas import (
    ExtendedHardwareInfo,
    RegionNodeInfo,
    RegionSummary,
    RegionsOverviewResponse,
    ClusterStatus,
)

from app.services.lxd.freshness import read_resources
from app.services.lxd.gpu import build_gpu_inventory
from app.services.lxd.node_resources import aggregate_microcloud_resources
from app.services.lxd.storage_model import PoolReading, summarise_cluster_storage

logger = logging.getLogger(__name__)
from app.core.auth import AuthenticatedUser
from app.core.dependencies import require_tenant
from app.core.rbac import Permission, has_permission
from app.services.lxd.hardware import (
    cpu_model_from_sockets,
    cpu_vendor_from_sockets,
    storage_types_from_disks,
)
from app.core.registry_guard import require_known_registry
from app.core.org_scope import org_scope_dependency, get_scope_cache_key, apply_cluster_filter, get_tenant_acl
import time
from app.core.single_flight import mark_rebuilt, rebuild_lock, rebuilt_since, release_lock
from app.core.tenant_visibility import tenant_acl_has_permission
from app.core.type_utils import resolve_cluster_type, resolve_clustered
from app.services.lxd.project_scope import list_instance_records
from app.services.lxd_certs.storage import stored_location_fields
from app.core.lxd_path import safe_segment

router = APIRouter(
    prefix="/regions",
    tags=["Regions"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


# note: the CPU and disk helpers these three names used to hold now live in
# services/lxd/hardware.py, so the region rollup and the per-node profile read the same
# code. The disk classifier gained a fix on the way: it used to branch on a `rotational`
# key LXD does not emit, so every SATA disk was reported as an SSD.

def region_hardware(
    nodes_hardware: List[Optional[ExtendedHardwareInfo]],
) -> Optional[ExtendedHardwareInfo]:
    """A region's hardware from the clusters whose hardware was read, or None when none was.

    A cluster that was offline or whose read failed contributes nothing rather than zeros, so a
    region of such clusters says it was not read instead of claiming an empty rack.
    """
    read = [hardware for hardware in nodes_hardware if hardware is not None]
    return aggregate_hardware_info(read) if read else None


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
    total_gpu_cards = 0
    total_gpu_integrated = 0
    total_storage = 0
    total_storage_used = 0
    
    for hw in nodes_hardware:
        total_cpu += hw.cpu_total
        total_sockets += hw.cpu_sockets
        total_memory += hw.memory_total
        total_memory_used += hw.memory_used
        total_gpu += hw.gpu_total
        total_gpu_cards += hw.gpu_cards_total
        total_gpu_integrated += hw.gpu_integrated_total
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
        gpu_cards_total=total_gpu_cards,
        gpu_integrated_total=total_gpu_integrated,
        gpu_vendors=list(sorted(all_gpu_vendors)),
        gpu_models=list(sorted(all_gpu_models)),
        storage_total=total_storage,
        storage_used=total_storage_used,
        storage_types=list(sorted(all_storage_types)),
        storage_drivers=list(sorted(all_storage_drivers)),
        network_interfaces=list(sorted(all_network_interfaces)),
    )


async def build_regions_overview(
    lxd_service: LXDService,
    fresh: bool = False,
) -> RegionsOverviewResponse:
    """The regions overview for the caller's org scope, before any role projection.

    Clusters are queried in parallel. The body is cached per scope and ACL, and the cache
    prewarm builds it with no caller at all, so nothing here may depend on the caller's role:
    the routes below project it for each caller.
    """
    cache = get_cache_manager()
    settings = get_settings()
    
    # Check cache first (2 minute TTL)
    cache_key = get_scope_cache_key(REGIONS_OVERVIEW_CACHE_KEY)
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug("Returning cached regions overview")
        return RegionsOverviewResponse(**cached_data)
    generations = (
        get_resource_list_generation(cache, CAPACITY_GENERATION_KIND),
        get_resource_list_generation(cache, "regions"),
    )
    
    # important: serialise cold rebuilds per scope. A manual refresh bypasses the
    # response cache on purpose, and this endpoint fans out over every cluster
    # (15-45s cold), so N simultaneous fresh requests would launch N fan-outs at
    # the same hardware. The second arrival waits, then reads the first's result.
    _lock_key = f"regions:overview:{cache_key}"
    _arrived_at = time.monotonic()
    _lock = rebuild_lock(_lock_key)
    try:
        async with _lock:
            # important: only accept an entry someone rebuilt AFTER we arrived. Reading
            # the cache unconditionally here made fresh=true return the very cached body
            # it was asked to bypass, because the admin key is kept warm by the metrics
            # collector and is therefore almost never absent.
            rebuilt = (
                cache.get("api_responses", cache_key)
                if rebuilt_since(_lock_key, _arrived_at)
                else None
            )
            if rebuilt is not None:
                logger.debug("Regions overview was rebuilt by a concurrent request")
                return RegionsOverviewResponse(**rebuilt)

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
                cluster_ids = apply_cluster_filter(cert_manager.list_clusters())
                # An unreadable registry with nothing known is not an empty map, and the branch
                # below would cache it for 120 s (issue #15).
                require_known_registry(cluster_ids)

                if not cluster_ids:
                    response = RegionsOverviewResponse(
                        regions=[], total_nodes=0, total_instances=0,
                        total_cpu=0, total_memory=0, total_storage=0, total_gpu=0,
                        collected_at=datetime.now(timezone.utc),
                    )
                    current = (
                        get_resource_list_generation(cache, CAPACITY_GENERATION_KIND),
                        get_resource_list_generation(cache, "regions"),
                    )
                    if current == generations:
                        cache.set("api_responses", cache_key, response.model_dump(), ttl=120)
                    mark_rebuilt(_lock_key)
                    return response

                tenant_acl = get_tenant_acl()

                # important: a cluster that answered but whose resource read failed is reported
                # ONLINE with no hardware, so the fleet totals leave it out. Caching that pins a
                # silently shrunken fleet for the full 120s. Recording the failure here lets the
                # write below decline, the same way
                # the monitoring dashboard declines to cache a no-cluster-answered body.
                # note: set.add from the thread pool is safe under the GIL.
                degraded_clusters: Set[str] = set()

                # process single cluster (runs in thread pool for parallelism)
                def _process_cluster(cluster_id: str) -> Optional[Tuple[str, Dict, RegionNodeInfo, int, int]]:
                    """Fetch metadata + resources for one cluster. Returns (region_id, region_data, node_info, inst_count, inst_running) or None."""
                    try:
                        cluster_info = cert_manager.get_cluster_info(cluster_id)
                        metadata = cluster_info.get("metadata", {})
                        # note: an unreadable stored location places the cluster under Unknown
                        # instead of dropping it from the map (a string or list raised here).
                        location_data = stored_location_fields(cluster_id, metadata)

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
                        # Stored value only; resolved against the live client further down,
                        # once one is in hand. None means not determined yet, not LXD.
                        node_type = metadata.get("type")
                        # note: None until a resource read succeeds. An offline cluster or a
                        # failed read is "not read", not a profile of zeros.
                        node_hardware: Optional[ExtendedHardwareInfo] = None
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
                                # The connected host is the authority on its own family; the
                                # stored string may predate any trusted read.
                                node_type = resolve_cluster_type(node_type, client)
                                try:
                                    # important: a live read, not pylxd's client.resources.
                                    # That property memoises the body for the life of the
                                    # pooled client, so this map showed each worker's
                                    # connect-time snapshot rather than the fleet now.
                                    resources = read_resources(client, cluster_id).body
                                    cpu_info = resources.get("cpu", {})
                                    cpu_total = cpu_info.get("total", 0)
                                    cpu_sockets = len(cpu_info.get("sockets", []))
                                    cpu_architecture = cpu_info.get("architecture", "x86_64")
                                    cpu_vendor = cpu_vendor_from_sockets(cpu_info)
                                    cpu_model = cpu_model_from_sockets(cpu_info)
                            
                                    memory_info = resources.get("memory", {})
                                    memory_total = memory_info.get("total", 0)
                                    memory_used = memory_info.get("used", 0)
                            
                                    # note: counting cards here would double-count a Tegra
                                    # iGPU, which enumerates a display node beside its compute
                                    # node, and the old vendor bucketing produced nothing at
                                    # all on hardware that reports no vendor string.
                                    gpu_inv = build_gpu_inventory(resources)
                            
                                    disk_info = resources.get("storage", {})
                                    storage_types = storage_types_from_disks(disk_info)
                            
                                    # important: folded through the same summariser /nodes uses,
                                    # so a region's capacity and a node's capacity are the same
                                    # arithmetic. This loop used to add every pool it saw, which
                                    # on a clustered host counts a shared backing store once per
                                    # member, and /regions and /nodes then disagreed about the
                                    # same cluster. That is the defect gpu-inventory.md exists to
                                    # stop repeating.
                                    #
                                    # note: still the coordinator's untargeted view, so a
                                    # clustered host contributes what the member we are connected
                                    # to can see. Reading every member here would multiply this
                                    # fan-out by the cluster size; /nodes carries the per-member
                                    # figure and is cached for it.
                                    storage_drivers: Set[str] = set()
                                    region_pool_readings: List[PoolReading] = []
                                    try:
                                        pools = client.storage_pools.all()
                                        for pool in pools:
                                            storage_drivers.add(pool.driver)
                                            try:
                                                pool_resources = client.api.storage_pools[safe_segment(pool.name)].resources.get()
                                                pool_data = pool_resources.json().get("metadata", {})
                                                space = pool_data.get("space", {})
                                                region_pool_readings.append(PoolReading(
                                                    member=cluster_id,
                                                    name=pool.name,
                                                    driver=pool.driver,
                                                    space_total=space.get("total", 0),
                                                    space_used=space.get("used", 0),
                                                ))
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    region_storage = summarise_cluster_storage(region_pool_readings)
                                    storage_total = region_storage.total
                                    storage_used = region_storage.used
                            
                                    network_info = resources.get("network", {})
                                    network_cards = network_info.get("cards", [])
                                    network_interfaces = [
                                        card.get("ports", [{}])[0].get("id", "")
                                        for card in network_cards if card.get("ports")
                                    ]
                            
                                    # important: a clustered host's capacity is the sum of its
                                    # members, and /nodes has aggregated it since the MicroCloud
                                    # work. This map did not, so the SAME cluster reported one
                                    # member's CPU, memory, GPU and storage here and the whole
                                    # cluster's on the nodes page. The aggregate is cached under
                                    # the key /nodes writes, so a warm fleet pays a Redis get and
                                    # the two pages cannot drift apart again.
                                    if resolve_clustered(metadata.get("clustered"), client):
                                        try:
                                            cluster_totals = aggregate_microcloud_resources(
                                                client=client,
                                                node_name=cluster_id,
                                                base_resources={},
                                            )
                                        except Exception as agg_err:
                                            cluster_totals = None
                                            logger.warning(
                                                "Could not aggregate members of %s for the region "
                                                "map, falling back to the coordinator's own view: %s",
                                                cluster_id,
                                                agg_err,
                                            )
                                        if cluster_totals is not None:
                                            cpu_total = cluster_totals.cpu_total
                                            cpu_sockets = cluster_totals.cpu_sockets
                                            memory_total = cluster_totals.memory_total
                                            memory_used = cluster_totals.memory_used
                                            gpu_inv = replace(
                                                gpu_inv,
                                                gpu_total=cluster_totals.gpu_total,
                                                gpu_cards_total=cluster_totals.gpu_cards_total,
                                                gpu_integrated_total=cluster_totals.gpu_integrated_total,
                                                vendors=cluster_totals.gpu_vendors,
                                                models=cluster_totals.gpu_models,
                                            )
                                            storage_total = cluster_totals.storage_total
                                            storage_used = cluster_totals.storage_used
                                            storage_drivers.update(
                                                str(pool.get("driver"))
                                                for pool in cluster_totals.storage_pools
                                                if pool.get("driver")
                                            )

                                    node_hardware = ExtendedHardwareInfo(
                                        cpu_total=cpu_total, cpu_sockets=cpu_sockets,
                                        cpu_architecture=cpu_architecture, cpu_vendor=cpu_vendor, cpu_model=cpu_model,
                                        memory_total=memory_total, memory_used=memory_used,
                                        gpu_total=gpu_inv.gpu_total,
                                        gpu_cards_total=gpu_inv.gpu_cards_total,
                                        gpu_integrated_total=gpu_inv.gpu_integrated_total,
                                        gpu_vendors=gpu_inv.vendors, gpu_models=gpu_inv.models,
                                        storage_total=storage_total, storage_used=storage_used,
                                        storage_types=storage_types, storage_drivers=list(storage_drivers),
                                        network_interfaces=network_interfaces,
                                    )
                                except Exception as e:
                                    logger.warning(f"Could not get resources for {cluster_id}: {e}")
                                    degraded_clusters.add(cluster_id)

                                # important: outside the resources read, so a cluster whose
                                # hardware read failed still reports its instances. These counts
                                # are the only figures a Tenant is sent, so a listing that failed
                                # keeps the body out of the cache rather than pinning a zero.
                                try:
                                    # every project: the pooled client is bound to default, and
                                    # the ACL check just below reads instance.project, which a
                                    # default-scoped listing never carries.
                                    instances, _ = list_instance_records(client)
                                    visible_instances = [
                                        instance for instance in instances
                                        if tenant_acl_has_permission(
                                            tenant_acl,
                                            cluster_id,
                                            getattr(instance, "project", "default") or "default",
                                            instance.name,
                                            "view",
                                        )
                                    ]
                                    instance_count = len(visible_instances)
                                    instance_running = sum(
                                        1 for instance in visible_instances
                                        if instance.status == "Running"
                                    )
                                except Exception as e:
                                    logger.warning("Could not list instances on %s: %s", cluster_id, e)
                                    degraded_clusters.add(cluster_id)
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
                #
                # important: each task carries its OWN copy of the caller's context.
                # `loop.run_in_executor` copies nothing, so `get_client` inside the thread read
                # an unset org-scope var and its filter could never deny; core/blocking.py
                # records the full consequence list. One shared Context would be worse than
                # none: `Context.run` refuses re-entry, so ten threads entering the same copy
                # raise "cannot enter context", which is why the copy is per task and not
                # hoisted out of the comprehension.
                loop = asyncio.get_event_loop()
                executor = ThreadPoolExecutor(max_workers=min(len(cluster_ids), 10))
                tasks = [
                    loop.run_in_executor(
                        executor, partial(contextvars.copy_context().run, _process_cluster, cid)
                    )
                    for cid in cluster_ids
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                executor.shutdown(wait=False)
        
                regions_map: Dict[str, RegionSummary] = {}
                total_nodes = 0
                total_instances = 0
                total_cpu = 0
                total_memory = 0
                total_storage = 0
                total_gpu = 0
                total_gpu_cards = 0
                total_gpu_integrated = 0
        
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
                            province=region_data.get("province"),
                            facility_name=region_data.get("facility_name"),
                            facility_provider=region_data.get("facility_provider"),
                            node_count=0, instance_count=0, instance_running=0,
                            hardware=None, nodes=[],
                        )
            
                    region = regions_map[region_id]
                    region.nodes.append(node_info)
                    region.node_count += 1
                    region.instance_count += instance_count
                    region.instance_running += instance_running
            
                    total_nodes += 1
                    total_instances += instance_count
                    hardware = node_info.hardware
                    if hardware is not None:
                        total_cpu += hardware.cpu_total
                        total_memory += hardware.memory_total
                        total_storage += hardware.storage_total
                        total_gpu += hardware.gpu_total
                        total_gpu_cards += hardware.gpu_cards_total
                        total_gpu_integrated += hardware.gpu_integrated_total
        
                # aggregate hardware per region
                for region in regions_map.values():
                    region.hardware = region_hardware([n.hardware for n in region.nodes])
        
                response = RegionsOverviewResponse(
                    regions=list(regions_map.values()),
                    total_nodes=total_nodes, total_instances=total_instances,
                    total_cpu=total_cpu, total_memory=total_memory,
                    total_storage=total_storage, total_gpu=total_gpu,
                    total_gpu_cards=total_gpu_cards, total_gpu_integrated=total_gpu_integrated,
                    # important: when the fan-out finished, carried through the 120s
                    # cache. Restamping on the cached read would make this map claim to
                    # be current every time anyone looked at it.
                    collected_at=datetime.now(timezone.utc),
                )
        
                if degraded_clusters:
                    logger.warning(
                        "Not caching regions overview: %s answered but did not report resources",
                        ", ".join(sorted(degraded_clusters)),
                    )
                elif (
                    get_resource_list_generation(cache, CAPACITY_GENERATION_KIND),
                    get_resource_list_generation(cache, "regions"),
                ) == generations:
                    cache.set("api_responses", cache_key, response.model_dump(), ttl=120)
                else:
                    logger.info("Not caching regions overview: capacity changed during the read")
                # important: mark only a rebuild that finished. In the finally below this
                # also ran when the build raised, so the next waiter believed a rebuild had
                # completed and served the cache it had asked to bypass.
                mark_rebuilt(_lock_key)
                logger.info(f"Built regions overview: {len(regions_map)} regions, {total_nodes} nodes")

                return response

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error building regions overview: {e}")
                raise HTTPException(status_code=500, detail=str(e))

    finally:
        release_lock(_lock_key)


def _can_read_hardware(user: AuthenticatedUser) -> bool:
    # important: the line GET /nodes draws. Hardware inventory and the hypervisor family are
    # Partner and above; a Tenant reads places, cluster names and status, and its own instance
    # counts, which the build already narrowed to its ACL.
    return has_permission(user, Permission.CLUSTER_READ)


def _region_without_hardware(region: RegionSummary) -> RegionSummary:
    return region.model_copy(
        update={
            "hardware": None,
            "nodes": [
                node.model_copy(update={"hardware": None, "node_type": None})
                for node in region.nodes
            ],
        }
    )


def _overview_for_caller(
    response: RegionsOverviewResponse, user: AuthenticatedUser
) -> RegionsOverviewResponse:
    """The overview as this caller may read it.

    important: new models, never an edit of the one passed in. That body is the cached entry,
    stored unprojected so the prewarm, which has no caller, can write it too.
    """
    if _can_read_hardware(user):
        return response
    return response.model_copy(
        update={
            "regions": [_region_without_hardware(region) for region in response.regions],
            "total_cpu": None,
            "total_memory": None,
            "total_storage": None,
            "total_gpu": None,
            "total_gpu_cards": None,
            "total_gpu_integrated": None,
        }
    )


@router.get(
    "",
    response_model=RegionsOverviewResponse,
    summary="Get regions overview",
    description="""
    Get an overview of all registered clusters grouped by geographic region.

    Returns:
    - List of regions with their clusters, status and instance counts
    - Total counts across all regions
    - Aggregated hardware per region and per cluster, for Partners and Administrators.
      For a Tenant the hardware fields, the hardware totals and the hypervisor type are null.

    **Caching:** Results are cached for 120 seconds for performance.
    """
)
async def get_regions_overview(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
    user: AuthenticatedUser = Depends(require_tenant()),
):
    """Get the regions overview, projected for the caller's role."""
    return _overview_for_caller(await build_regions_overview(lxd_service, fresh), user)


@router.get(
    "/{region_id}",
    response_model=RegionSummary,
    summary="Get region details",
    description="Get one region. Hardware fields and the hypervisor type are null for a Tenant."
)
async def get_region_details(
    region_id: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    """Get details for a specific region."""
    overview = await build_regions_overview(lxd_service)

    for region in overview.regions:
        if region.region_id == region_id:
            return region if _can_read_hardware(user) else _region_without_hardware(region)

    raise HTTPException(status_code=404, detail=f"Region '{region_id}' not found")
