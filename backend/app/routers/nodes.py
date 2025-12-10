"""
API Router for LXD node status and health checks.
Provides endpoints for monitoring node connectivity.
"""

from fastapi import APIRouter, Depends
import logging

from app.models.schemas import (
    NodeStatus,
    NodesStatusResponse,
    NodeResources,
    NodeUpdateRequest,
    NodeUpdateResponse,
)
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    LXDOperationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nodes", tags=["Nodes"])


@router.get(
    "",
    response_model=NodesStatusResponse,
    summary="Get nodes status",
    description="Check connectivity and status of all configured LXD nodes.",
    responses={
        200: {"description": "Node status retrieved successfully"},
    }
)
async def get_nodes_status(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get status of all LXD nodes."""
    statuses = lxd_service.check_nodes_status()
    
    return NodesStatusResponse(
        nodes=statuses,
        all_connected=all(node.connected for node in statuses),
    )


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
    try:
        resources = lxd_service.get_node_resources(node_name)
        return NodeResources(**resources)
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
):
    """Update node metadata (display name)."""
    from fastapi import HTTPException
    from app.core.node_metadata import get_node_metadata_store
    
    # Check if node exists
    if node_name not in lxd_service.get_node_names():
        raise HTTPException(
            status_code=404,
            detail=f"Node '{node_name}' not found"
        )
    
    try:
        # Update display name
        metadata_store = get_node_metadata_store()
        metadata_store.set_node_display_name(node_name, request.display_name)
        
        return NodeUpdateResponse(
            success=True,
            message=f"Node '{node_name}' updated successfully",
            node_name=node_name,
            display_name=request.display_name,
        )
    except Exception as e:
        logger.error(f"Error updating node {node_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
