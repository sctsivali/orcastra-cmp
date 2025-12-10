"""
API Router for Profile operations.
Provides endpoints for managing LXD profiles.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any
import logging

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profiles", tags=["Profiles"])


class ProfileInfo(BaseModel):
    """Profile information."""
    name: str = Field(..., description="Profile name")
    description: str = Field("", description="Profile description")
    config: Dict[str, str] = Field(default_factory=dict, description="Profile configuration")
    devices: Dict[str, Dict[str, str]] = Field(default_factory=dict, description="Profile devices")
    used_by: List[str] = Field(default_factory=list, description="Instances using this profile")
    node: str = Field(..., description="Node name where profile is defined")


class ProfileCreateRequest(BaseModel):
    """Request body for creating a profile."""
    name: str = Field(..., description="Profile name")
    description: str = Field("", description="Profile description")
    config: Dict[str, str] = Field(default_factory=dict, description="Profile configuration")
    devices: Dict[str, Dict[str, str]] = Field(default_factory=dict, description="Profile devices")


class ProfileListResponse(BaseModel):
    """Response for listing profiles."""
    total: int = Field(..., description="Total number of profiles")
    profiles: List[ProfileInfo] = Field(..., description="List of profiles")
    nodes: List[str] = Field(..., description="List of nodes")


@router.get(
    "",
    response_model=ProfileListResponse,
    summary="List all profiles",
    description="Get a list of all profiles from LXD nodes.",
)
async def list_profiles(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all profiles."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        all_profiles = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                profiles = client.profiles.all()
                for profile in profiles:
                    all_profiles.append(ProfileInfo(
                        name=profile.name,
                        description=profile.description or "",
                        config=profile.config or {},
                        devices=profile.devices or {},
                        used_by=profile.used_by or [],
                        node=node_name
                    ))
            except Exception as e:
                logger.error(f"Error listing profiles on {node_name}: {e}")
        
        return ProfileListResponse(
            total=len(all_profiles),
            profiles=all_profiles,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing profiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{profile_name}",
    response_model=ProfileInfo,
    summary="Get profile details",
    description="Get detailed information about a specific profile.",
)
async def get_profile(
    profile_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific profile."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        profile = client.profiles.get(profile_name)
        
        return ProfileInfo(
            name=profile.name,
            description=profile.description or "",
            config=profile.config or {},
            devices=profile.devices or {},
            used_by=profile.used_by or []
        )
    except Exception as e:
        logger.error(f"Error getting profile {profile_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Profile {profile_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create profile",
    description="Create a new profile.",
)
async def create_profile(
    request: ProfileCreateRequest,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Create a new profile."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        profile = client.profiles.create(
            name=request.name,
            config=request.config,
            devices=request.devices,
            description=request.description
        )
        
        return {
            "success": True,
            "message": f"Profile {request.name} created successfully",
            "profile": ProfileInfo(
                name=profile.name,
                description=profile.description or "",
                config=profile.config or {},
                devices=profile.devices or {},
                used_by=profile.used_by or []
            )
        }
    except Exception as e:
        logger.error(f"Error creating profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/{profile_name}",
    response_model=dict,
    summary="Update profile",
    description="Update an existing profile.",
)
async def update_profile(
    profile_name: str,
    host: str = Query(..., description="Target host node"),
    config: Optional[Dict[str, str]] = Body(None),
    devices: Optional[Dict[str, Dict[str, str]]] = Body(None),
    description: Optional[str] = Body(None),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Update a profile."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        profile = client.profiles.get(profile_name)
        
        if config is not None:
            profile.config = config
        if devices is not None:
            profile.devices = devices
        if description is not None:
            profile.description = description
        
        profile.save()
        
        return {
            "success": True,
            "message": f"Profile {profile_name} updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating profile {profile_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{profile_name}",
    response_model=dict,
    summary="Delete profile",
    description="Delete a profile.",
)
async def delete_profile(
    profile_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a profile."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        profile = client.profiles.get(profile_name)
        profile.delete()
        
        return {
            "success": True,
            "message": f"Profile {profile_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting profile {profile_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
