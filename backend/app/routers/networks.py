"""
API Router for Network operations.
Provides endpoints for managing LXD networks.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/networks", tags=["Networks"])


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
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_networks = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                networks = client.networks.all()
                for network in networks:
                    all_networks.append(NetworkInfo(
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
        
        return NetworkListResponse(
            total=len(all_networks),
            networks=all_networks,
            nodes=lxd_service.connection_manager.get_node_names()
        )
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
    try:
        client = lxd_service.connection_manager.get_client(host)
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
):
    """Create a new network."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        network = client.networks.create(
            name=request.name,
            type=request.type,
            config=request.config,
            description=request.description
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
                        node=node_name
            )
        }
    except Exception as e:
        logger.error(f"Error creating network: {e}")
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
):
    """Update a network."""
    try:
        client = lxd_service.connection_manager.get_client(host)
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
):
    """Delete a network."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        network = client.networks.get(network_name)
        network.delete()
        
        return {
            "success": True,
            "message": f"Network {network_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting network {network_name}: {e}")
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
    try:
        client = lxd_service.connection_manager.get_client(host)
        network = client.networks.get(network_name)
        state = network.state()
        
        return {
            "name": network_name,
            "state": state
        }
    except Exception as e:
        logger.error(f"Error getting network state {network_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
