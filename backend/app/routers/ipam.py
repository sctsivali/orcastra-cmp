"""
API Router for IPAM (IP Address Management) operations.
Provides endpoints for managing LXD network IP address allocations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ipam", tags=["IPAM"])


class IPAMAllocation(BaseModel):
    """IPAM allocation information."""
    address: str = Field(..., description="IP address")
    type: str = Field(..., description="Address type (instance, network, etc)")
    used_by: str = Field(..., description="Resource using this address")
    hardware_address: str = Field("", description="MAC address")
    network: str = Field(..., description="Network name")
    node: str = Field(..., description="Node name")
    project: str = Field("default", description="Project name")


class IPAMListResponse(BaseModel):
    """Response for listing IPAM allocations."""
    total: int = Field(..., description="Total number of allocations")
    allocations: List[IPAMAllocation] = Field(..., description="List of IP allocations")
    nodes: List[str] = Field(..., description="List of nodes")


@router.get(
    "",
    response_model=IPAMListResponse,
    summary="List IPAM allocations",
    description="Get a list of all IP address allocations across networks.",
)
async def list_ipam_allocations(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    network: Optional[str] = Query(None, description="Filter by specific network"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all IPAM allocations."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_allocations = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                
                # Get all projects using the high-level pylxd interface
                try:
                    projects = client.projects.all()
                    project_names = [p.name for p in projects]
                except Exception as e:
                    logger.error(f"Could not get projects for {node_name}: {e}")
                    project_names = ['default']  # Fallback to default project
                
                # Get network allocations from each project
                for project_name in project_names:
                    try:
                        # Use the network-allocations API endpoint which provides comprehensive IPAM data
                        # This is the same endpoint used by the LXD UI
                        # We need to use the session directly as pylxd doesn't have a high-level wrapper for this
                        base_url = client.api._api_endpoint
                        # Note: base_url already includes `/1.0`, so we just add the endpoint path
                        allocations_url = f"network-allocations?project={project_name}&recursion=1"
                        full_url = f"{base_url}/{allocations_url}"
                        
                        # Use the underlying session from the client
                        allocations_response = client.api.session.get(full_url)
                        
                        if allocations_response.status_code == 200:
                            allocations_data = allocations_response.json().get('metadata', [])
                            
                            for alloc in allocations_data:
                                # Filter by network if specified
                                alloc_network = alloc.get('network', '')
                                if network and alloc_network != network:
                                    continue
                                
                                # Note: the API returns 'addresses' (plural) with CIDR notation
                                addresses_str = alloc.get('addresses', '')
                                # Extract just the IP address without CIDR
                                address = addresses_str.split('/')[0] if addresses_str else ''
                                alloc_type = alloc.get('type', 'network')
                                used_by = alloc.get('used_by', '')
                                hwaddr = alloc.get('hwaddr', '')
                                nat = alloc.get('nat', False)
                                
                                # Determine the type and used_by field
                                if used_by:
                                    # Extract instance name from used_by path
                                    # Format is usually: `/1.0/instances/<name>?project=<project>`
                                    if '/instances/' in used_by:
                                        instance_name = used_by.split('/instances/')[-1].split('?')[0]
                                        resource_type = "instance"
                                        resource_name = instance_name
                                    else:
                                        resource_type = alloc_type
                                        resource_name = alloc_network
                                else:
                                    resource_type = "network"
                                    resource_name = alloc_network
                                
                                if address:
                                    all_allocations.append(IPAMAllocation(
                                        address=address,
                                        type=resource_type,
                                        used_by=resource_name,
                                        hardware_address=hwaddr or "",
                                        network=alloc_network,
                                        node=node_name,
                                        project=project_name
                                    ))
                        else:
                            logger.debug(
                                f"Could not get network allocations for project {project_name} on {node_name}: "
                                f"status {allocations_response.status_code}"
                            )
                    except Exception as e:
                        logger.debug(f"Error getting allocations for project {project_name} on {node_name}: {e}")
                        
            except Exception as e:
                logger.error(f"Error listing IPAM on {node_name}: {e}")
        
        return IPAMListResponse(
            total=len(all_allocations),
            allocations=all_allocations,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing IPAM: {e}")
        raise HTTPException(status_code=500, detail=str(e))
