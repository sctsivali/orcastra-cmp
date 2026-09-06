"""
API Router for Storage Volume operations.
Provides endpoints for managing LXD storage volumes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.lxd_errors import lxd_http_error
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    parse_lxd_reference,
    tenant_acl_active,
)
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field
from app.services.activity_logger import get_activity_logger
from app.services.lxd.lazy_client import client_for

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/storage/volumes",
    tags=["Storage Volumes"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class StorageVolumeInfo(BaseModel):
    """Storage volume information."""
    name: str = Field(..., description="Volume name")
    type: str = Field(..., description="Volume type")
    pool: str = Field(..., description="Storage pool name")
    description: str = Field("", description="Volume description")
    config: Dict[str, str] = Field(default_factory=dict, description="Volume configuration")
    used_by: List[str] = Field(default_factory=list, description="Resources using this volume")
    node: str = Field("", description="Node name where volume is defined")
    content_type: str = Field("", description="Content type")


class StorageVolumeCreateRequest(BaseModel):
    """Request body for creating a storage volume."""
    name: str = Field(..., description="Volume name")
    pool: str = Field(..., description="Storage pool name")
    type: str = Field("custom", description="Volume type")
    description: str = Field("", description="Volume description")
    config: Dict[str, str] = Field(default_factory=dict, description="Volume configuration")
    content_type: str = Field("filesystem", description="Content type")


class StorageVolumeListResponse(BaseModel):
    """Response for listing storage volumes."""
    total: int = Field(..., description="Total number of volumes")
    volumes: List[StorageVolumeInfo] = Field(..., description="List of storage volumes")
    nodes: List[str] = Field(..., description="List of nodes")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their entries are missing from this list, "
            "so an unreachable node is distinguishable from one that has none."
        ),
    )


def _fetch_volumes_for_node(lxd_service: LXDService, node_name: str, pool_filter: Optional[str]) -> List[StorageVolumeInfo]:
    """Fetch storage volumes for a single node - runs in thread pool."""
    volumes_list = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        if client is None:
            # important: unreachable is not empty. See the sibling routers.
            raise LXDConnectionError(f"Could not connect to {node_name}")
        # Get all storage pools
        pools = client.storage_pools.all()
        
        for storage_pool in pools:
            if pool_filter and storage_pool.name != pool_filter:
                continue
            
            try:
                volumes = storage_pool.volumes.all()
                for volume in volumes:
                    volumes_list.append(StorageVolumeInfo(
                        name=volume.name,
                        type=volume.type,
                        pool=storage_pool.name,
                        description=getattr(volume, 'description', '') or "",
                        config=volume.config or {},
                        used_by=volume.used_by or [],
                        content_type=getattr(volume, 'content_type', ''),
                        node=node_name
                    ))
            except LXDConnectionError:
                # a missing client IS unreachability: let it through
                raise
            except Exception as e:
                # important: not unreachability. See networks.py: an item
                # vanishing mid-listing must not condemn the whole node.
                logger.warning(f"Error listing volumes in pool {storage_pool.name}: {e}")
    except LXDConnectionError:
        # a missing client IS unreachability: let it through
        raise
    except Exception as e:
        # important: not unreachability. See networks.py: an item
        # vanishing mid-listing must not condemn the whole node.
        logger.warning(f"Error listing storage volumes on {node_name}: {e}")
    return volumes_list


@router.get(
    "",
    response_model=StorageVolumeListResponse,
    summary="List all storage volumes",
    description="Get a list of all storage volumes from LXD nodes.",
)
@single_flight("storage_volumes:list")
async def list_storage_volumes(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    pool: Optional[str] = Query(None, description="Filter by storage pool"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all storage volumes."""
    if host:
        verify_cluster_access(host)
    # Try cache first (30 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"storage_volumes:list:host={host}:pool={pool}")
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached storage volumes for host={host}, pool={pool}")
        # Reconstruct Pydantic model from cached dict
        return StorageVolumeListResponse(**cached_data)
    
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch volumes from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_volumes_for_node, lxd_service, node_name, pool)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_volumes = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        answered = 0
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                answered += 1
                all_volumes.extend(result)
            elif isinstance(result, Exception):
                logger.warning(f"Error fetching storage volumes: {result}")
                unreachable_nodes.append(node_name)

        if tenant_acl_active():
            visible_volumes = []
            for volume in all_volumes:
                volume.used_by = filter_visible_lxd_references(volume.node, volume.used_by)
                if volume.used_by:
                    visible_volumes.append(volume)
            all_volumes = visible_volumes
        
        # Store in variable first
        response = StorageVolumeListResponse(
            total=len(all_volumes),
            volumes=all_volumes,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
        )
        
        # important: an outage must not be remembered as the answer. See instances.py.
        if answered:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
            logger.debug("Cached storage volumes list for 180 seconds")
        else:
            logger.warning("Not caching storage volumes list: no node answered")

        return response
    except Exception as e:
        logger.error(f"Error listing storage volumes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{volume_type}/{volume_name}",
    response_model=StorageVolumeInfo,
    summary="Get storage volume details",
    description="Get detailed information about a specific storage volume.",
)
async def get_storage_volume(
    volume_type: str,
    volume_name: str,
    host: str = Query(..., description="Target host node"),
    pool: str = Query(..., description="Storage pool name"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific storage volume."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        storage_pool = client.storage_pools.get(pool)
        volume = storage_pool.volumes.get(volume_type, volume_name)
        used_by = filter_visible_lxd_references(host, volume.used_by or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this storage volume")
        
        return StorageVolumeInfo(
            name=volume.name,
            type=volume.type,
            pool=pool,
            description=getattr(volume, 'description', '') or "",
            config=volume.config or {},
            used_by=used_by,
            content_type=getattr(volume, 'content_type', ''),
            node=host,
        )
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error getting storage volume {volume_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Storage volume {volume_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create storage volume",
    description="Create a new storage volume.",
)
async def create_storage_volume(
    request: StorageVolumeCreateRequest,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new storage volume."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        storage_pool = client.storage_pools.get(request.pool)
        
        volume = storage_pool.volumes.create({
            "name": request.name,
            "type": request.type,
            "config": request.config,
            "description": request.description,
            "content_type": request.content_type
        })
        
        # Announce the lifecycle change to any subscribed add-on. Volumes have no operation in
        # the integrations proxy vocabulary, so this router is their only source: without it two
        # of the nine catalogued events could never fire at all.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.volume.created.v1",
            cluster_id=host,
            resource_type="volume",
            resource_name=request.name,
        )

        # Log activity: storage volume creation success
        activity_logger.log_activity(
            action="storage.volume.create",
            target_type="storage_volume",
            target_id=request.name,
            target_host=host,
            result="success",
            details={"pool": request.pool, "type": request.type, "content_type": request.content_type}
        )
        
        schedule_orcahub_event("volume.created", host, getattr(volume, "project", None), request.name, resource_type="VOLUME")
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("storage_volumes")
        return {
            "success": True,
            "message": f"Storage volume {request.name} created successfully",
            "volume": StorageVolumeInfo(
                name=volume.name,
                type=volume.type,
                pool=request.pool,
                description=getattr(volume, 'description', '') or "",
                config=volume.config or {},
                used_by=volume.used_by or [],
                content_type=getattr(volume, 'content_type', ''),
                node=host
            )
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error creating storage volume: {e}")
        # Log activity: storage volume creation failure
        activity_logger.log_activity(
            action="storage.volume.create",
            target_type="storage_volume",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/{volume_type}/{volume_name}",
    response_model=dict,
    summary="Update storage volume",
    description="Update an existing storage volume.",
)
async def update_storage_volume(
    volume_type: str,
    volume_name: str,
    host: str = Query(..., description="Target host node"),
    pool: str = Query(..., description="Storage pool name"),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a storage volume."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        storage_pool = client.storage_pools.get(pool)
        volume = storage_pool.volumes.get(volume_type, volume_name)
        
        if config is not None:
            volume.config = config
        if description is not None and hasattr(volume, 'description'):
            volume.description = description
        
        volume.save()
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("storage_volumes")
        return {
            "success": True,
            "message": f"Storage volume {volume_name} updated successfully"
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error updating storage volume {volume_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# How many attachments the 409 names before it stops listing and starts counting.
_ATTACHMENTS_NAMED_IN_ERROR = 5


def _in_use_detail(volume_name: str, references: List[str]) -> str:
    """Name what is holding a volume, from its raw LXD used_by references.

    The references are API paths, ex; /1.0/instances/web-01?project=acme. Showing those to an
    operator is showing them the wire, so they are resolved through the same parser the tenant
    visibility filter uses rather than a second regex written here.
    """
    names = []
    for reference in references:
        _project, instance = parse_lxd_reference(reference)
        names.append(instance or reference)
    shown = ", ".join(names[:_ATTACHMENTS_NAMED_IN_ERROR])
    rest = len(names) - _ATTACHMENTS_NAMED_IN_ERROR
    if rest > 0:
        shown = f"{shown} and {rest} more"
    return (
        f"Storage volume '{volume_name}' is attached to {len(names)} instance(s): {shown}. "
        "Detach it from each before deleting."
    )


@router.delete(
    "/{volume_type}/{volume_name}",
    response_model=dict,
    summary="Delete storage volume",
    description=(
        "Delete a storage volume. Refuses with 409 when the volume is still attached to an "
        "instance; there is no force, because LXD cannot remove an attached volume."
    ),
    responses={
        200: {"description": "Volume deleted"},
        403: {"description": "Cluster out of scope"},
        404: {"description": "No such volume on that cluster"},
        409: {"description": "Volume still attached to an instance"},
        503: {"description": "Cluster unreachable"},
    },
)
async def delete_storage_volume(
    volume_type: str,
    volume_name: str,
    host: str = Query(..., description="Target host node"),
    pool: str = Query(..., description="Storage pool name"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a storage volume."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        storage_pool = client.storage_pools.get(pool)
        volume = storage_pool.volumes.get(volume_type, volume_name)

        # important: refuse before asking the daemon. LXD rejects an attached volume anyway,
        # but its message arrived here as a bare Exception and left as a 500, so an operator
        # error read as a server fault and the fix it needs was never stated. Unlike an image,
        # there is no force: an attached volume genuinely cannot be removed.
        attached = list(getattr(volume, "used_by", None) or [])
        if attached:
            raise HTTPException(status_code=409, detail=_in_use_detail(volume_name, attached))

        volume.delete()

        # Announce the lifecycle change to any subscribed add-on. Volumes have no operation in
        # the integrations proxy vocabulary, so this router is their only source: without it two
        # of the nine catalogued events could never fire at all.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.volume.deleted.v1",
            cluster_id=host,
            resource_type="volume",
            resource_name=volume_name,
        )

        # Log activity: storage volume deletion success
        activity_logger.log_activity(
            action="storage.volume.delete",
            target_type="storage_volume",
            target_id=volume_name,
            target_host=host,
            result="success",
            details={"pool": pool, "type": volume_type}
        )
        
        schedule_orcahub_event("volume.deleted", host, getattr(volume, "project", None), volume_name, resource_type="VOLUME")
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("storage_volumes")
        return {
            "success": True,
            "message": f"Storage volume {volume_name} deleted successfully"
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 and the 409 above through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error deleting storage volume {volume_name}: {e}")
        # Log activity: storage volume deletion failure
        activity_logger.log_activity(
            action="storage.volume.delete",
            target_type="storage_volume",
            target_id=volume_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        # note: was a blanket 500. The pre-check above catches the common in-use case, but the
        # daemon still owns the race, and a name it cannot resolve is a 404 rather than a fault.
        raise lxd_http_error(e, "Storage volume", volume_name)
