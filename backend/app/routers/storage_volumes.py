"""
API Router for Storage Volume operations.
Provides endpoints for managing LXD storage volumes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from pylxd.exceptions import LXDAPIException

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect
from app.services.lxd.project_scope import (
    ALL_PROJECTS_READ_TIMEOUT,
    DEFAULT_PROJECT,
    list_pool_volumes,
)
from app.models.schemas import FanoutReachability
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.tenant_visibility import filter_visible_lxd_references, tenant_acl_active
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field
from app.services.activity_logger import get_activity_logger
from app.core.lxd_names import safe_segment

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
    project: str = Field(DEFAULT_PROJECT, description="Project that holds the volume")
    location: str = Field(
        "",
        description="Cluster member that holds the volume; empty for a shared pool or a single host",
    )


class StorageVolumeCreateRequest(BaseModel):
    """Request body for creating a storage volume."""
    name: str = Field(..., description="Volume name")
    pool: str = Field(..., description="Storage pool name")
    type: str = Field("custom", description="Volume type")
    description: str = Field("", description="Volume description")
    config: Dict[str, str] = Field(default_factory=dict, description="Volume configuration")
    content_type: str = Field("filesystem", description="Content type")


class StorageVolumeListResponse(FanoutReachability):
    """Response for listing storage volumes."""
    total: int = Field(..., description="Total number of volumes")
    volumes: List[StorageVolumeInfo] = Field(..., description="List of storage volumes")
    nodes: List[str] = Field(..., description="List of nodes")


def _pool_names(client, pool_filter: Optional[str]) -> List[str]:
    """The pools to read, from one recursion=0 listing (or the filter alone)."""
    if pool_filter:
        return [pool_filter]
    response = client.api.storage_pools.get(
        params={"recursion": 0}, timeout=ALL_PROJECTS_READ_TIMEOUT
    )
    names: List[str] = []
    for entry in response.json().get("metadata") or []:
        if isinstance(entry, str):
            names.append(entry.rstrip("/").rsplit("/", 1)[-1].split("?")[0])
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(entry["name"])
    return names


def _fetch_volumes_for_node(
    lxd_service: LXDService, node_name: str, pool_filter: Optional[str]
) -> NodeRead[StorageVolumeInfo]:
    """Every volume on one cluster, one request per pool. Runs in the thread pool.

    important: pylxd's collection manager listed at recursion 0 in the default project and
    then synced every volume one request at a time. On a clustered host a per-member pool
    lists its volumes as `.../volumes/custom/backups?target=microcloud20`, which the sync
    could not read, and the first refusal dropped the rest of that pool. Nothing is synced
    now, every project is read, and a pool that cannot be read is named, not skipped.
    """
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. See the sibling routers.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    read: NodeRead[StorageVolumeInfo] = NodeRead()
    for pool_name in _pool_names(client, pool_filter):
        try:
            rows, complete = list_pool_volumes(client, pool_name)
        except LXDAPIException as exc:
            # note: only LXD's own refusal is a per-pool problem; a transport error means the
            # rest of the cluster cannot be read either and is classified by the caller.
            read.lost(f"pool {pool_name}: LXD answered {getattr(exc.response, 'status_code', '?')}: "
                      f"{' '.join(str(exc).split())[:200]}")
            continue
        if not complete:
            read.lost(f"pool {pool_name}: not every project could be read")
        for row in rows:
            try:
                read.rows.append(StorageVolumeInfo(
                    name=row.get("name") or "",
                    type=row.get("type") or "",
                    pool=pool_name,
                    description=row.get("description") or "",
                    config=row.get("config") or {},
                    used_by=row.get("used_by") or [],
                    content_type=row.get("content_type") or "",
                    node=node_name,
                    project=row.get("project") or DEFAULT_PROJECT,
                    location=row.get("location") or "",
                ))
            except Exception as exc:  # noqa: BLE001 - one malformed row is named, not fatal
                read.lost(f"volume {row.get('name', '?')} in pool {pool_name}: {type(exc).__name__}")
    return read


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
        
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        fan = collect(
            nodes_to_query, results, noun="storage volumes", reasons_visible=not tenant_acl_active()
        )
        unreachable_nodes.extend(fan.unreachable_nodes)
        all_volumes = fan.rows
        cacheable = fan.cacheable

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
            partial_nodes=fan.partial_nodes,
            node_errors=fan.node_errors,
        )
        
        # important: an outage must not be remembered as the answer, and neither may a
        # partial one: a cluster that failed its listing was cached as "no volumes" for
        # 180 seconds. See instances.py.
        if cacheable:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
            logger.debug("Cached storage volumes list for 180 seconds")
        else:
            logger.warning("Not caching storage volumes list: %s", fan.node_errors or "no node answered")

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
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        storage_pool = client.storage_pools.get(safe_segment(pool))
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
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        storage_pool = client.storage_pools.get(safe_segment(request.pool))
        
        volume = storage_pool.volumes.create({
            "name": request.name,
            "type": request.type,
            "config": request.config,
            "description": request.description,
            "content_type": request.content_type
        })
        
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
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        storage_pool = client.storage_pools.get(safe_segment(pool))
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


@router.delete(
    "/{volume_type}/{volume_name}",
    response_model=dict,
    summary="Delete storage volume",
    description="Delete a storage volume.",
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
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        storage_pool = client.storage_pools.get(safe_segment(pool))
        volume = storage_pool.volumes.get(volume_type, volume_name)
        volume.delete()
        
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
        # important: let require_client's 503 through. Without this the
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
        raise HTTPException(status_code=500, detail=str(e))
