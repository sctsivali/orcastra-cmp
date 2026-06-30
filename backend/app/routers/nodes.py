"""
API Router for LXD node status and health checks.
Provides endpoints for monitoring node connectivity.
"""

from fastapi import APIRouter, Depends
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    NodeStatus,
    NodesStatusResponse,
    NodeResources,
    NodeUpdateRequest,
    NodeUpdateResponse,
    GpuCardInfo,
    GpuCardListResponse,
)
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    LXDOperationError,
)
from app.core.config import get_settings
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, require_admin, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, apply_cluster_filter
from app.core.vault_client import get_vault_client
from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/nodes",
    tags=["Nodes"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel node status fetching
_node_executor = ThreadPoolExecutor(max_workers=10)


@router.get(
    "",
    response_model=NodesStatusResponse,
    summary="Get nodes status",
    description="Check connectivity and status of all configured LXD nodes including Vault-registered clusters.",
    responses={
        200: {"description": "Node status retrieved successfully"},
    }
)
async def get_nodes_status(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get status of all LXD nodes including Vault-registered clusters."""
    
    # Try to get from cache first (180 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("nodes:status:all")
    cached_response = cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug("Returning cached nodes status")
        return NodesStatusResponse(**cached_response)
    
    # Not in cache - fetch fresh data using ASYNC PARALLEL version
    statuses = await lxd_service.check_nodes_status_async()
    
    # Also fetch clusters from Vault if enabled
    settings = get_settings()
    if settings.vault_enabled and settings.vault_token:
        try:
            from app.services.lxd_cert_manager import LXDCertificateManager
            vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
            cert_manager = LXDCertificateManager(
                vault_client=vault_client,
                clusters_path=settings.vault_clusters_path
            )
            
            # Get all cluster IDs from Vault
            cluster_ids = apply_cluster_filter(cert_manager.list_clusters())
            
            # Filter out clusters already in statuses
            existing_names = {s.name for s in statuses}
            new_cluster_ids = [cid for cid in cluster_ids if cid not in existing_names]
            
            if new_cluster_ids:
                # Helper function to fetch cluster status
                def fetch_cluster_status(cluster_id: str) -> NodeStatus:
                    try:
                        cluster_info = cert_manager.get_cluster_info(cluster_id)
                        metadata = cluster_info.get("metadata", {})
                        certificate_info = cluster_info.get("certificate", {})
                        endpoint = metadata.get("endpoint", "")
                        
                        # Try to connect with certificate and get full node status
                        node_status = None
                        if certificate_info.get("has_certificate"):
                            try:
                                # Get certificate content from Vault
                                cert_data = cert_manager.vault.vault_read(
                                    f"{settings.vault_clusters_path}/{cluster_id}/certs"
                                )
                                
                                cert_content = cert_data.get("client.crt", "")
                                key_content = cert_data.get("client.key", "")
                                
                                if cert_content and key_content:
                                    # Try to connect using Vault certificate
                                    from app.services.lxd.connection import LXDConnectionManager
                                    conn_manager = LXDConnectionManager()
                                    client = conn_manager.connect_with_vault_cert(
                                        cluster_id=cluster_id,
                                        endpoint=endpoint,
                                        cert_content=cert_content,
                                        key_content=key_content
                                    )
                                    
                                    # Get resources using the connected client
                                    from app.services.lxd.resources import ResourceManager
                                    resource_manager = ResourceManager()
                                    node_status = resource_manager.check_node_status(
                                        client=client,
                                        node_name=cluster_id,
                                        endpoint=endpoint
                                    )
                                    
                                    # Override display name from metadata
                                    node_status.display_name = metadata.get("display_name", cluster_id)
                                
                            except Exception as e:
                                logger.warning(f"Failed to connect to {cluster_id} with certificate: {e}")
                        
                        # If connection failed or no certificate, create basic status
                        if node_status is None:
                            is_connected = False
                            if not certificate_info.get("has_certificate"):
                                try:
                                    import requests
                                    response = requests.get(
                                        f"{endpoint}/1.0",
                                        verify=False,
                                        timeout=2
                                    )
                                    is_connected = response.ok
                                except:
                                    pass
                            
                            node_status = NodeStatus(
                                name=cluster_id,
                                display_name=metadata.get("display_name", cluster_id),
                                endpoint=endpoint,
                                connected=is_connected,
                                node_type=None,
                                tags=[],
                                error=None if is_connected or certificate_info.get("has_certificate") else "Not reachable"
                            )
                        
                        return node_status
                    except Exception as e:
                        logger.warning(f"Failed to get info for cluster {cluster_id}: {e}")
                        return None
                
                # Fetch all cluster statuses in parallel
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor(max_workers=min(len(new_cluster_ids), 5)) as executor:
                    results = await asyncio.gather(
                        *[loop.run_in_executor(executor, fetch_cluster_status, cid) for cid in new_cluster_ids]
                    )
                
                # Add valid results to statuses
                for result in results:
                    if result is not None:
                        statuses.append(result)
                    
        except Exception as e:
            logger.warning(f"Failed to fetch Vault clusters: {e}")
    
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
    description="Get list of configured LXD node names.",
)
async def get_node_names(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get list of configured node names."""
    return lxd_service.get_node_names()


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
        resources = lxd_service.get_node_resources(node_name)
        return NodeResources(**resources)
    except LXDOperationError as e:
        from fastapi import HTTPException
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
    """List the physical GPUs on a specific node."""
    verify_cluster_access(node_name)
    try:
        # get_node_gpus makes blocking LXD calls; keep them off the event loop.
        gpus = await asyncio.to_thread(lxd_service.get_node_gpus, node_name)
        return GpuCardListResponse(
            host=node_name,
            gpus=[GpuCardInfo(**g) for g in gpus],
            total=len(gpus),
        )
    except LXDOperationError as e:
        from fastapi import HTTPException
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
            cache.delete("api_responses", get_scope_cache_key("nodes:status:all"))
            cache.delete("api_responses", "nodes:status:all:scope=all")
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
    from fastapi import HTTPException
    
    try:
        cache = get_cache_manager()
        
        # Invalidate various cache keys for this node (pattern match for all scope variants)
        cache.delete_pattern("api_responses", "nodes:status:all*")
        cache.delete("lxd_resources", f"microcloud_resources:{node_name}")
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
        cache.delete_pattern("api_responses", "nodes:status:all*")
        
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
