"""LXD server information and configuration, per cluster."""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect, complete_reasons
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
from app.services.lxd.utils import wait_for_lxd_operation, write_timeout
from app.core.database import get_db
from app.services.shared_cluster_audit import serialize_cluster_configuration
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# important: partner-tier, reads included, to match the pages that use this router
# (useRequireRole(Role.PARTNER)). Org scope still applies on top.
router = APIRouter(
    prefix="/server",
    tags=["Server"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Pool for the per-cluster listing fan-out.
_executor = ThreadPoolExecutor(max_workers=5)
SERVER_CONFIG_WRITE_WAIT_SECONDS = 60


class ServerInfo(BaseModel):
    """Server information for one cluster; config and environment are filtered of secrets."""
    node: str = Field(..., description="Cluster name")
    node_type: str = Field("LXD", description="Cluster type (LXD, Incus, MicroCloud)")
    api_version: str = Field("", description="API version")
    auth: str = Field("", description="Authentication method")
    config: Dict[str, Any] = Field(default_factory=dict, description="Server configuration")
    environment: Dict[str, Any] = Field(default_factory=dict, description="Environment info")
    public: bool = Field(False, description="Whether server is public")


class ServerListResponse(FanoutReachability):
    """Server information across the requested clusters."""
    servers: List[ServerInfo] = Field(..., description="List of server information")
    total: int = Field(..., description="Total number of servers")


def _fetch_server_info_for_node(lxd_service: LXDService, node_name: str) -> NodeRead[ServerInfo]:
    """Server info for one cluster. Runs in the thread pool.

    A failure raises instead of returning nothing: a node in neither `servers` nor
    `unreachable_nodes` would leave the page empty and silent.
    """
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. Raise the connection error so the
        # node is named as unreachable; the next line would otherwise fail with an
        # AttributeError on None and be reported as a failed read.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    server_data = client.host_info or {}

    # The cluster type is detected from the /1.0 payload's api_extensions and environment.
    metadata = {
        "api_extensions": server_data.get('api_extensions', []),
        "environment": server_data.get('environment') or {},
    }
    # important: host_info comes from a trusted connection, so the live payload outranks the
    # stored type.
    stored_type = get_node_metadata_store().get_all_node_metadata().get(node_name, {}).get("type")
    detected_type = resolve_cluster_type(stored_type, client) or detect_cluster_type_from_metadata(metadata)

    # important: config and environment go through the same filters as the single read, since
    # the raw block carries the cluster certificate and core.trust_password. LXD renders `config`
    # as null for an identity without can_edit on the server (a restricted certificate); that is
    # still a server that answered.
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
    description="Get server information from all connected clusters.",
)
async def list_servers(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all server information."""
    try:
        if host:
            verify_cluster_access(host)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # All clusters in parallel.
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_server_info_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # important: two things make a node absent, and both must be named. A node can fail
        # during the fan-out, or never be asked at all: connection_manager.get_node_names()
        # returns only nodes with a live client, while the fleet is whatever Vault has
        # registered. Naming only the first kind leaves a dead cluster silently missing.
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
            node_errors=complete_reasons(unreachable_nodes, fan.node_errors, partial=fan.partial_nodes),
        )
    except HTTPException:
        # An authorization refusal stays a 403. The generic handler below would wrap it into
        # a 500 reading "403: Access denied ...", which misleads clients and pages on-call.
        raise
    except Exception as e:
        logger.error(f"Error listing server information: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{node_name}",
    response_model=ServerInfo,
    summary="Get server information for a specific cluster",
    description="Get detailed server information from a specific cluster.",
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
        # important: let require_client's 503 through. The broad handler below would turn
        # it into a 500, so an offline cluster would read as a server fault.
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
        # important: the same allowlist as the two ServerInfo builders above. The raw block
        # carries core.trust_password.
        return public_config(server_data.get('config', {}))
    except HTTPException:
        # important: let require_client's 503 through. The broad handler below would turn
        # it into a 500, so an offline cluster would read as a server fault.
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
        # into a 500, so an unreachable node would read as a server fault.
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
    db: AsyncSession = Depends(get_db),
):
    """Update server configuration."""
    verify_cluster_access(node_name)
    try:
        async with serialize_cluster_configuration(db, node_name):
            try:
                await assert_cluster_not_migration_fenced(
                    cluster_id=node_name,
                    db=db,
                )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            client = require_client(await client_for(lxd_service, node_name), node_name)
            # important: client.api is already rooted at <endpoint>/1.0, the server config
            # endpoint itself; indexing '1.0' again would address /1.0/1.0. pylxd calls
            # `self.api.get()` for the same resource.
            response = client.api.put(
                json={'config': config},
                timeout=write_timeout(SERVER_CONFIG_WRITE_WAIT_SECONDS),
            )
            wait_for_lxd_operation(
                client,
                response,
                timeout=SERVER_CONFIG_WRITE_WAIT_SECONDS,
            )
        if response.status_code in [200, 202]:
            return response.json().get('metadata', {})
        else:
            raise HTTPException(status_code=response.status_code, detail="Could not update server config")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating server config on {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
