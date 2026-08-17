"""
API Router for LXD Operations.
Provides endpoints for monitoring and managing LXD operations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, check_tenant_permission
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/operations",
    tags=["Operations"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


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
    node_name: str = Field("", description="Node where operation is running")
    project: str = Field("default", description="Project for this operation")
    instance_name: str = Field("", description="Instance name if applicable")


class OperationListResponse(BaseModel):
    """Response for listing operations."""
    total: int = Field(..., description="Total number of operations")
    operations: List[OperationInfo] = Field(..., description="List of operations")
    nodes: List[str] = Field(..., description="List of nodes")


def _fetch_operations_for_node(lxd_service: LXDService, node_name: str) -> List[OperationInfo]:
    """Fetch operations for a single node - runs in thread pool."""
    operations = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Use recursion=1 to get full operation details
        response = client.api.operations.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            # Operations are returned in 'running' and 'success' keys
            running_ops = operations_data.get('running', [])
            
            for op_data in running_ops:
                try:
                    # Extract project and instance name from resources
                    project = "default"
                    instance_name = ""
                    resources = op_data.get('resources', {})
                    if resources:
                        # Try to extract from instance path
                        instances = resources.get('instances', [])
                        if instances and len(instances) > 0:
                            # Path format: /1.0/instances/name?project=xxx
                            instance_path = instances[0]
                            # Extract instance name
                            import re
                            match = re.search(r'/instances/([^?/]+)', instance_path)
                            if match:
                                instance_name = match.group(1)
                            # Extract project
                            if 'project=' in instance_path:
                                project = instance_path.split('project=')[-1].split('&')[0]
                    
                    operations.append(OperationInfo(
                        id=op_data.get('id', ''),
                        class_name=op_data.get('class', ''),
                        created_at=op_data.get('created_at', ''),
                        updated_at=op_data.get('updated_at', ''),
                        status=op_data.get('status', ''),
                        status_code=op_data.get('status_code', 0),
                        description=op_data.get('description', ''),
                        err=op_data.get('err', ''),
                        may_cancel=op_data.get('may_cancel', False),
                        metadata=op_data.get('metadata', {}),
                        node_name=node_name,
                        project=project,
                        instance_name=instance_name,
                    ))
                except Exception as e:
                    logger.error(f"Error parsing operation data on {node_name}: {e}")
        else:
            logger.warning(f"Got status {response.status_code} for operations on {node_name}")
    except Exception as e:
        logger.error(f"Error listing operations on {node_name}: {e}", exc_info=True)
    return operations


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
        if host:
            verify_cluster_access(host)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch operations from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_operations_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_operations = []
        for result in results:
            if isinstance(result, list):
                all_operations.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching operations: {result}")

        all_operations = [
            operation for operation in all_operations
            if check_tenant_permission(
                operation.node_name,
                operation.project or "default",
                operation.instance_name or None,
                "view",
            )
        ]
        
        return OperationListResponse(
            total=len(all_operations),
            operations=all_operations,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except HTTPException:
        # note: an authorization refusal is a 403, not a server fault. The generic handler
        # below would wrap it into a 500 whose body reads "403: Access denied ...", which
        # misleads clients and pages an on-call for a working permission check.
        raise
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
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Cancel an operation."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        # pylxd Operation objects don't have a cancel() method;
        # use the raw API to send DELETE to /1.0/operations/<uuid>
        client.api.operations[operation_id].delete()
        
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
