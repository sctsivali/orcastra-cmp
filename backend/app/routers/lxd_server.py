"""
API Router for LXD Server information.
Provides endpoints for getting LXD server details and configuration.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect
from app.models.schemas import FanoutReachability
from app.core.tenant_visibility import tenant_acl_active
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.dependencies import check_read_only_mode, require_partner, require_admin, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.services.lxd.resources import public_config, public_environment
from app.core.type_utils import detect_cluster_type_from_metadata, resolve_cluster_type
from app.core.node_metadata import get_node_metadata_store
from app.services.lxd.lazy_client import client_for
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# important: raised from require_tenant() to require_partner(). Every page that consumes this
# router declares useRequireRole(Role.PARTNER), so the UI already treated it as partner-tier while
# the API accepted a tenant. Mutations here were gated individually and reads were not, which is
# the pattern this closes. Org scope still applies on top, so this narrows the tier rather than
# replacing the boundary.
router = APIRouter(
    prefix="/server",
    tags=["Server"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class ServerInfo(BaseModel):
    """Server information."""
    node: str = Field(..., description="Node name")
    node_type: str = Field("LXD", description="Node type (LXD, Incus, MicroCloud)")
    api_version: str = Field("", description="API version")
    auth: str = Field("", description="Authentication method")
    config: Dict[str, Any] = Field(default_factory=dict, description="Server configuration")
    environment: Dict[str, Any] = Field(default_factory=dict, description="Environment info")
    public: bool = Field(False, description="Whether server is public")


class ServerListResponse(FanoutReachability):
    """Response for listing server info."""
    servers: List[ServerInfo] = Field(..., description="List of server information")
    total: int = Field(..., description="Total number of servers")


def _fetch_server_info_for_node(lxd_service: LXDService, node_name: str) -> NodeRead[ServerInfo]:
    """Server info for a single node - runs in thread pool.

    important: a failure here is reported, not swallowed. The previous version returned None
    on any error, and a node that returned None was in neither `servers` nor
    `unreachable_nodes`, so the page rendered nothing and said nothing.
    """
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. Without this the next line
        # raises AttributeError on None, which the generic handler below
        # turns into a partial result and the node is never named.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    # Get server info from host_info property
    server_data = client.host_info or {}

    # Detect node type from api_extensions in the server response
    # Build metadata dict similar to /1.0 endpoint response
    metadata = {
        "api_extensions": server_data.get('api_extensions', []),
        "environment": server_data.get('environment') or {},
    }
    # important: host_info comes from a trusted connection, so it outranks anything stored.
    # This used to prefer the stored value through metadata_store.get_node(), a method
    # NodeMetadataStore never defined - every call raised AttributeError into the debug
    # log below, so the override silently never ran. The live payload is the one rule.
    stored_type = get_node_metadata_store().get_all_node_metadata().get(node_name, {}).get("type")
    detected_type = resolve_cluster_type(stored_type, client) or detect_cluster_type_from_metadata(metadata)

    # note: LXD renders `config` as null for an identity without can_edit on the server (a
    # restricted certificate). That is still a server that answered. The config and the
    # environment go through the same filters as the single read: the raw block carries the
    # cluster certificate and core.trust_password.
    return NodeRead(rows=[ServerInfo(
        node=node_name,
        node_type=detected_type,
        api_version=server_data.get('api_version') or '',
        auth=server_data.get('auth') or '',
        config=public_config(server_data.get('config') or {}),
        environment=public_environment(server_data.get('environment') or {}),
        public=bool(server_data.get('public', False)),
    )])


@router.get(
    "",
    response_model=ServerListResponse,
    summary="List all server information",
    description="Get server information from all LXD nodes.",
)
async def list_servers(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all server information."""
    try:
        if host:
            verify_cluster_access(host)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch server info from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_server_info_for_node, lxd_service, node_name)
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
            nodes_to_query, results, noun="server information", reasons_visible=not tenant_acl_active()
        )
        unreachable_nodes.extend(fan.unreachable_nodes)
        all_servers = fan.rows
        
        return ServerListResponse(
            servers=all_servers,
            total=len(all_servers),
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            node_errors=fan.node_errors,
        )
    except HTTPException:
        # note: an authorization refusal is a 403, not a server fault. The generic handler
        # below would wrap it into a 500 whose body reads "403: Access denied ...", which
        # misleads clients and pages an on-call for a working permission check.
        raise
    except Exception as e:
        logger.error(f"Error listing server information: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{node_name}",
    response_model=ServerInfo,
    summary="Get server information for a specific node",
    description="Get detailed server information from a specific LXD node.",
)
async def get_server_info(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get server information for a specific node."""
    verify_cluster_access(node_name)
    try:
        client = require_client(await client_for(lxd_service, node_name), node_name)
        server_data = client.host_info
        
        # Detect node type from api_extensions in the server response
        metadata = {
            "api_extensions": server_data.get('api_extensions', []),
            "environment": server_data.get('environment', {})
        }
        # Same rule as _fetch_server_info_for_node: the trusted live payload wins.
        stored_type = get_node_metadata_store().get_all_node_metadata().get(node_name, {}).get("type")
        detected_type = resolve_cluster_type(stored_type, client) or detect_cluster_type_from_metadata(metadata)
        
        return ServerInfo(
            node=node_name,
            node_type=detected_type,
            api_version=server_data.get('api_version', ''),
            auth=server_data.get('auth', ''),
            config=public_config(server_data.get('config', {})),
            environment=public_environment(server_data.get('environment', {})),
            public=server_data.get('public', False)
        )
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error getting server info from {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{node_name}/config",
    response_model=Dict[str, Any],
    summary="Get server configuration",
    description="Get the configuration for a specific LXD server.",
)
async def get_server_config(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get server configuration."""
    verify_cluster_access(node_name)
    try:
        client = require_client(await client_for(lxd_service, node_name), node_name)
        server_data = client.host_info
        # important: the same allowlist as the two ServerInfo builders above. This endpoint
        # returned the raw block, which is where an LXD daemon keeps core.trust_password.
        return public_config(server_data.get('config', {}))
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error getting server config from {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{node_name}/resources",
    response_model=Dict[str, Any],
    summary="Get server resources",
    description="Get resource information (CPU, memory, storage) for a specific LXD server.",
)
async def get_server_resources(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get server resources."""
    verify_cluster_access(node_name)
    try:
        return lxd_service.get_node_resources(node_name)
    except HTTPException:
        # important: let require_client's 503 through. The broad handler below would turn it
        # into a 500, so an unreachable node would read as a server fault. The sibling config
        # route above carries the same pair; this one was missing both halves.
        raise
    except Exception as e:
        logger.error(f"Error getting server resources from {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch(
    "/{node_name}/config",
    response_model=Dict[str, Any],
    summary="Update server configuration",
    description="Update the configuration for a specific LXD server.",
    dependencies=[Depends(check_read_only_mode)],
)
async def update_server_config(
    node_name: str,
    config: Dict[str, Any] = Body(..., description="Configuration to update"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """Update server configuration."""
    verify_cluster_access(node_name)
    try:
        client = require_client(await client_for(lxd_service, node_name), node_name)
        # Update config using PUT method
        # important: client.api is already rooted at <endpoint>/1.0, which is the server
        # config endpoint itself. Indexing '1.0' again addressed /1.0/1.0, so this PUT
        # never reached the config it was meant to write. pylxd's own code calls
        # `self.api.get()` for the same resource.
        response = client.api.put(json={'config': config})
        if response.status_code in [200, 202]:
            return response.json().get('metadata', {})
        else:
            raise HTTPException(status_code=response.status_code, detail="Failed to update server config")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating server config on {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
