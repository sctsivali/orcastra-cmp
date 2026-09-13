"""
API Router for Profile operations.
Provides endpoints for managing LXD profiles.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from typing import Any, Optional, List, Dict
import logging
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from pylxd.exceptions import LXDAPIException
from app.services.lxd.profile_writes import (
    PreconditionFailed,
    ProfileInUse,
    create_profile_sync,
    delete_profile_sync,
    read_profile_sync,
    update_profile_sync,
)

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
    resolve_owner,
)
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    tenant_acl_active,
    tenant_may_see_project,
    parse_lxd_reference,
)
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field, field_validator
from app.services.activity_logger import get_activity_logger
from app.core.lxd_names import LXD_NAME_RE, MAX_NAME_LENGTH

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/profiles",
    tags=["Profiles"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


# LXD refuses almost nothing in a profile name (empty, a slash, "." and ".."); the house rule
# is applied on create so a name the console would not accept is a 422 at the boundary. The
# path routes stay permissive so a profile `lxc` created with another shape is still reachable.
_PATH_NAME_RE = re.compile(r"^[^\s/]+$")

STALE_PROFILE_DETAIL = (
    "This profile changed since you opened it. Reload and apply your change again."
)
DEFAULT_PROFILE_DETAIL = "The default profile cannot be deleted. LXD keeps one in every project."


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


class ProfileDetail(ProfileInfo):
    """A single profile as read from the daemon, with the version it was read at."""
    etag: Optional[str] = Field(
        None,
        description=(
            "Opaque version of config, description and devices as LXD holds them. Send it "
            "back as the If-Match header on PUT and a write over someone else's change is "
            "refused with 412."
        ),
    )


class ProfileCreateRequest(BaseModel):
    """Request body for creating a profile."""
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, description="Profile name")
    description: str = Field("", description="Profile description")
    config: Dict[str, str] = Field(default_factory=dict, description="Profile configuration")
    devices: Dict[str, Dict[str, str]] = Field(default_factory=dict, description="Profile devices")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not LXD_NAME_RE.fullmatch(v):
            raise ValueError(
                "Name must start with a letter and contain only letters, numbers, dots, "
                "underscores and hyphens"
            )
        return v


class ProfileUpdateRequest(BaseModel):
    """Request body for updating a profile. A map that is sent replaces LXD's whole map."""
    description: Optional[str] = Field(None, description="Profile description")
    config: Optional[Dict[str, str]] = Field(None, description="Profile configuration")
    devices: Optional[Dict[str, Dict[str, str]]] = Field(None, description="Profile devices")


def _check_path_name(profile_name: str) -> None:
    if profile_name in (".", "..") or not _PATH_NAME_RE.fullmatch(profile_name):
        raise HTTPException(status_code=400, detail="Not a profile name")


def _bounded_used_by(host: str, used_by: List[str]) -> List[str]:
    """The references inside the Partner project bound; the tenant ACL is applied by the caller.

    On a cluster shared by two organizations the other one's projects inherit `default`
    profiles too, so an unbounded used_by names their instances; the bound exists for that.
    """
    return [ref for ref in (used_by or []) if project_is_visible(host, parse_lxd_reference(ref)[0])]


def _detail_of(profile_name: str, data: Dict[str, Any], host: str,
               owner: str, etag: Optional[str]) -> "ProfileDetail":
    """The daemon's record as the caller may see it: used_by is filtered to their scope."""
    return ProfileDetail(
        name=data.get("name") or profile_name,
        description=data.get("description") or "",
        config=data.get("config") or {},
        devices=data.get("devices") or {},
        used_by=filter_visible_lxd_references(host, _bounded_used_by(host, data.get("used_by") or [])),
        node=host,
        project=data.get("project") or owner,
        etag=etag,
    )


def _lxd_status(exc: Exception) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _refusal(exc: Exception, profile_name: str) -> HTTPException:
    """The daemon's own refusal as the operator should read it."""
    status = _lxd_status(exc)
    text = str(exc)
    if status == 412:
        return HTTPException(status_code=412, detail=STALE_PROFILE_DETAIL)
    if status == 403 and "default" in text and "cannot be deleted" in text:
        return HTTPException(status_code=403, detail=DEFAULT_PROFILE_DETAIL)
    if status == 400 and "currently in use" in text:
        return HTTPException(
            status_code=409,
            detail=f"Profile {profile_name} is still in use. Detach it from every instance before deleting it.",
        )
    if status in (400, 404, 409):
        return HTTPException(status_code=status, detail=text)
    # a 200 with an err body is a failed operation (utils.wait_for_lxd_operation raises it)
    return HTTPException(status_code=400, detail=f"The cluster refused the change: {text}")


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
        # the rows' used_by is held to the same bound as the single read: on a shared cluster
        # the other organization's projects inherit `default` profiles too, and the row feeds
        # the delete confirm and the "in use by N" reason on the page
        for p in all_profiles:
            p.used_by = filter_visible_lxd_references(p.node, _bounded_used_by(p.node, p.used_by))

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
    response_model=ProfileDetail,
    summary="Get profile details",
    description=(
        "Get detailed information about a specific profile, with the ETag to send back as "
        "If-Match on an update."
    ),
)
async def get_profile(
    profile_name: str,
    http_response: Response,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project holding the profile (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific profile."""
    verify_cluster_access(host)
    _check_path_name(profile_name)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        data, etag = await asyncio.to_thread(read_profile_sync, client, profile_name, owner)
        used_by = filter_visible_lxd_references(host, _bounded_used_by(host, data.get("used_by") or []))
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        if etag:
            http_response.headers["ETag"] = etag
        return _detail_of(profile_name, data, host, owner, etag)
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
        created, etag = await asyncio.to_thread(
            create_profile_sync, client, owner,
            {
                "name": request.name,
                "config": request.config,
                "devices": request.devices,
                "description": request.description,
            },
        )
        
        # Log activity: profile creation success
        activity_logger.log_activity(
            action="profile.create",
            target_type="profile",
            target_id=request.name,
            target_host=host,
            target_project=owner,
            result="success",
            details={"description": request.description}
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("profiles")
        return {
            "success": True,
            "message": f"Profile {request.name} created successfully",
            "profile": _detail_of(request.name, created, host, owner, etag),
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
            error_code=type(e).__name__,
            error_message=str(e)
        )
        raise _write_failure(e, request.name, host)


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
    request: ProfileUpdateRequest = ProfileUpdateRequest(),
    if_match: Optional[str] = Header(
        None,
        description=(
            "The etag from the single read. When sent, the write is refused with 412 if the "
            "profile changed since; when omitted the write is unconditional."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a profile."""
    verify_cluster_access(host)
    _check_path_name(profile_name)
    changed = [k for k in ("description", "config", "devices") if getattr(request, k) is not None]
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        written, etag = await asyncio.to_thread(
            update_profile_sync, client, profile_name, owner,
            config=request.config, devices=request.devices, description=request.description,
            if_match=if_match,
        )
        
        # Log activity: profile update success
        activity_logger.log_activity(
            action="profile.update",
            target_type="profile",
            target_id=profile_name,
            target_host=host,
            target_project=owner,
            result="success",
            details={"changed": changed, "etag_checked": bool(if_match)},
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("profiles")
        return {
            "success": True,
            "message": f"Profile {profile_name} updated successfully",
            "profile": _detail_of(profile_name, written, host, owner, etag),
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
            error_code=type(e).__name__,
            error_message=str(e),
            details={"changed": changed, "etag_checked": bool(if_match)},
        )
        raise _write_failure(e, profile_name, host)


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
    _check_path_name(profile_name)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "profiles", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this profile")
        current, _etag = await asyncio.to_thread(read_profile_sync, client, profile_name, owner)
        visible_used_by = filter_visible_lxd_references(host, _bounded_used_by(host, current.get("used_by") or []))
        await asyncio.to_thread(delete_profile_sync, client, profile_name, owner, visible_used_by)
        
        # Log activity: profile deletion success
        activity_logger.log_activity(
            action="profile.delete",
            target_type="profile",
            target_id=profile_name,
            target_host=host,
            target_project=owner,
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
            error_code=type(e).__name__,
            error_message=str(e)
        )
        raise _write_failure(e, profile_name, host)


def _write_failure(exc: Exception, profile_name: str, host: str) -> HTTPException:
    """What a failed profile write answers: the daemon's refusal, the precondition, the
    in-use guard, or a transport failure, each with its own status and sentence."""
    if isinstance(exc, PreconditionFailed):
        return HTTPException(status_code=412, detail=STALE_PROFILE_DETAIL)
    if isinstance(exc, ProfileInUse):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LXDAPIException):
        return _refusal(exc, profile_name)
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        # see instances.py: with Retry mounted a read timeout surfaces as ConnectionError
        return HTTPException(
            status_code=504,
            detail=(
                f"{host} did not finish the change in time. It may still be applying, so "
                "refresh the profile before retrying."
            ),
        )
    return HTTPException(status_code=500, detail=str(exc))
