"""
API Router for LXD Server information.
Provides endpoints for getting LXD server details and configuration.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/server", tags=["Server"])

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class ServerInfo(BaseModel):
    """Server information."""
    node: str = Field(..., description="Node name")
    api_version: str = Field("", description="API version")
    auth: str = Field("", description="Authentication method")
    config: Dict[str, Any] = Field(default_factory=dict, description="Server configuration")
    environment: Dict[str, Any] = Field(default_factory=dict, description="Environment info")
    public: bool = Field(False, description="Whether server is public")


class ServerListResponse(BaseModel):
    """Response for listing server info."""
    servers: List[ServerInfo] = Field(..., description="List of server information")
    total: int = Field(..., description="Total number of servers")


def _fetch_server_info_for_node(lxd_service: LXDService, node_name: str) -> Optional[ServerInfo]:
    """Fetch server info for a single node - runs in thread pool."""
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Get server info from host_info property
        server_data = client.host_info
        return ServerInfo(
            node=node_name,
            api_version=server_data.get('api_version', ''),
            auth=server_data.get('auth', ''),
            config=server_data.get('config', {}),
            environment=server_data.get('environment', {}),
            public=server_data.get('public', False)
        )
    except Exception as e:
        logger.error(f"Error getting server info from {node_name}: {e}")
        return None


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
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch server info from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_server_info_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out None and exceptions
        all_servers = []
        for result in results:
            if isinstance(result, ServerInfo):
                all_servers.append(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching server info: {result}")
        
        return ServerListResponse(
            servers=all_servers,
            total=len(all_servers)
        )
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
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        server_data = client.host_info
        
        return ServerInfo(
            node=node_name,
            api_version=server_data.get('api_version', ''),
            auth=server_data.get('auth', ''),
            config=server_data.get('config', {}),
            environment=server_data.get('environment', {}),
            public=server_data.get('public', False)
        )
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
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        server_data = client.host_info
        return server_data.get('config', {})
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
    try:
        return lxd_service.get_node_resources(node_name)
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
):
    """Update server configuration."""
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Update config using PUT method
        response = client.api['1.0'].put(json={'config': config})
        if response.status_code in [200, 202]:
            return response.json().get('metadata', {})
        else:
            raise HTTPException(status_code=response.status_code, detail="Failed to update server config")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating server config on {node_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
