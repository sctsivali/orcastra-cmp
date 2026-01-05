"""
API Router for Network ACL operations.
Provides endpoints for managing LXD network ACLs.
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

router = APIRouter(prefix="/network-acls", tags=["Network ACLs"])

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class NetworkACLInfo(BaseModel):
    """Network ACL information."""
    name: str = Field(..., description="ACL name")
    description: str = Field("", description="ACL description")
    ingress: List[Dict[str, Any]] = Field(default_factory=list, description="Ingress rules")
    egress: List[Dict[str, Any]] = Field(default_factory=list, description="Egress rules")
    config: Dict[str, str] = Field(default_factory=dict, description="ACL configuration")
    used_by: List[str] = Field(default_factory=list, description="Resources using this ACL")
    node: str = Field("", description="Node name")


class NetworkACLCreateRequest(BaseModel):
    """Request body for creating a network ACL."""
    name: str = Field(..., description="ACL name")
    description: str = Field("", description="ACL description")
    ingress: List[Dict[str, Any]] = Field(default_factory=list, description="Ingress rules")
    egress: List[Dict[str, Any]] = Field(default_factory=list, description="Egress rules")
    config: Dict[str, str] = Field(default_factory=dict, description="ACL configuration")


class NetworkACLListResponse(BaseModel):
    """Response for listing network ACLs."""
    total: int = Field(..., description="Total number of ACLs")
    acls: List[NetworkACLInfo] = Field(..., description="List of network ACLs")
    nodes: List[str] = Field(..., description="List of nodes")


def _fetch_acls_for_node(lxd_service: LXDService, node_name: str) -> List[NetworkACLInfo]:
    """Fetch network ACLs for a single node - runs in thread pool."""
    acls = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Get ACLs via API with recursion=1 to get full details in one call
        response = client.api.get('/1.0/network-acls?recursion=1')
        if response.status_code == 200:
            acls_data = response.json().get('metadata', [])
            for acl_data in acls_data:
                try:
                    acls.append(NetworkACLInfo(
                        name=acl_data.get('name', ''),
                        description=acl_data.get('description', ''),
                        ingress=acl_data.get('ingress', []),
                        egress=acl_data.get('egress', []),
                        config=acl_data.get('config', {}),
                        used_by=acl_data.get('used_by', []),
                        node=node_name
                    ))
                except Exception as e:
                    logger.error(f"Error parsing ACL data on {node_name}: {e}")
    except Exception as e:
        logger.error(f"Error listing network ACLs on {node_name}: {e}")
    return acls


@router.get(
    "",
    response_model=NetworkACLListResponse,
    summary="List all network ACLs",
    description="Get a list of all network ACLs from LXD nodes.",
)
async def list_network_acls(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all network ACLs."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch ACLs from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_acls_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_acls = []
        for result in results:
            if isinstance(result, list):
                all_acls.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching network ACLs: {result}")
        
        return NetworkACLListResponse(
            total=len(all_acls),
            acls=all_acls,
            nodes=lxd_service.connection_manager.get_node_names()
        )
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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific network ACL."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.get(f'/1.0/network-acls/{acl_name}')
        
        if response.status_code == 200:
            acl_data = response.json().get('metadata', {})
            return NetworkACLInfo(
                name=acl_data.get('name', ''),
                description=acl_data.get('description', ''),
                ingress=acl_data.get('ingress', []),
                egress=acl_data.get('egress', []),
                config=acl_data.get('config', {}),
                used_by=acl_data.get('used_by', [])
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
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Create a new network ACL."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        acl_data = {
            "name": request.name,
            "description": request.description,
            "ingress": request.ingress,
            "egress": request.egress,
            "config": request.config
        }
        
        response = client.api.post('/1.0/network-acls', json=acl_data)
        
        if response.status_code in [200, 201, 202]:
            return {
                "success": True,
                "message": f"Network ACL {request.name} created successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to create network ACL")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating network ACL: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Update a network ACL."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # Get current ACL
        get_response = client.api.get(f'/1.0/network-acls/{acl_name}')
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
        put_response = client.api.put(f'/1.0/network-acls/{acl_name}', json=acl_data)
        
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
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{acl_name}",
    response_model=dict,
    summary="Delete network ACL",
    description="Delete a network ACL.",
)
async def delete_network_acl(
    acl_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a network ACL."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.delete(f'/1.0/network-acls/{acl_name}')
        
        if response.status_code in [200, 202]:
            return {
                "success": True,
                "message": f"Network ACL {acl_name} deleted successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to delete network ACL")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting network ACL {acl_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
