"""
API Router for LXD node status and health checks.
Provides endpoints for monitoring node connectivity.
"""

from fastapi import APIRouter, Depends, HTTPException
import logging
import asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    ClusterAvailabilityResponse,
    ClusterStatus,
    NodesStatusResponse,
    NodeResources,
    NodeUpdateRequest,
    NodeUpdateResponse,
    GpuCardListResponse,
)
from app.services.lxd import status as lxd_status
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    LXDOperationError,
)
from app.core.single_flight import single_flight_on_fresh
from app.core.config import get_settings
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, require_admin, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.vault_client import get_vault_client
from app.core.cache import (
    get_cache_manager,
    MICROCLOUD_RESOURCES_CACHE_PREFIX,
    NODES_AVAILABILITY_CACHE_KEY,
    NODES_AVAILABILITY_CACHE_TTL,
    NODES_STATUS_CACHE_KEY,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/nodes",
    tags=["Nodes"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel node status fetching
_node_executor = ThreadPoolExecutor(max_workers=10)

# Availability probes get their own pool so a slow reachability sweep cannot starve the
# node-status fan-out, and vice versa.
_availability_executor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="nodes_availability"
)
# One connect budget (3s) plus one read budget (8s) plus margin. The probes run in parallel,
# so this bounds the whole sweep, not one cluster.
NODES_AVAILABILITY_PROBE_TIMEOUT = 20.0


@router.get(
    "",
    response_model=NodesStatusResponse,
    summary="Get nodes status",
    description="Check connectivity and status of all configured LXD nodes including Vault-registered clusters.",
    responses={
        200: {"description": "Node status retrieved successfully"},
    }
)
@single_flight_on_fresh("nodes:status")
async def get_nodes_status(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """Get status of all LXD nodes including Vault-registered clusters."""
    
    # Try to get from cache first (180 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(NODES_STATUS_CACHE_KEY)
    cached_response = None if fresh else cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug("Returning cached nodes status")
        return NodesStatusResponse(**cached_response)
    
    # Not in cache - fetch fresh data using ASYNC PARALLEL version
    statuses = await lxd_service.check_nodes_status_async()
    
    # note: a second Vault sweep used to live here, adding any cluster the fan-out had not
    # already returned. It was unreachable: check_nodes_status_async iterates the same
    # apply_cluster_filter(cert_manager.list_clusters()) set, so the difference it computed
    # was always empty and its failure logs never appeared once in seven days of production
    # logs. Removed rather than left as dead weight on the hottest read in the app.

    response = NodesStatusResponse(
        nodes=statuses,
        all_connected=all(node.connected for node in statuses),
    )
    
    # Cache the response for 120 seconds (enterprise TTL)
    cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
    logger.debug("Cached nodes status for 120 seconds")
    
    return response



@router.get(
    "/names",
    response_model=list[str],
    summary="Get node names",
    description=(
        "Get list of configured LXD node names. Carries no reachability: use "
        "/nodes/availability when the caller has to know which clusters will answer."
    ),
    deprecated=True,
)
async def get_node_names(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get list of configured node names."""
    return lxd_service.get_node_names()


# important: declared before any /{node_name}/... route. FastAPI matches in declaration order,
# so moving this below /{node_name}/resources makes "availability" a node name and this
# endpoint unreachable.
@router.get(
    "/availability",
    response_model=ClusterAvailabilityResponse,
    summary="Get cluster availability",
    description=(
        "Per-cluster reachability for node pickers. Cheap by construction: it rides the cached "
        "/nodes fan-out when one is warm and falls back to a reachability-only probe, never to "
        "the resource fan-out."
    ),
)
@single_flight_on_fresh("nodes:availability")
async def get_nodes_availability(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """Availability of every cluster in the caller's org scope.

    important: never raises for a cluster that is down. "That cluster is offline" is this
    endpoint's payload, not its failure, and a picker that 503s tells an operator nothing.
    """
    cache = get_cache_manager()
    availability_key = get_scope_cache_key(NODES_AVAILABILITY_CACHE_KEY)

    if not fresh:
        cached = cache.get("api_responses", availability_key)
        if cached is not None:
            # note: overwrite `source`. The stored body records how it was first produced, so
            # returning it verbatim reports "probe" on every cache hit and makes the field
            # useless for the one question it exists to answer.
            return ClusterAvailabilityResponse(**{**cached, "source": "availability_cache"})

        # The node-status page and the metrics prewarm both fill this key. Riding it costs one
        # Redis read and keeps six pickers from generating their own fan-out.
        nodes_body = cache.get("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
        if nodes_body is not None:
            return _availability_response(
                cache, availability_key, lxd_status.project_from_nodes_body(nodes_body), "nodes_cache"
            )

    cluster_ids = lxd_service.get_node_names()
    try:
        rows = await run_blocking(
            lxd_status.probe_clusters,
            cluster_ids,
            cert_manager=lxd_service.cert_manager,
            connection_manager=lxd_service.connection_manager,
            settings=get_settings(),
            # Only an operator-initiated recheck may re-probe past the breaker. An ordinary
            # picker open must not walk a fleet of dead clusters paying connect timeouts.
            force=fresh,
            timeout=NODES_AVAILABILITY_PROBE_TIMEOUT,
            label="nodes.availability",
            executor=_availability_executor,
            # important: without a cap, _acquire never raises and the BlockingPoolFull handler
            # below is dead code. On a degraded fleet, non-fresh reads are not deduped, so
            # queued sweeps would accumulate instead of shedding.
            max_in_flight=4,
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        # Degrade to "we could not tell", never to "everything is offline". A picker that
        # marks a healthy fleet dead because a probe was slow is worse than one with no marks.
        logger.warning("Availability probe unavailable: %s", exc)
        raise HTTPException(
            status_code=503, detail="Cluster reachability is temporarily unavailable"
        )

    return _availability_response(cache, availability_key, rows, "probe")


def _availability_response(cache, cache_key: str, rows, source: str) -> ClusterAvailabilityResponse:
    """Build the response and cache it.

    note: an all-offline result IS the answer here and is cached like any other. That differs
    from the list fan-outs elsewhere, which refuse to cache an empty result because an empty
    list cannot be told apart from "nobody answered". Here the difference is the payload.
    """
    response = ClusterAvailabilityResponse(
        clusters=rows,
        online_count=sum(1 for row in rows if row.status == ClusterStatus.ONLINE),
        total_count=len(rows),
        source=source,
        generated_at=datetime.now(timezone.utc),
    )
    if cache:
        cache.set(
            "api_responses",
            cache_key,
            response.model_dump(mode="json"),
            ttl=NODES_AVAILABILITY_CACHE_TTL,
        )
    return response


@router.get(
    "/{node_name}/resources",
    response_model=NodeResources,
    summary="Get node resources",
    description="Get available resources (CPU, RAM, storage) for a specific node.",
)
async def get_node_resources(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get available resources for a specific node."""
    verify_cluster_access(node_name)
    try:
        return await asyncio.to_thread(lxd_service.get_node_resources_detail, node_name)
    except HTTPException:
        raise
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/{node_name}/gpus",
    response_model=GpuCardListResponse,
    summary="Get node GPUs",
    description="List the physical GPUs available on a specific node (for GPU passthrough).",
)
async def get_node_gpus(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List the GPUs on a specific node, classified for the passthrough picker."""
    verify_cluster_access(node_name)
    try:
        # get_node_gpus makes blocking LXD calls; keep them off the event loop.
        inventory = await asyncio.to_thread(lxd_service.get_node_gpus, node_name)
        # note: every detected card is returned, display controllers included. Each carries
        # its class and an `attachable` flag so the picker can rank and mark them rather than
        # hide hardware the operator can see in LXD.
        return GpuCardListResponse(
            host=node_name,
            gpus=inventory.cards,
            total=inventory.gpu_cards_total,
            compute_total=inventory.gpu_total,
        )
    except HTTPException:
        raise
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch(
    "/{node_name}",
    response_model=NodeUpdateResponse,
    summary="Update node metadata",
    description="Update node display name and other metadata.",
    responses={
        200: {"description": "Node metadata updated successfully"},
        404: {"description": "Node not found"},
        400: {"description": "Invalid request"},
    }
)
async def update_node(
    node_name: str,
    request: NodeUpdateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update node metadata (display name)."""
    verify_cluster_access(node_name)
    from fastapi import HTTPException
    from app.core.node_metadata import get_node_metadata_store
    
    # Check if node exists in config or Vault
    node_exists = False
    in_vault = False
    
    # Check in config - get_node_names() queries Vault-registered clusters
    if node_name in lxd_service.get_node_names():
        node_exists = True
        in_vault = True  # get_node_names() sources from Vault via cert_manager.list_clusters()
    
    # Fallback: check Vault directly if not found
    if not node_exists:
        settings = get_settings()
        if settings.vault_enabled and settings.vault_token:
            try:
                from app.services.lxd_cert_manager import LXDCertificateManager
                vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault_client,
                    clusters_path=settings.vault_clusters_path
                )
                cluster_ids = cert_manager.list_clusters()
                if node_name in cluster_ids:
                    node_exists = True
                    in_vault = True
            except Exception as e:
                logger.warning(f"Failed to check Vault clusters: {e}")
    
    if not node_exists:
        raise HTTPException(
            status_code=404,
            detail=f"Node '{node_name}' not found"
        )
    
    try:
        # Update display name in metadata store
        metadata_store = get_node_metadata_store()
        metadata_store.set_node_display_name(node_name, request.display_name)
        
        # Also update Vault if this is a Vault cluster
        if in_vault:
            settings = get_settings()
            if settings.vault_enabled and settings.vault_token:
                try:
                    from app.services.lxd_cert_manager import LXDCertificateManager
                    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                    cert_manager = LXDCertificateManager(
                        vault_client=vault_client,
                        clusters_path=settings.vault_clusters_path
                    )
                    # Read existing metadata
                    cluster_info = cert_manager.get_cluster_info(node_name)
                    metadata = cluster_info.get("metadata", {})
                    # Only update Vault if we got valid existing metadata
                    if metadata.get("endpoint"):
                        # Save back to Vault with updated display name (preserve all fields)
                        cert_manager.save_cluster_metadata(
                            cluster_id=node_name,
                            display_name=request.display_name,
                            endpoint=metadata.get("endpoint", ""),
                            zone=metadata.get("zone", ""),
                            cluster_type=metadata.get("type", "LXD"),
                            merchant_id=metadata.get("merchant_id", ""),
                            location=metadata.get("location", {}),
                        )
                        logger.info(f"Updated display name for Vault cluster {node_name}")
                    else:
                        logger.warning(f"Skipped Vault metadata update for {node_name}: existing metadata is empty/incomplete")
                except Exception as e:
                    logger.warning(f"Failed to update Vault metadata: {e}")
        
        # Invalidate caches so updated name appears immediately
        cache = get_cache_manager()
        if cache:
            cache.delete("api_responses", get_scope_cache_key("clusters:list:all"))
            cache.delete("api_responses", "clusters:list:all:scope=all")
            cache.delete("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
            cache.delete("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=all")
            # trailing * so every org-scope suffix goes with it
            cache.delete_pattern("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*")
            cache.invalidate_cluster_cache(node_name)
            logger.info(f"Invalidated caches after updating node {node_name}")
        
        return NodeUpdateResponse(
            success=True,
            message=f"Node '{node_name}' updated successfully",
            node_name=node_name,
            display_name=request.display_name,
        )
    except Exception as e:
        logger.error(f"Error updating node {node_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{node_name}/cache/invalidate",
    summary="Invalidate node cache",
    description="Force refresh node data by invalidating cache entries. Useful after configuration changes.",
    responses={
        200: {"description": "Cache invalidated successfully"},
    }
)
async def invalidate_node_cache(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Invalidate all cache entries for a specific node."""
    verify_cluster_access(node_name)
    from fastapi import HTTPException
    
    try:
        cache = get_cache_manager()
        
        # Invalidate various cache keys for this node (pattern match for all scope variants)
        cache.delete_pattern("api_responses", f"{NODES_STATUS_CACHE_KEY}*")
        cache.delete_pattern("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*")
        cache.delete("lxd_resources", f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:{node_name}")
        cache.delete_pattern("api_responses", f"cluster_members:list:host={node_name}*")
        
        logger.info(f"Invalidated all cache entries for node: {node_name}")
        
        return {
            "success": True,
            "message": f"Cache invalidated for node '{node_name}'",
            "node_name": node_name
        }
    except Exception as e:
        logger.error(f"Error invalidating cache for {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/cache/invalidate-all",
    summary="Invalidate all nodes cache",
    description="Force refresh all nodes data by clearing all node-related cache entries.",
    responses={
        200: {"description": "All caches invalidated successfully"},
    }
)
async def invalidate_all_nodes_cache(
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """Invalidate all node-related cache entries."""
    from fastapi import HTTPException
    
    try:
        cache = get_cache_manager()
        
        # Clear all nodes status cache (all scope variants)
        cache.delete_pattern("api_responses", f"{NODES_STATUS_CACHE_KEY}*")
        cache.delete_pattern("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*")
        
        # Clear all LXD resources cache (pattern delete)
        cache.delete_pattern("lxd_resources", "*")
        
        # Clear all cluster members cache
        cache.delete_pattern("api_responses", "cluster_members:*")
        
        logger.info("Invalidated all node-related cache entries")
        
        return {
            "success": True,
            "message": "All node caches invalidated successfully"
        }
    except Exception as e:
        logger.error(f"Error invalidating all caches: {e}")
        raise HTTPException(status_code=500, detail=str(e))
