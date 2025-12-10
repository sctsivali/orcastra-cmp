"""
API Router for LXD image operations.
Provides endpoints for listing available images on each node.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List
import logging

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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/images", tags=["Images"])


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
    results = []
    
    for node_name in lxd_service.get_node_names():
        try:
            images = await lxd_service.list_images(node_name)
            results.append(ImageListResponse(
                host=node_name,
                images=images,
                total=len(images),
            ))
        except Exception as e:
            logger.warning(f"Could not list images from {node_name}: {e}")
            results.append(ImageListResponse(
                host=node_name,
                images=[],
                total=0,
            ))
    
    return results


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
