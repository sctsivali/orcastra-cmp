"""
API Router for Storage Volume operations.
Provides endpoints for managing LXD storage volumes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage/volumes", tags=["Storage Volumes"])

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


def _fetch_volumes_for_node(lxd_service: LXDService, node_name: str, pool_filter: Optional[str]) -> List[StorageVolumeInfo]:
    """Fetch storage volumes for a single node - runs in thread pool."""
    volumes_list = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
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
            except Exception as e:
                logger.error(f"Error listing volumes in pool {storage_pool.name}: {e}")
    except Exception as e:
        logger.error(f"Error listing storage volumes on {node_name}: {e}")
    return volumes_list


@router.get(
    "",
    response_model=StorageVolumeListResponse,
    summary="List all storage volumes",
    description="Get a list of all storage volumes from LXD nodes.",
)
async def list_storage_volumes(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    pool: Optional[str] = Query(None, description="Filter by storage pool"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all storage volumes."""
    # Try cache first (30 second cache)
    cache = get_cache_manager()
    cache_key = f"storage_volumes:list:host={host}:pool={pool}"
    cached_data = cache.get("api_responses", cache_key)
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
        for result in results:
            if isinstance(result, list):
                all_volumes.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching storage volumes: {result}")
        
        # Store in variable first
        response = StorageVolumeListResponse(
            total=len(all_volumes),
            volumes=all_volumes,
            nodes=lxd_service.connection_manager.get_node_names()
        )
        
        # Cache as dict for 120 seconds (enterprise TTL)
        cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
        logger.debug(f"Cached storage volumes list for 120 seconds")
        
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
    try:
        client = lxd_service.connection_manager.get_client(host)
        storage_pool = client.storage_pools.get(pool)
        volume = storage_pool.volumes.get(volume_type, volume_name)
        
        return StorageVolumeInfo(
            name=volume.name,
            type=volume.type,
            pool=pool,
            description=getattr(volume, 'description', '') or "",
            config=volume.config or {},
            used_by=volume.used_by or [],
            content_type=getattr(volume, 'content_type', '')
        )
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
):
    """Create a new storage volume."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        storage_pool = client.storage_pools.get(request.pool)
        
        volume = storage_pool.volumes.create({
            "name": request.name,
            "type": request.type,
            "config": request.config,
            "description": request.description,
            "content_type": request.content_type
        })
        
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
                content_type=getattr(volume, 'content_type', '')
            )
        }
    except Exception as e:
        logger.error(f"Error creating storage volume: {e}")
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
):
    """Update a storage volume."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        storage_pool = client.storage_pools.get(pool)
        volume = storage_pool.volumes.get(volume_type, volume_name)
        
        if config is not None:
            volume.config = config
        if description is not None and hasattr(volume, 'description'):
            volume.description = description
        
        volume.save()
        
        return {
            "success": True,
            "message": f"Storage volume {volume_name} updated successfully"
        }
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
):
    """Delete a storage volume."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        storage_pool = client.storage_pools.get(pool)
        volume = storage_pool.volumes.get(volume_type, volume_name)
        volume.delete()
        
        return {
            "success": True,
            "message": f"Storage volume {volume_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting storage volume {volume_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
