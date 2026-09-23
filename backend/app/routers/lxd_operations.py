"""
API Router for LXD Operations.
Provides endpoints for monitoring and managing LXD operations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.dependencies import check_read_only_mode, require_partner, AuthenticatedUser
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    check_tenant_permission,
    project_is_visible,
)
from app.core.instance_access import operation_projects
from pydantic import BaseModel, Field, PrivateAttr
from app.core.lxd_names import safe_segment
from app.services.lxd.utils import read_running_operations, strip_operation_secrets

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/operations",
    tags=["Operations"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class OperationInfo(BaseModel):
    """Operation information."""

    # note: the raw resources, kept off the response, so the list can apply the project bound on
    # the event loop where the scope is set. The fetch runs in an executor, which has none.
    _resources: Dict[str, Any] = PrivateAttr(default_factory=dict)

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


def _operation_is_visible(host: str, resources: Any) -> bool:
    """Whether every resource an operation names sits inside this caller's project bound.

    important: an operation id is enough to read, wait on or cancel any operation on a host, and
    on a cluster bound to two organizations the other organization's console sessions, instance
    creates, volume, image and network work and project deletes are operations too. Every
    reference is checked, not only instances. One that names no project is cluster-level.
    """
    projects = operation_projects({"resources": resources if isinstance(resources, dict) else {}})
    return all(project_is_visible(host, project) for project in projects)


def _read_visible_operation(client: Any, host: str, operation_id: str) -> Any:
    """The operation, or a 404 the same as a missing one when it is outside the bound."""
    operation = client.operations.get(safe_segment(operation_id))
    if not _operation_is_visible(host, getattr(operation, "resources", None)):
        raise HTTPException(status_code=404, detail=f"Operation {operation_id} not found")
    return operation


class OperationListResponse(BaseModel):
    """Response for listing operations."""
    total: int = Field(..., description="Total number of operations")
    operations: List[OperationInfo] = Field(..., description="List of operations")
    nodes: List[str] = Field(..., description="List of nodes")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their entries are missing from this list, "
            "so an unreachable node is distinguishable from one that has none."
        ),
    )


def _fetch_operations_for_node(lxd_service: LXDService, node_name: str) -> List[OperationInfo]:
    """Fetch operations for a single node - runs in thread pool."""
    operations = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        if client is None:
            # important: unreachable is not empty. Without this the next line
            # raises AttributeError on None, which the generic handler below
            # turns into a partial result and the node is never named.
            raise LXDConnectionError(f"Could not connect to {node_name}")
        # Use recursion=1 to get full operation details
        response = read_running_operations(client)
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
                    
                    info = OperationInfo(
                        id=op_data.get('id', ''),
                        class_name=op_data.get('class', ''),
                        created_at=op_data.get('created_at', ''),
                        updated_at=op_data.get('updated_at', ''),
                        status=op_data.get('status', ''),
                        status_code=op_data.get('status_code', 0),
                        description=op_data.get('description', ''),
                        err=op_data.get('err', ''),
                        may_cancel=op_data.get('may_cancel', False),
                        metadata=strip_operation_secrets(op_data.get('metadata')),
                        node_name=node_name,
                        project=project,
                        instance_name=instance_name,
                    )
                    info._resources = resources if isinstance(resources, dict) else {}
                    operations.append(info)
                except LXDConnectionError:
                    # a missing client IS unreachability: let it through
                    raise
                except Exception as e:
                    # important: not unreachability. See networks.py: an item
                    # vanishing mid-listing must not condemn the whole node.
                    logger.warning(f"Error parsing operation data on {node_name}: {e}")
        else:
            logger.warning(f"Got status {response.status_code} for operations on {node_name}")
    except LXDConnectionError:
        # a missing client IS unreachability: let it through
        raise
    except Exception as e:
        # important: not unreachability. See networks.py: an item
        # vanishing mid-listing must not condemn the whole node.
        logger.warning(f"Error listing operations on {node_name}: {e}", exc_info=True)
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
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                all_operations.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching operations: {result}")
                unreachable_nodes.append(node_name)

        # note: the project bound as well as the ACL. A Partner has no ACL, so without the bound
        # a shared cluster listed the other organization's operations, instance names included.
        all_operations = [
            operation for operation in all_operations
            if _operation_is_visible(operation.node_name, operation._resources)
            and project_is_visible(operation.node_name, operation.project or "default")
            and check_tenant_permission(
                operation.node_name,
                operation.project or "default",
                operation.instance_name or None,
                "view",
            )
        ]
        
        return OperationListResponse(
            total=len(all_operations),
            operations=all_operations,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
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
    verify_cluster_access(host)
    try:
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        operation = await asyncio.to_thread(_read_visible_operation, client, host, operation_id)
        
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
            metadata=strip_operation_secrets(getattr(operation, 'metadata', None)),
        )
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
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
    verify_cluster_access(host)
    try:
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        await asyncio.to_thread(_read_visible_operation, client, host, operation_id)
        # pylxd Operation objects don't have a cancel() method;
        # use the raw API to send DELETE to /1.0/operations/<uuid>
        client.api.operations[safe_segment(operation_id)].delete()
        
        return {
            "success": True,
            "message": f"Operation {operation_id} cancelled successfully"
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
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
    verify_cluster_access(host)
    try:
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        operation = await asyncio.to_thread(_read_visible_operation, client, host, operation_id)
        operation.wait(timeout=timeout)
        
        return {
            "success": True,
            "message": f"Operation {operation_id} completed",
            "status": getattr(operation, 'status', ''),
            "status_code": getattr(operation, 'status_code', 0)
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error waiting for operation {operation_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
