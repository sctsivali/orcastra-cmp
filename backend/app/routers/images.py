"""
API Router for LXD image operations.
Provides endpoints for listing available images on each node.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    ImageInfo,
    ImageListResponse,
    ErrorResponse,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    get_lxd_service,
)
from app.core.cache import get_cache_manager
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/images",
    tags=["Images"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


async def _fetch_images_for_node(lxd_service: LXDService, node_name: str) -> ImageListResponse:
    """Fetch images for a single node."""
    try:
        images = await lxd_service.list_images(node_name)
        return ImageListResponse(
            host=node_name,
            images=images,
            total=len(images),
        )
    except Exception as e:
        logger.warning(f"Could not list images from {node_name}: {e}")
        return ImageListResponse(
            host=node_name,
            images=[],
            total=0,
        )


@router.get(
    "",
    response_model=List[ImageListResponse],
    summary="List images from all nodes",
    description="Get a list of available images from all connected LXD nodes.",
    responses={
        200: {"description": "Images listed successfully"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_all_images(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all images from all LXD nodes."""
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("images:list:all")
    cached_data = cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached images list")
        # Reconstruct Pydantic models from cached dict
        return [ImageListResponse(**item) for item in cached_data]
    
    # Fetch images from all nodes in parallel
    node_names = lxd_service.get_node_names()
    tasks = [_fetch_images_for_node(lxd_service, node_name) for node_name in node_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Filter out exceptions
    response = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Error fetching images from node: {result}")
            response.append(ImageListResponse(
                host=node_names[i] if i < len(node_names) else "unknown",
                images=[],
                total=0,
            ))
        else:
            response.append(result)
    
    # Cache as dict for 120 seconds
    cache_data = [item.model_dump() for item in response]
    cache.set("api_responses", cache_key, cache_data, ttl=180)
    logger.debug(f"Cached images list for 120 seconds")
    
    return response


@router.get(
    "/{host}",
    response_model=ImageListResponse,
    summary="List images from specific node",
    description="Get a list of available images from a specific LXD node.",
    responses={
        200: {"description": "Images listed successfully"},
        400: {"model": ErrorResponse, "description": "Invalid host"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_node_images(
    host: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all images from a specific LXD node."""
    verify_cluster_access(host)
    try:
        # Validate host
        valid_nodes = lxd_service.get_node_names()
        if host not in valid_nodes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid host. Valid hosts: {valid_nodes}"
            )
        
        images = await lxd_service.list_images(host)
        
        return ImageListResponse(
            host=host,
            images=images,
            total=len(images),
        )
        
    except HTTPException:
        raise
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing images from {host}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
