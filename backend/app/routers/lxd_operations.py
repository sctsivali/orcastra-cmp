"""
API Router for LXD Operations.
Provides endpoints for monitoring and managing LXD operations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging
from datetime import datetime

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/operations", tags=["Operations"])


class OperationInfo(BaseModel):
    """Operation information."""
    id: str = Field(..., description="Operation ID")
    class_name: str = Field("", description="Operation class")
    created_at: str = Field("", description="Creation timestamp")
    updated_at: str = Field("", description="Last update timestamp")
    status: str = Field("", description="Operation status")
    status_code: int = Field(0, description="Status code")
    description: str = Field("", description="Operation description")
    err: str = Field("", description="Error message if any")
    may_cancel: bool = Field(False, description="Whether operation can be cancelled")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Operation metadata")


class OperationListResponse(BaseModel):
    """Response for listing operations."""
    total: int = Field(..., description="Total number of operations")
    operations: List[OperationInfo] = Field(..., description="List of operations")
    nodes: List[str] = Field(..., description="List of nodes")


@router.get(
    "",
    response_model=OperationListResponse,
    summary="List all operations",
    description="Get a list of all operations from LXD nodes.",
)
async def list_operations(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all operations."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_operations = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                operations = client.operations.all()
                for operation in operations:
                    all_operations.append(OperationInfo(
                        id=operation.id,
                        class_name=getattr(operation, 'class', ''),
                        created_at=getattr(operation, 'created_at', ''),
                        updated_at=getattr(operation, 'updated_at', ''),
                        status=getattr(operation, 'status', ''),
                        status_code=getattr(operation, 'status_code', 0),
                        description=getattr(operation, 'description', ''),
                        err=getattr(operation, 'err', ''),
                        may_cancel=getattr(operation, 'may_cancel', False),
                        metadata=getattr(operation, 'metadata', {})
                    ))
            except Exception as e:
                logger.error(f"Error listing operations on {node_name}: {e}")
        
        return OperationListResponse(
            total=len(all_operations),
            operations=all_operations,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing operations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{operation_id}",
    response_model=OperationInfo,
    summary="Get operation details",
    description="Get detailed information about a specific operation.",
)
async def get_operation(
    operation_id: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific operation."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        operation = client.operations.get(operation_id)
        
        return OperationInfo(
            id=operation.id,
            class_name=getattr(operation, 'class', ''),
            created_at=getattr(operation, 'created_at', ''),
            updated_at=getattr(operation, 'updated_at', ''),
            status=getattr(operation, 'status', ''),
            status_code=getattr(operation, 'status_code', 0),
            description=getattr(operation, 'description', ''),
            err=getattr(operation, 'err', ''),
            may_cancel=getattr(operation, 'may_cancel', False),
            metadata=getattr(operation, 'metadata', {})
        )
    except Exception as e:
        logger.error(f"Error getting operation {operation_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Operation {operation_id} not found")


@router.delete(
    "/{operation_id}",
    response_model=dict,
    summary="Cancel operation",
    description="Cancel a running operation.",
)
async def cancel_operation(
    operation_id: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Cancel an operation."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        operation = client.operations.get(operation_id)
        operation.cancel()
        
        return {
            "success": True,
            "message": f"Operation {operation_id} cancelled successfully"
        }
    except Exception as e:
        logger.error(f"Error cancelling operation {operation_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{operation_id}/wait",
    response_model=dict,
    summary="Wait for operation",
    description="Wait for an operation to complete.",
)
async def wait_operation(
    operation_id: str,
    host: str = Query(..., description="Target host node"),
    timeout: Optional[int] = Query(30, description="Timeout in seconds"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Wait for an operation to complete."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        operation = client.operations.get(operation_id)
        operation.wait(timeout=timeout)
        
        return {
            "success": True,
            "message": f"Operation {operation_id} completed",
            "status": getattr(operation, 'status', ''),
            "status_code": getattr(operation, 'status_code', 0)
        }
    except Exception as e:
        logger.error(f"Error waiting for operation {operation_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
