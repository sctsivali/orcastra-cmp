"""
API Router for Profile operations.
Provides endpoints for managing LXD profiles.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDConnectionError,
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.tenant_visibility import filter_visible_lxd_references, tenant_acl_active
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/profiles",
    tags=["Profiles"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


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
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their entries are missing from this list, "
            "so an unreachable node is distinguishable from one that has none."
        ),
    )


def _fetch_profiles_for_node(lxd_service: LXDService, node_name: str) -> List[ProfileInfo]:
    """Fetch profiles for a single node - runs in thread pool."""
    profiles_list = []
    try:
        client = lxd_service.get_client_with_reconnect(node_name)
        if client is None:
            # important: unreachable is not empty. Returning a bare list here
            # made a dead node indistinguishable from one with nothing.
            raise LXDConnectionError(f"Could not connect to {node_name}")
        profiles = client.profiles.all()
        for profile in profiles:
            profiles_list.append(ProfileInfo(
                name=profile.name,
                description=profile.description or "",
                config=profile.config or {},
                devices=profile.devices or {},
                used_by=profile.used_by or [],
                node=node_name
            ))
    except LXDConnectionError:
        # a missing client IS unreachability: let it through
        raise
    except Exception as e:
        # important: not unreachability. See networks.py: an item
        # vanishing mid-listing must not condemn the whole node.
        logger.warning(f"Error listing profiles on {node_name}: {e}")
    return profiles_list


@router.get(
    "",
    response_model=ProfileListResponse,
    summary="List all profiles",
    description="Get a list of all profiles from LXD nodes.",
)
async def list_profiles(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all profiles."""
    if host:
        verify_cluster_access(host)
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"profiles:list:host={host}")
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached profiles for host={host}")
        # Reconstruct Pydantic model from cached dict
        return ProfileListResponse(**cached_data)
    
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch profiles from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_profiles_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_profiles = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        answered = 0
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                answered += 1
                all_profiles.extend(result)
            elif isinstance(result, Exception):
                logger.warning(f"Error fetching profiles: {result}")
                unreachable_nodes.append(node_name)

        if tenant_acl_active():
            visible_profiles = []
            for profile in all_profiles:
                profile.used_by = filter_visible_lxd_references(profile.node, profile.used_by)
                if profile.used_by:
                    visible_profiles.append(profile)
            all_profiles = visible_profiles
        
        # Store in variable first
        response = ProfileListResponse(
            total=len(all_profiles),
            profiles=all_profiles,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
        )
        
        # important: an outage must not be remembered as the answer. See instances.py.
        if answered:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
            logger.debug("Cached profiles list for 180 seconds")
        else:
            logger.warning("Not caching profiles list: no node answered")

        return response
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
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        profile = client.profiles.get(profile_name)
        used_by = filter_visible_lxd_references(host, profile.used_by or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        
        return ProfileInfo(
            name=profile.name,
            description=profile.description or "",
            config=profile.config or {},
            devices=profile.devices or {},
            used_by=used_by,
            node=host,
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new profile."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        profile = client.profiles.create(
            name=request.name,
            config=request.config,
            devices=request.devices,
            description=request.description
        )
        
        # Log activity: profile creation success
        activity_logger.log_activity(
            action="profile.create",
            target_type="profile",
            target_id=request.name,
            target_host=host,
            result="success",
            details={"description": request.description}
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("profiles")
        return {
            "success": True,
            "message": f"Profile {request.name} created successfully",
            "profile": ProfileInfo(
                name=profile.name,
                description=profile.description or "",
                config=profile.config or {},
                devices=profile.devices or {},
                used_by=profile.used_by or [],
                node=host
            )
        }
    except Exception as e:
        logger.error(f"Error creating profile: {e}")
        # Log activity: profile creation failure
        activity_logger.log_activity(
            action="profile.create",
            target_type="profile",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a profile."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        profile = client.profiles.get(profile_name)
        
        if config is not None:
            profile.config = config
        if devices is not None:
            profile.devices = devices
        if description is not None:
            profile.description = description
        
        profile.save()
        
        # Log activity: profile update success
        activity_logger.log_activity(
            action="profile.update",
            target_type="profile",
            target_id=profile_name,
            target_host=host,
            result="success"
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("profiles")
        return {
            "success": True,
            "message": f"Profile {profile_name} updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating profile {profile_name}: {e}")
        # Log activity: profile update failure
        activity_logger.log_activity(
            action="profile.update",
            target_type="profile",
            target_id=profile_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a profile."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        profile = client.profiles.get(profile_name)
        profile.delete()
        
        # Log activity: profile deletion success
        activity_logger.log_activity(
            action="profile.delete",
            target_type="profile",
            target_id=profile_name,
            target_host=host,
            result="success"
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("profiles")
        return {
            "success": True,
            "message": f"Profile {profile_name} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting profile {profile_name}: {e}")
        # Log activity: profile deletion failure
        activity_logger.log_activity(
            action="profile.delete",
            target_type="profile",
            target_id=profile_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))
