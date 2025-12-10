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
        
        # Build instance -> project mapping by querying all projects
        instance_project_map = {}
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                # Get all projects
                projects_response = client.api.get('/1.0/projects?recursion=0')
                if projects_response.status_code == 200:
                    project_urls = projects_response.json().get('metadata', [])
                    for project_url in project_urls:
                        project_name = project_url.split('/')[-1]
                        try:
                            # Get instances for each project
                            instances_response = client.api.get(f'/1.0/instances?project={project_name}')
                            if instances_response.status_code == 200:
                                instance_urls = instances_response.json().get('metadata', [])
                                for inst_url in instance_urls:
                                    inst_name = inst_url.split('/')[-1].split('?')[0]
                                    instance_project_map[inst_name] = project_name
                        except Exception as e:
                            logger.debug(f"Error getting instances for project {project_name}: {e}")
            except Exception as e:
                logger.debug(f"Error building instance map for {node_name}: {e}")
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                networks = client.networks.all()
                
                for net in networks:
                    # Filter by network if specified
                    if network and net.name != network:
                        continue
                    
                    # Get network state which contains IPAM information
                    try:
                        state = net.state()
                        
                        # Parse IPv4 addresses
                        if hasattr(state, 'addresses') and state.addresses:
                            for addr_info in state.addresses:
                                address = addr_info.get('address', '')
                                family = addr_info.get('family', '')
                                
                                if address and family in ['inet', 'inet6']:
                                    all_allocations.append(IPAMAllocation(
                                        address=address,
                                        type="network",
                                        used_by=net.name,
                                        hardware_address="",
                                        network=net.name,
                                        node=node_name,
                                        project="default"
                                    ))
                        
                        # Parse leases (DHCP allocations)
                        if hasattr(state, 'leases') and state.leases:
                            for lease_type, leases in state.leases.items():
                                for lease in leases:
                                    hostname = lease.get('hostname', '')
                                    address = lease.get('address', '')
                                    hwaddr = lease.get('hwaddr', '')
                                    
                                    if address:
                                        project = instance_project_map.get(hostname, 'default')
                                        all_allocations.append(IPAMAllocation(
                                            address=address,
                                            type="instance",
                                            used_by=hostname or "unknown",
                                            hardware_address=hwaddr,
                                            network=net.name,
                                            node=node_name,
                                            project=project
                                        ))
                    except Exception as e:
                        logger.debug(f"Could not get state for network {net.name} on {node_name}: {e}")
                        
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
