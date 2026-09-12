"""
API Router for Network ACL operations.
Provides endpoints for managing LXD network ACLs.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect
from app.models.schemas import FanoutReachability
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    project_is_visible,
)
from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    list_across_projects,
    list_in_project,
    project_params,
    resolve_owner,
)
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    tenant_acl_active,
    tenant_may_see_project,
)
from app.services.activity_logger import get_activity_logger
from pydantic import BaseModel, Field, field_validator
from app.core.lxd_names import safe_segment
from app.services.lxd.utils import wait_for_lxd_operation, write_timeout

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/network-acls",
    tags=["Network ACLs"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)
# an ACL write on LXD 6.9 is an operation; see NETWORK_WRITE_WAIT_SECONDS in networks.py
ACL_WRITE_WAIT_SECONDS = 60


class NetworkACLInfo(BaseModel):
    """Network ACL information."""
    name: str = Field(..., description="ACL name")
    description: str = Field("", description="ACL description")
    ingress: List[Dict[str, Any]] = Field(default_factory=list, description="Ingress rules")
    egress: List[Dict[str, Any]] = Field(default_factory=list, description="Egress rules")
    config: Dict[str, str] = Field(default_factory=dict, description="ACL configuration")
    used_by: List[str] = Field(default_factory=list, description="Resources using this ACL")
    node: str = Field("", description="Node name")
    project: str = Field(
        DEFAULT_PROJECT,
        description="LXD project that holds this ACL",
    )


# LXD's own rule for an ACL name. Checked here so a bad name is a 422 at the boundary, not
# the daemon's refusal surfacing as a 500.
_ACL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")


def _http_status_for(exc: Exception) -> int:
    """The daemon's own 4xx is the caller's mistake and keeps its status; anything else is ours."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if status in (400, 404, 409) else 500


class NetworkACLCreateRequest(BaseModel):
    """Request body for creating a network ACL."""
    name: str = Field(..., min_length=1, max_length=63, description="ACL name")
    description: str = Field("", description="ACL description")
    ingress: List[Dict[str, Any]] = Field(default_factory=list, description="Ingress rules")
    egress: List[Dict[str, Any]] = Field(default_factory=list, description="Egress rules")
    config: Dict[str, str] = Field(default_factory=dict, description="ACL configuration")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _ACL_NAME_RE.match(v):
            raise ValueError(
                "Name must start with a letter and contain only letters, numbers and hyphens"
            )
        return v


class NetworkACLListResponse(FanoutReachability):
    """Response for listing network ACLs."""
    total: int = Field(..., description="Total number of ACLs")
    acls: List[NetworkACLInfo] = Field(..., description="List of network ACLs")
    nodes: List[str] = Field(..., description="List of nodes")


def _fetch_acls_for_node(
    lxd_service: LXDService, node_name: str, project: Optional[str] = None
) -> NodeRead[NetworkACLInfo]:
    """Fetch network ACLs for a single node - runs in thread pool.

    LXD scopes network ACLs by project and gives them no feature key of their own, so they
    follow features.networks: a project holding its own networks holds its own ACLs too. A
    read that could not cover every project is a partial read; a listing that failed
    propagates and the caller names the cluster.
    """
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. Without this the next line
        # raises AttributeError on None, which the generic handler below
        # turns into a partial result and the node is never named.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    read: NodeRead[NetworkACLInfo] = NodeRead()
    if project:
        owner = resolve_owner(client, "network-acls", project)
        rows = list_in_project(client, "network-acls", owner)
    else:
        rows, complete = list_across_projects(client, "network-acls")
        if not complete:
            read.lost("not every project could be read")
    for acl_data in rows:
        try:
            read.rows.append(NetworkACLInfo(
                name=acl_data.get('name', ''),
                description=acl_data.get('description', ''),
                ingress=acl_data.get('ingress', []),
                egress=acl_data.get('egress', []),
                config=acl_data.get('config', {}),
                used_by=acl_data.get('used_by', []),
                node=node_name,
                project=acl_data.get('project') or DEFAULT_PROJECT,
            ))
        except Exception as exc:  # noqa: BLE001 - one malformed row is named, not fatal
            read.lost(f"ACL {acl_data.get('name', '?')}: {type(exc).__name__}")
    return read


@router.get(
    "",
    response_model=NetworkACLListResponse,
    summary="List all network ACLs",
    description="Get a list of all network ACLs from LXD nodes.",
)
async def list_network_acls(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project to read. Omit for every project, each row naming the one that "
            "holds it."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all network ACLs."""
    try:
        if host:
            verify_cluster_access(host)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch ACLs from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(
                _executor, _fetch_acls_for_node, lxd_service, node_name, project
            )
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
            nodes_to_query, results, noun="network ACLs", reasons_visible=not tenant_acl_active()
        )
        unreachable_nodes.extend(fan.unreachable_nodes)
        all_acls = fan.rows

        # important: a Partner is scoped by cluster, not by project, and the used_by filter
        # below never runs for them. An ACL body is a firewall policy, so on a shared cluster
        # this is the only thing keeping one organization's rules out of another's console.
        all_acls = [a for a in all_acls if project_is_visible(a.node, a.project)]

        if tenant_acl_active():
            visible_acls = []
            for acl in all_acls:
                # ownership as well as usage: see tenant_may_see_project
                if not tenant_may_see_project(acl.node, acl.project):
                    continue
                acl.used_by = filter_visible_lxd_references(acl.node, acl.used_by)
                if acl.used_by:
                    visible_acls.append(acl)
            all_acls = visible_acls
        
        return NetworkACLListResponse(
            total=len(all_acls),
            acls=all_acls,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
            partial_nodes=sorted(fan.partial_nodes),
            node_errors=fan.node_errors,
        )
    except HTTPException:
        # note: an authorization refusal is a 403, not a server fault. The generic handler
        # below would wrap it into a 500 whose body reads "403: Access denied ...", which
        # misleads clients and pages an on-call for a working permission check.
        raise
    except Exception as e:
        logger.error(f"Error listing network ACLs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{acl_name}",
    response_model=NetworkACLInfo,
    summary="Get network ACL details",
    description="Get detailed information about a specific network ACL.",
)
async def get_network_acl(
    acl_name: str,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project holding the ACL (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific network ACL."""
    try:
        verify_cluster_access(host)
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        owner = resolve_owner(client, "network-acls", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network ACL")
        scope = project_params(owner)
        response = client.api["network-acls"][safe_segment(acl_name)].get(params=scope)
        
        if response.status_code == 200:
            acl_data = response.json().get('metadata', {})
            used_by = filter_visible_lxd_references(host, acl_data.get('used_by', []))
            if tenant_acl_active() and not used_by:
                raise HTTPException(status_code=403, detail="Access denied to this network ACL")
            return NetworkACLInfo(
                name=acl_data.get('name', ''),
                description=acl_data.get('description', ''),
                ingress=acl_data.get('ingress', []),
                egress=acl_data.get('egress', []),
                config=acl_data.get('config', {}),
                used_by=used_by,
                node=host,
                project=acl_data.get('project') or owner,
            )
        else:
            raise HTTPException(status_code=404, detail=f"Network ACL {acl_name} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting network ACL {acl_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Network ACL {acl_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create network ACL",
    description="Create a new network ACL.",
)
async def create_network_acl(
    request: NetworkACLCreateRequest,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project holding the ACL (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new network ACL."""
    verify_cluster_access(host)
    try:
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        owner = resolve_owner(client, "network-acls", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network ACL")
        scope = project_params(owner)
        
        acl_data = {
            "name": request.name,
            "description": request.description,
            "ingress": request.ingress,
            "egress": request.egress,
            "config": request.config
        }
        
        response = client.api["network-acls"].post(
            json=acl_data, params=scope, timeout=write_timeout(ACL_WRITE_WAIT_SECONDS)
        )
        # LXD 6.9 answers with an operation (storage_and_network_operations); settle it before
        # reporting success, or the list right after the create does not hold the ACL yet
        wait_for_lxd_operation(client, response, timeout=ACL_WRITE_WAIT_SECONDS)
        
        if response.status_code in [200, 201, 202]:
            # Log activity for audit trail
            activity_logger.log_activity(
                action="network.acl.create",
                target_type="network_acl",
                target_id=request.name,
                target_host=host,
                result="success",
                details={
                    "description": request.description,
                    "ingress_rules": len(request.ingress),
                    "egress_rules": len(request.egress),
                }
            )
            return {
                "success": True,
                "message": f"Network ACL {request.name} created successfully"
            }
        else:
            activity_logger.log_activity(
                action="network.acl.create",
                target_type="network_acl",
                target_id=request.name,
                target_host=host,
                result="failure",
                error_code="LXDError",
                error_message="Failed to create network ACL",
            )
            raise HTTPException(status_code=500, detail="Failed to create network ACL")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating network ACL: {e}")
        activity_logger.log_activity(
            action="network.acl.create",
            target_type="network_acl",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_code=type(e).__name__,
            error_message=str(e),
        )
        raise HTTPException(status_code=_http_status_for(e), detail=str(e))


@router.put(
    "/{acl_name}",
    response_model=dict,
    summary="Update network ACL",
    description="Update an existing network ACL.",
)
async def update_network_acl(
    acl_name: str,
    host: str = Query(..., description="Target host node"),
    description: Optional[str] = Body(None),
    ingress: Optional[List[Dict[str, Any]]] = Body(None),
    egress: Optional[List[Dict[str, Any]]] = Body(None),
    config: Optional[Dict[str, str]] = Body(None),
    project: Optional[str] = Query(
        None, description="LXD project holding the ACL (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a network ACL."""
    verify_cluster_access(host)
    try:
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        owner = resolve_owner(client, "network-acls", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network ACL")
        scope = project_params(owner)
        
        # Get current ACL
        get_response = client.api["network-acls"][safe_segment(acl_name)].get(params=scope)
        if get_response.status_code != 200:
            raise HTTPException(status_code=404, detail=f"Network ACL {acl_name} not found")
        
        acl_data = get_response.json().get('metadata', {})
        
        # Update fields
        if description is not None:
            acl_data['description'] = description
        if ingress is not None:
            acl_data['ingress'] = ingress
        if egress is not None:
            acl_data['egress'] = egress
        if config is not None:
            acl_data['config'] = config
        
        # Put updated ACL
        put_response = client.api["network-acls"][safe_segment(acl_name)].put(
            json=acl_data, params=scope, timeout=write_timeout(ACL_WRITE_WAIT_SECONDS)
        )
        wait_for_lxd_operation(client, put_response, timeout=ACL_WRITE_WAIT_SECONDS)
        
        if put_response.status_code in [200, 202]:
            return {
                "success": True,
                "message": f"Network ACL {acl_name} updated successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to update network ACL")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating network ACL {acl_name}: {e}")
        raise HTTPException(status_code=_http_status_for(e), detail=str(e))


@router.delete(
    "/{acl_name}",
    response_model=dict,
    summary="Delete network ACL",
    description="Delete a network ACL.",
)
async def delete_network_acl(
    acl_name: str,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project holding the ACL (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a network ACL."""
    verify_cluster_access(host)
    try:
        client = require_client(lxd_service.connection_manager.get_client(host), host)
        owner = resolve_owner(client, "network-acls", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network ACL")
        scope = project_params(owner)
        response = client.api["network-acls"][safe_segment(acl_name)].delete(
            params=scope, timeout=write_timeout(ACL_WRITE_WAIT_SECONDS)
        )
        wait_for_lxd_operation(client, response, timeout=ACL_WRITE_WAIT_SECONDS)
        
        if response.status_code in [200, 202]:
            # Log activity for audit trail
            activity_logger.log_activity(
                action="network.acl.delete",
                target_type="network_acl",
                target_id=acl_name,
                target_host=host,
                result="success",
            )
            return {
                "success": True,
                "message": f"Network ACL {acl_name} deleted successfully"
            }
        else:
            activity_logger.log_activity(
                action="network.acl.delete",
                target_type="network_acl",
                target_id=acl_name,
                target_host=host,
                result="failure",
                error_code="LXDError",
                error_message="Failed to delete network ACL",
            )
            raise HTTPException(status_code=500, detail="Failed to delete network ACL")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting network ACL {acl_name}: {e}")
        activity_logger.log_activity(
            action="network.acl.delete",
            target_type="network_acl",
            target_id=acl_name,
            target_host=host,
            result="failure",
            error_code=type(e).__name__,
            error_message=str(e),
        )
        raise HTTPException(status_code=_http_status_for(e), detail=str(e))
