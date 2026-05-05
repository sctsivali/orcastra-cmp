"""
API Router for Network operations.
Provides endpoints for managing LXD networks.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/networks",
    tags=["Networks"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class NetworkInfo(BaseModel):
    """Network information."""
    name: str = Field(..., description="Network name")
    description: str = Field("", description="Network description")
    type: str = Field(..., description="Network type (bridge, physical, etc)")
    managed: bool = Field(True, description="Whether network is managed by LXD")
    config: Dict[str, str] = Field(default_factory=dict, description="Network configuration")
    used_by: List[str] = Field(default_factory=list, description="Instances using this network")
    status: str = Field("", description="Network status")
    node: str = Field("", description="Node name where network is defined")


class NetworkCreateRequest(BaseModel):
    """Request body for creating a network."""
    name: str = Field(..., description="Network name")
    description: str = Field("", description="Network description")
    type: str = Field("bridge", description="Network type")
    config: Dict[str, str] = Field(default_factory=dict, description="Network configuration")


class NetworkListResponse(BaseModel):
    """Response for listing networks."""
    total: int = Field(..., description="Total number of networks")
    networks: List[NetworkInfo] = Field(..., description="List of networks")
    nodes: List[str] = Field(..., description="List of nodes")


def _fetch_networks_for_node(lxd_service: LXDService, node_name: str) -> List[NetworkInfo]:
    """Fetch networks for a single node - runs in thread pool."""
    networks_list = []
    try:
        client = lxd_service.get_client_with_reconnect(node_name)
        if client is None:
            logger.warning(f"Could not connect to {node_name}, skipping for networks list")
            return networks_list
        networks = client.networks.all()
        for network in networks:
            networks_list.append(NetworkInfo(
                name=network.name,
                description=getattr(network, 'description', '') or "",
                type=network.type,
                managed=network.managed,
                config=network.config or {},
                used_by=network.used_by or [],
                status=getattr(network, 'status', ''),
                node=node_name
            ))
    except Exception as e:
        logger.error(f"Error listing networks on {node_name}: {e}")
    return networks_list


@router.get(
    "",
    response_model=NetworkListResponse,
    summary="List all networks",
    description="Get a list of all networks from LXD nodes.",
)
async def list_networks(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all networks."""
    if host:
        verify_cluster_access(host)
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"networks:list:host={host}")
    cached_data = cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached networks for host={host}")
        # Reconstruct Pydantic model from cached dict
        return NetworkListResponse(**cached_data)
    
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch networks from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_networks_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_networks = []
        for result in results:
            if isinstance(result, list):
                all_networks.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching networks: {result}")
        
        # Store in variable first
        response = NetworkListResponse(
            total=len(all_networks),
            networks=all_networks,
            nodes=lxd_service.connection_manager.get_node_names()
        )
        
        # Cache as dict for 120 seconds
        cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
        logger.debug(f"Cached networks list for 120 seconds")
        
        return response
    except Exception as e:
        logger.error(f"Error listing networks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{network_name}",
    response_model=NetworkInfo,
    summary="Get network details",
    description="Get detailed information about a specific network.",
)
async def get_network(
    network_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.get(network_name)
        
        return NetworkInfo(
            name=network.name,
            description=getattr(network, 'description', '') or "",
            type=network.type,
            managed=network.managed,
            config=network.config or {},
            used_by=network.used_by or [],
            status=getattr(network, 'status', ''),
                        node=node_name
        )
    except Exception as e:
        logger.error(f"Error getting network {network_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Network {network_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create network",
    description="Create a new network.",
)
async def create_network(
    request: NetworkCreateRequest,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.create(
            name=request.name,
            type=request.type,
            config=request.config,
            description=request.description
        )
        
        # Log activity: network creation success
        activity_logger.log_activity(
            action="network.create",
            target_type="network",
            target_id=request.name,
            target_host=host,
            result="success",
            details={"type": request.type, "description": request.description}
        )
        
        return {
            "success": True,
            "message": f"Network {request.name} created successfully",
            "network": NetworkInfo(
                name=network.name,
                description=getattr(network, 'description', '') or "",
                type=network.type,
                managed=network.managed,
                config=network.config or {},
                used_by=network.used_by or [],
                status=getattr(network, 'status', ''),
                node=host
            )
        }
    except Exception as e:
        logger.error(f"Error creating network: {e}")
        # Log activity: network creation failure
        activity_logger.log_activity(
            action="network.create",
            target_type="network",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/{network_name}",
    response_model=dict,
    summary="Update network",
    description="Update an existing network.",
)
async def update_network(
    network_name: str,
    host: str = Query(..., description="Target host node"),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.get(network_name)
        
        if config is not None:
            network.config = config
        if description is not None and hasattr(network, 'description'):
            network.description = description
        
        network.save()
        
        return {
            "success": True,
            "message": f"Network {network_name} updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating network {network_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{network_name}",
    response_model=dict,
    summary="Delete network",
    description="Delete a network.",
)
async def delete_network(
    network_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.get(network_name)
        network.delete()
        
        # Log activity: network deletion success
        activity_logger.log_activity(
            action="network.delete",
            target_type="network",
            target_id=network_name,
            target_host=host,
            result="success"
        )
        
        return {
            "success": True,
            "message": f"Network {network_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting network {network_name}: {e}")
        # Log activity: network deletion failure
        activity_logger.log_activity(
            action="network.delete",
            target_type="network",
            target_id=network_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{network_name}/state",
    response_model=dict,
    summary="Get network state",
    description="Get the current state of a network.",
)
async def get_network_state(
    network_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get network state."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.get(network_name)
        state = network.state()
        
        return {
            "name": network_name,
            "state": state
        }
    except Exception as e:
        logger.error(f"Error getting network state {network_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
