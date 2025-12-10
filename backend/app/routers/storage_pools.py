"""
API Router for Storage Pool operations.
Provides endpoints for managing LXD storage pools.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage/pools", tags=["Storage Pools"])


class StoragePoolInfo(BaseModel):
    """Storage pool information."""
    name: str = Field(..., description="Pool name")
    description: str = Field("", description="Pool description")
    driver: str = Field(..., description="Storage driver (zfs, dir, btrfs, etc)")
    config: Dict[str, str] = Field(default_factory=dict, description="Pool configuration")
    used_by: List[str] = Field(default_factory=list, description="Resources using this pool")
    status: str = Field("", description="Pool status")
    node: str = Field("", description="Node name where pool is defined")


class StoragePoolCreateRequest(BaseModel):
    """Request body for creating a storage pool."""
    name: str = Field(..., description="Pool name")
    description: str = Field("", description="Pool description")
    driver: str = Field(..., description="Storage driver")
    config: Dict[str, str] = Field(default_factory=dict, description="Pool configuration")


class StoragePoolListResponse(BaseModel):
    """Response for listing storage pools."""
    total: int = Field(..., description="Total number of pools")
    pools: List[StoragePoolInfo] = Field(..., description="List of storage pools")
    nodes: List[str] = Field(..., description="List of nodes")


@router.get(
    "",
    response_model=StoragePoolListResponse,
    summary="List all storage pools",
    description="Get a list of all storage pools from LXD nodes.",
)
async def list_storage_pools(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all storage pools."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_pools = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                pools = client.storage_pools.all()
                for pool in pools:
                    all_pools.append(StoragePoolInfo(
                        name=pool.name,
                        description=getattr(pool, 'description', '') or "",
                        driver=pool.driver,
                        config=pool.config or {},
                        used_by=pool.used_by or [],
                        status=getattr(pool, 'status', ''),
                        node=node_name
                    ))
            except Exception as e:
                logger.error(f"Error listing storage pools on {node_name}: {e}")
        
        return StoragePoolListResponse(
            total=len(all_pools),
            pools=all_pools,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing storage pools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/resources-all",
    summary="Get all storage pool resources",
    description="Get resources (space, inodes) for all storage pools across all nodes.",
)
async def get_all_storage_pool_resources(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get resources for all storage pools on all nodes."""
    try:
        all_resources = {}
        nodes = lxd_service.connection_manager.get_node_names()
        
        for node_name in nodes:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                pools = client.storage_pools.all()
                
                for pool in pools:
                    try:
                        resources = pool.resources.get()
                        pool_key = f"{node_name}-{pool.name}"
                        all_resources[pool_key] = resources
                    except Exception as pool_err:
                        logger.warning(f"Could not get resources for pool {pool.name} on {node_name}: {pool_err}")
                        continue
            except Exception as node_err:
                logger.warning(f"Could not access node {node_name}: {node_err}")
                continue
        
        return {"resources": all_resources}
    except Exception as e:
        logger.error(f"Error getting all storage pool resources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{pool_name}",
    response_model=StoragePoolInfo,
    summary="Get storage pool details",
    description="Get detailed information about a specific storage pool.",
)
async def get_storage_pool(
    pool_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific storage pool."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        pool = client.storage_pools.get(pool_name)
        
        return StoragePoolInfo(
            name=pool.name,
            description=getattr(pool, 'description', '') or "",
            driver=pool.driver,
            config=pool.config or {},
            used_by=pool.used_by or [],
            status=getattr(pool, 'status', '')
        )
    except Exception as e:
        logger.error(f"Error getting storage pool {pool_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Storage pool {pool_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create storage pool",
    description="Create a new storage pool.",
)
async def create_storage_pool(
    request: StoragePoolCreateRequest,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Create a new storage pool."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        pool = client.storage_pools.create({
            "name": request.name,
            "driver": request.driver,
            "config": request.config,
            "description": request.description
        })
        
        return {
            "success": True,
            "message": f"Storage pool {request.name} created successfully",
            "pool": StoragePoolInfo(
                name=pool.name,
                description=getattr(pool, 'description', '') or "",
                driver=pool.driver,
                config=pool.config or {},
                used_by=pool.used_by or [],
                status=getattr(pool, 'status', '')
            )
        }
    except Exception as e:
        logger.error(f"Error creating storage pool: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/{pool_name}",
    response_model=dict,
    summary="Update storage pool",
    description="Update an existing storage pool.",
)
async def update_storage_pool(
    pool_name: str,
    host: str = Query(..., description="Target host node"),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Update a storage pool."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        pool = client.storage_pools.get(pool_name)
        
        if config is not None:
            pool.config = config
        if description is not None and hasattr(pool, 'description'):
            pool.description = description
        
        pool.save()
        
        return {
            "success": True,
            "message": f"Storage pool {pool_name} updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating storage pool {pool_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{pool_name}",
    response_model=dict,
    summary="Delete storage pool",
    description="Delete a storage pool.",
)
async def delete_storage_pool(
    pool_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a storage pool."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        pool = client.storage_pools.get(pool_name)
        pool.delete()
        
        return {
            "success": True,
            "message": f"Storage pool {pool_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting storage pool {pool_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "-resources",
    response_model=dict,
    summary="Get all storage pool resources",
    description="Get resource usage information for all storage pools across all nodes.",
)
async def get_all_storage_pool_resources(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get storage pool resources for all pools."""
    try:
        all_resources = {}
        nodes = lxd_service.connection_manager.get_node_names()
        
        for node_name in nodes:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                pools = client.storage_pools.all()
                
                for pool in pools:
                    try:
                        resources = pool.resources.get()
                        pool_key = f"{node_name}-{pool.name}"
                        all_resources[pool_key] = resources
                    except Exception as e:
                        logger.warning(f"Could not get resources for pool {pool.name} on {node_name}: {e}")
            except Exception as e:
                logger.error(f"Error getting pools on {node_name}: {e}")
        
        return {"resources": all_resources}
    except Exception as e:
        logger.error(f"Error getting storage pool resources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{pool_name}/resources",
    response_model=dict,
    summary="Get storage pool resources",
    description="Get resource usage information for a specific storage pool.",
)
async def get_storage_pool_resources(
    pool_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get storage pool resources."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        pool = client.storage_pools.get(pool_name)
        resources = pool.resources.get()
        
        return {
            "name": pool_name,
            "resources": resources
        }
    except Exception as e:
        logger.error(f"Error getting storage pool resources {pool_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
