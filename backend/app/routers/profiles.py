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
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    project_is_visible,
)
from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    effective_project,
    list_across_projects,
    list_in_project,
    list_project_configs,
    project_params,
    resolve_owner,
)
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    tenant_acl_active,
    tenant_may_see_project,
)
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field
from app.services.activity_logger import get_activity_logger
from app.core.lxd_names import safe_segment

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
    project: str = Field(
        DEFAULT_PROJECT,
        description="LXD project that holds this profile",
    )


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
    partial_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that answered but could not be read for every project, so their entries "
            "may be incomplete. A short list is never presented as the whole truth."
        ),
    )


def _fetch_profiles_for_node(
    lxd_service: LXDService, node_name: str, project: Optional[str] = None
) -> tuple[List[ProfileInfo], bool]:
    """Fetch profiles for a single node - runs in thread pool.

    Returns (profiles, complete). With no project asked for, this reads every project on the
    node; naming one reads the project that actually holds its profiles, which is the project
    itself only when it sets features.profiles.
    """
    profiles_list: List[ProfileInfo] = []
    complete = True
    try:
        client = lxd_service.get_client_with_reconnect(node_name)
        if client is None:
            # important: unreachable is not empty. Returning a bare list here
            # made a dead node indistinguishable from one with nothing.
            raise LXDConnectionError(f"Could not connect to {node_name}")
        if project:
            owner = effective_project("profiles", project, list_project_configs(client))
            rows = list_in_project(client, "profiles", owner)
        else:
            rows, complete = list_across_projects(client, "profiles")
        for row in rows:
            profiles_list.append(ProfileInfo(
                name=row.get("name") or "",
                description=row.get("description") or "",
                config=row.get("config") or {},
                devices=row.get("devices") or {},
                used_by=row.get("used_by") or [],
                node=node_name,
                project=row.get("project") or DEFAULT_PROJECT,
            ))
    except LXDConnectionError:
        # a missing client IS unreachability: let it through
        raise
    except Exception as e:
        # important: not unreachability. See networks.py: an item
        # vanishing mid-listing must not condemn the whole node.
        logger.warning(f"Error listing profiles on {node_name}: {e}")
        complete = False
    return profiles_list, complete


@router.get(
    "",
    response_model=ProfileListResponse,
    summary="List all profiles",
    description="Get a list of all profiles from LXD nodes.",
)
@single_flight("profiles:list")
async def list_profiles(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project to read. Omit for every project, each row naming the one that "
            "holds it."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all profiles."""
    if host:
        verify_cluster_access(host)
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    # important: the project belongs in the key. get_scope_cache_key mixes in the cluster
    # scope and the ACL digest but not the query, so without it a project-scoped read and a
    # fleet-wide one collide on one entry for the whole TTL.
    cache_key = get_scope_cache_key(f"profiles:list:host={host}:project={project or '*'}")
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
            loop.run_in_executor(
                _executor, _fetch_profiles_for_node, lxd_service, node_name, project
            )
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
        partial_nodes: List[str] = []
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, tuple):
                rows, complete = result
                answered += 1
                all_profiles.extend(rows)
                if not complete:
                    partial_nodes.append(node_name)
            elif isinstance(result, Exception):
                logger.warning(f"Error fetching profiles: {result}")
                unreachable_nodes.append(node_name)

        # important: a Partner is scoped by cluster, not by project, and the used_by filter
        # below never runs for them. On a cluster shared with another organization this is the
        # only thing standing between them and that organization's profile devices.
        all_profiles = [
            p for p in all_profiles if project_is_visible(p.node, p.project)
        ]

        if tenant_acl_active():
            visible_profiles = []
            for profile in all_profiles:
                # ownership as well as usage: see tenant_may_see_project
                if not tenant_may_see_project(profile.node, profile.project):
                    continue
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
            partial_nodes=sorted(partial_nodes),
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
    project: Optional[str] = Query(
        None, description="LXD project holding the profile (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific profile."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        response = client.api["profiles"][safe_segment(profile_name)].get(params=project_params(owner))
        data = response.json().get("metadata") or {}
        used_by = filter_visible_lxd_references(host, data.get("used_by") or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this profile")

        return ProfileInfo(
            name=data.get("name") or profile_name,
            description=data.get("description") or "",
            config=data.get("config") or {},
            devices=data.get("devices") or {},
            used_by=used_by,
            node=host,
            project=data.get("project") or owner,
        )
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
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
    project: Optional[str] = Query(
        None, description="LXD project holding the profile (default project when omitted)"
    ),
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
        # important: the owning project is resolved, not taken as given. A project that does
        # not set features.profiles has no profiles of its own, and LXD stores the profile in
        # default. Writing it as asked and reading it back as resolved would disagree.
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this project")
        client.api["profiles"].post(
            json={
                "name": request.name,
                "config": request.config,
                "devices": request.devices,
                "description": request.description,
            },
            params=project_params(owner),
        )
        created = client.api["profiles"][safe_segment(request.name)].get(
            params=project_params(owner)
        ).json().get("metadata") or {}
        
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
                name=created.get("name") or request.name,
                description=created.get("description") or "",
                config=created.get("config") or {},
                devices=created.get("devices") or {},
                used_by=created.get("used_by") or [],
                node=host,
                project=created.get("project") or owner,
            )
        }
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
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
    project: Optional[str] = Query(
        None, description="LXD project holding the profile (default project when omitted)"
    ),
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
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        current = client.api["profiles"][safe_segment(profile_name)].get(
            params=project_params(owner)
        ).json().get("metadata") or {}

        payload = {
            "config": current.get("config") or {},
            "devices": current.get("devices") or {},
            "description": current.get("description") or "",
        }
        if config is not None:
            payload["config"] = config
        if devices is not None:
            payload["devices"] = devices
        if description is not None:
            payload["description"] = description

        client.api["profiles"][safe_segment(profile_name)].put(
            json=payload, params=project_params(owner)
        )
        
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
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
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
    project: Optional[str] = Query(
        None, description="LXD project holding the profile (default project when omitted)"
    ),
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
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        client.api["profiles"][safe_segment(profile_name)].delete(params=project_params(owner))
        
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
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
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
