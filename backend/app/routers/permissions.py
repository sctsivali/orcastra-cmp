"""
API Router for Identity and Group management (Permissions).
Provides endpoints for managing LXD identities and groups.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Identities Router
identities_router = APIRouter(prefix="/identities", tags=["Identities"])

# Groups Router
groups_router = APIRouter(prefix="/groups", tags=["Groups"])


class IdentityInfo(BaseModel):
    """Identity information."""
    id: str = Field(..., description="Identity ID")
    name: str = Field("", description="Identity name")
    type: str = Field("", description="Identity type")
    groups: List[str] = Field(default_factory=list, description="Groups this identity belongs to")


class GroupInfo(BaseModel):
    """Group information."""
    name: str = Field(..., description="Group name")
    description: str = Field("", description="Group description")
    identities: List[str] = Field(default_factory=list, description="Identities in this group")
    permissions: List[Dict[str, Any]] = Field(default_factory=list, description="Group permissions")


class IdentityListResponse(BaseModel):
    """Response for listing identities."""
    total: int = Field(..., description="Total number of identities")
    identities: List[IdentityInfo] = Field(..., description="List of identities")
    nodes: List[str] = Field(..., description="List of nodes")


class GroupListResponse(BaseModel):
    """Response for listing groups."""
    total: int = Field(..., description="Total number of groups")
    groups: List[GroupInfo] = Field(..., description="List of groups")
    nodes: List[str] = Field(..., description="List of nodes")


# Identity Endpoints
@identities_router.get(
    "",
    response_model=IdentityListResponse,
    summary="List all identities",
    description="Get a list of all identities from LXD nodes.",
)
async def list_identities(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all identities."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_identities = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                # Use recursion=1 to get full details in one call
                response = client.api.get('/1.0/auth/identities?recursion=1')
                if response.status_code == 200:
                    identities_data = response.json().get('metadata', [])
                    for identity_data in identities_data:
                        try:
                            all_identities.append(IdentityInfo(
                                id=identity_data.get('id', ''),
                                name=identity_data.get('name', ''),
                                type=identity_data.get('type', ''),
                                groups=identity_data.get('groups', [])
                            ))
                        except Exception as e:
                            logger.error(f"Error parsing identity data: {e}")
                else:
                    logger.warning(f"Got status {response.status_code} for identities on {node_name}")
            except Exception as e:
                logger.error(f"Error listing identities on {node_name}: {e}")
        
        return IdentityListResponse(
            total=len(all_identities),
            identities=all_identities,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing identities: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@identities_router.get(
    "/{identity_id}",
    response_model=IdentityInfo,
    summary="Get identity details",
    description="Get detailed information about a specific identity.",
)
async def get_identity(
    identity_id: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific identity."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.get(f'/1.0/auth/identities/{identity_id}')
        
        if response.status_code == 200:
            identity_data = response.json().get('metadata', {})
            return IdentityInfo(
                id=identity_data.get('id', ''),
                name=identity_data.get('name', ''),
                type=identity_data.get('type', ''),
                groups=identity_data.get('groups', [])
            )
        else:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting identity {identity_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")


# Group Endpoints
@groups_router.get(
    "",
    response_model=GroupListResponse,
    summary="List all groups",
    description="Get a list of all groups from LXD nodes.",
)
async def list_groups(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all groups."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_groups = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                # Use recursion=1 to get full details in one call
                response = client.api.get('/1.0/auth/groups?recursion=1')
                if response.status_code == 200:
                    groups_data = response.json().get('metadata', [])
                    for group_data in groups_data:
                        try:
                            all_groups.append(GroupInfo(
                                name=group_data.get('name', ''),
                                description=group_data.get('description', ''),
                                identities=group_data.get('identities', []),
                                permissions=group_data.get('permissions', [])
                            ))
                        except Exception as e:
                            logger.error(f"Error parsing group data: {e}")
                else:
                    logger.warning(f"Got status {response.status_code} for groups on {node_name}")
            except Exception as e:
                logger.error(f"Error listing groups on {node_name}: {e}")
        
        return GroupListResponse(
            total=len(all_groups),
            groups=all_groups,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing groups: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@groups_router.get(
    "/{group_name}",
    response_model=GroupInfo,
    summary="Get group details",
    description="Get detailed information about a specific group.",
)
async def get_group(
    group_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific group."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.get(f'/1.0/auth/groups/{group_name}')
        
        if response.status_code == 200:
            group_data = response.json().get('metadata', {})
            return GroupInfo(
                name=group_data.get('name', ''),
                description=group_data.get('description', ''),
                identities=group_data.get('identities', []),
                permissions=group_data.get('permissions', [])
            )
        else:
            raise HTTPException(status_code=404, detail=f"Group {group_name} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting group {group_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Group {group_name} not found")


@groups_router.post(
    "",
    response_model=dict,
    summary="Create group",
    description="Create a new group.",
)
async def create_group(
    name: str = Body(..., description="Group name"),
    description: str = Body("", description="Group description"),
    permissions: List[Dict[str, Any]] = Body(default_factory=list, description="Group permissions"),
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Create a new group."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        group_data = {
            "name": name,
            "description": description,
            "permissions": permissions
        }
        
        response = client.api.post('/1.0/auth/groups', json=group_data)
        
        if response.status_code in [200, 201, 202]:
            return {
                "success": True,
                "message": f"Group {name} created successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to create group")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating group: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@groups_router.delete(
    "/{group_name}",
    response_model=dict,
    summary="Delete group",
    description="Delete a group.",
)
async def delete_group(
    group_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a group."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.delete(f'/1.0/auth/groups/{group_name}')
        
        if response.status_code in [200, 202]:
            return {
                "success": True,
                "message": f"Group {group_name} deleted successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to delete group")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting group {group_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
