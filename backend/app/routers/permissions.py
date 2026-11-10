"""
API Router for Identity and Group management (Permissions).
Provides endpoints for managing LXD identities and groups.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Any, Set
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from pylxd.exceptions import LXDAPIException
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect, complete_reasons
from app.models.schemas import FanoutReachability
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, get_current_user, require_partner
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.whole_cluster_access import clusters_held_elsewhere, verify_whole_cluster_access
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.cache import get_cache_manager
from app.services.lxd.lazy_client import client_for
from pydantic import BaseModel, Field
from app.core.lxd_path import safe_segment
from app.services.lxd import client_identities

logger = logging.getLogger(__name__)

identities_router = APIRouter(
    prefix="/identities",
    tags=["Identities"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

groups_router = APIRouter(
    prefix="/groups",
    tags=["Groups"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

_executor = ThreadPoolExecutor(max_workers=5)


class IdentityInfo(BaseModel):
    """An LXD identity, or on Incus a trust-store certificate."""
    id: str = Field(..., description="Identity ID")
    name: str = Field("", description="Identity name")
    type: str = Field("", description="Identity type (display name)")
    auth_type: str = Field("", description="Authentication type for API (tls or oidc)")
    groups: List[str] = Field(default_factory=list, description="Groups this identity belongs to")
    node: str = Field("", description="Cluster this identity belongs to")
    # Incus has no identities or groups: its clients are trust-store certificates, each restricted
    # to a list of projects or not. On an Incus row `id` is the fingerprint and `groups` is empty.
    engine: str = Field("lxd", description="lxd (an identity) or incus (a trust-store certificate)")
    restricted: Optional[bool] = Field(None, description="Incus only: whether the certificate is held to `projects`")
    projects: Optional[List[str]] = Field(None, description="Incus only: the projects a restricted certificate reaches")


class GroupIdentities(BaseModel):
    """Group identities organized by auth type."""
    tls: List[str] = Field(default_factory=list, description="TLS certificate identities")
    oidc: List[str] = Field(default_factory=list, description="OIDC identities")


class GroupInfo(BaseModel):
    """An LXD authorization group and its permissions."""
    name: str = Field(..., description="Group name")
    description: str = Field("", description="Group description")
    identities: GroupIdentities = Field(default_factory=GroupIdentities, description="Identities in this group organized by auth type")
    permissions: List[Dict[str, Any]] = Field(default_factory=list, description="Group permissions")
    node: str = Field("", description="Cluster this group belongs to")


class IdentityListResponse(FanoutReachability):
    """Response for listing identities."""
    total: int = Field(..., description="Total number of identities")
    identities: List[IdentityInfo] = Field(..., description="List of identities")
    nodes: List[str] = Field(..., description="Clusters included in this response")
    node_engines: Dict[str, str] = Field(default_factory=dict, description="Engine per answering cluster: lxd or incus")


class GroupListResponse(FanoutReachability):
    """Response for listing groups."""
    total: int = Field(..., description="Total number of groups")
    groups: List[GroupInfo] = Field(..., description="List of groups")
    nodes: List[str] = Field(..., description="Clusters included in this response")
    unsupported_nodes: List[str] = Field(
        default_factory=list, description="Clusters not asked because their engine (Incus) has no groups"
    )


class IdentityAddRequest(BaseModel):
    """Request to add identity to group."""
    auth_type: str = Field(..., description="Authentication type: tls or oidc")
    identity_id: str = Field(..., description="Identity ID or name to add")


def _get_auth_type_from_type(identity_type: str) -> str:
    """Map identity type display name to API auth type."""
    type_lower = identity_type.lower()
    if "oidc" in type_lower:
        return "oidc"
    # Default to tls for certificate-based types
    return "tls"


def _known_incus(client: Any) -> bool:
    """True only when the cluster has said it is Incus; an unknown engine keeps the LXD path."""
    try:
        return client_identities.engine_of(client) == client_identities.INCUS
    except client_identities.EngineUnknown:
        return False


def _pooled_is_incus(lxd_service: LXDService, node: str) -> bool:
    """Whether the node is Incus, from what its pooled client already says, without a round trip."""
    peek = getattr(lxd_service.connection_manager, "peek_client", None)
    return _known_incus(peek(node)) if callable(peek) else False


def _refuse_groups_on_incus(client: Any, host: str) -> None:
    if _known_incus(client):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "groups_not_on_incus",
                "message": f"{host} runs Incus, which has no groups. Its clients are trust-store certificates, "
                "each restricted to its projects; see Identities.",
            },
        )


def _certificate_row(entry: Dict[str, Any], node_name: str) -> IdentityInfo:
    return IdentityInfo(
        id=entry.get("fingerprint") or "",
        name=entry.get("name") or "",
        type=entry.get("type") or "",
        auth_type="tls",
        groups=[],
        node=node_name,
        engine=client_identities.INCUS,
        restricted=bool(entry.get("restricted")),
        projects=list(entry.get("projects") or []),
    )


def _fetch_identities_for_node(lxd_service: LXDService, node_name: str) -> NodeRead[IdentityInfo]:
    """One cluster's identities. Runs in the thread pool.

    A refusal or a failed read propagates to the caller, which names the cluster, so a cluster
    whose identities could not be read never looks like a cluster with none.
    """
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. Raise so the caller names the cluster; a None
        # client would fail below with an AttributeError instead.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    read: NodeRead[IdentityInfo] = NodeRead()
    if _known_incus(client):
        entries = client.api.certificates.get(params={'recursion': '1'}, timeout=(5, 30)).json().get('metadata') or []
        for entry in entries:
            if isinstance(entry, dict):
                read.rows.append(_certificate_row(entry, node_name))
        return read
    # Use recursion=1 to get full details in one call
    response = client.api.auth.identities.get(params={'recursion': '1'}, timeout=(5, 30))
    for identity_data in response.json().get('metadata') or []:
        if not isinstance(identity_data, dict):
            continue
        try:
            identity_type = identity_data.get('type') or ''
            read.rows.append(IdentityInfo(
                id=identity_data.get('id') or '',
                name=identity_data.get('name') or '',
                type=identity_type,
                auth_type=_get_auth_type_from_type(identity_type),
                groups=identity_data.get('groups') or [],
                node=node_name,
                engine=client_identities.LXD,
                restricted=None,
                projects=None,
            ))
        except Exception as exc:  # noqa: BLE001 - one malformed row is named, not fatal
            read.lost(f"identity {identity_data.get('id', '?')}: {type(exc).__name__}")
    return read


# Identity Endpoints
@identities_router.get(
    "",
    response_model=IdentityListResponse,
    summary="List all identities",
    description="Get a list of all identities from connected clusters.",
)
async def list_identities(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List all identities."""
    if host:
        verify_cluster_access(host)
        await verify_whole_cluster_access(host, db, user)
    try:
        # important: another party's groups and identities carry its project URLs and client
        # certificates, so a bounded caller reads them no more than it may change them.
        held = set() if host else await clusters_held_elsewhere(
            [*lxd_service.connection_manager.get_node_names(), *lxd_service.get_node_names()], db, user
        )
        nodes_to_query = [host] if host else [
            node for node in lxd_service.connection_manager.get_node_names() if node not in held
        ]
        
        # Fetch identities from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_identities_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # important: two things make a node absent, and both must be named. A node can fail
        # during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while the
        # fleet is whatever Vault has registered. Naming only the first hides a dead cluster.
        unreachable_nodes: List[str] = (
            [] if host else [
                node for node in sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
                if node not in held
            ]
        )
        fan = collect(nodes_to_query, results, noun="identities")
        unreachable_nodes.extend(fan.unreachable_nodes)
        all_identities = fan.rows
        node_engines = {
            node: (client_identities.INCUS if _pooled_is_incus(lxd_service, node) else client_identities.LXD)
            for node in nodes_to_query
            if node not in unreachable_nodes
        }
        
        return IdentityListResponse(
            total=len(all_identities),
            identities=all_identities,
            node_engines=node_engines,
            nodes=[node for node in lxd_service.connection_manager.get_node_names() if node not in held],
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            node_errors=complete_reasons(unreachable_nodes, fan.node_errors, partial=fan.partial_nodes),
        )
    except HTTPException:
        # A refusal or a 503 keeps its own status instead of becoming a 500.
        raise
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
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get details for a specific identity."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        if _known_incus(client):
            entry = client.api.certificates[safe_segment(identity_id)].get(timeout=(5, 30)).json().get('metadata') or {}
            if entry.get('fingerprint') != identity_id:
                # The daemon resolves a prefix; only the exact entry is the one asked for.
                raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
            return _certificate_row(entry, host)
        response = client.api["auth"]["identities"][safe_segment(identity_id)].get()
        
        if response.status_code == 200:
            identity_data = response.json().get('metadata', {})
            # Same shape as the list route: auth_type is derived from the type, and node is the
            # host validated above.
            return IdentityInfo(
                id=identity_data.get('id', ''),
                name=identity_data.get('name', ''),
                type=identity_data.get('type', ''),
                auth_type=_get_auth_type_from_type(identity_data.get('type', '')),
                groups=identity_data.get('groups', []),
                node=host,
                engine=client_identities.LXD,
                restricted=None,
                projects=None,
            )
        else:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting identity {identity_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")


class _NoGroupsHere(Exception):
    """The node turned out to be Incus once connected, so it is reported as unsupported."""


def _fetch_groups_for_node(lxd_service: LXDService, node_name: str) -> NodeRead[GroupInfo]:
    """One cluster's groups. Runs in the thread pool."""
    client = lxd_service.connection_manager.get_client(node_name)

    # If client is None, try to reconnect
    if client is None:
        logger.info(f"Reconnecting to {node_name} for groups list")
        try:
            client = lxd_service.cert_manager.connect_with_vault_cert(node_name, connection_manager=lxd_service.connection_manager)
        except Exception as e:
            # important: raise so the caller names the node as unreachable; returning would
            # read as a node with no groups.
            logger.warning(f"Failed to reconnect to {node_name}: {e}")
            raise LXDConnectionError(f"Could not connect to {node_name}") from e
        if client is None:
            # important: raise for the same reason; a bare collection would read as a
            # cluster with no groups.
            raise LXDConnectionError(f"Could not connect to {node_name}")
    if _known_incus(client):
        raise _NoGroupsHere(node_name)

    # Use recursion=1 to get full details in one call
    response = client.api.auth.groups.get(params={'recursion': '1'}, timeout=(5, 30))
    read: NodeRead[GroupInfo] = NodeRead()
    for group_data in response.json().get('metadata') or []:
        if not isinstance(group_data, dict):
            continue
        try:
            identities_dict = group_data.get('identities') or {}

            # LXD returns identities as nested dict: {auth_type: [ids]}
            # Preserve the structure for frontend
            tls_identities = []
            oidc_identities = []
            if isinstance(identities_dict, dict):
                tls_identities = identities_dict.get('tls') or []
                oidc_identities = identities_dict.get('oidc') or []

            read.rows.append(GroupInfo(
                name=group_data.get('name') or '',
                description=group_data.get('description') or '',
                identities=GroupIdentities(tls=tls_identities, oidc=oidc_identities),
                permissions=group_data.get('permissions') or [],
                node=node_name
            ))
        except Exception as exc:  # noqa: BLE001 - one malformed row is named, not fatal
            read.lost(f"group {group_data.get('name', '?')}: {type(exc).__name__}")
    return read


# Group Endpoints
@groups_router.get(
    "",
    response_model=GroupListResponse,
    summary="List all groups",
    description="Get a list of all groups from connected clusters.",
)
async def list_groups(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List all groups."""
    if host:
        verify_cluster_access(host)
        await verify_whole_cluster_access(host, db, user)
    try:
        # important: another party's groups and identities carry its project URLs and client
        # certificates, so a bounded caller reads them no more than it may change them.
        held = set() if host else await clusters_held_elsewhere(
            [*lxd_service.connection_manager.get_node_names(), *lxd_service.get_node_names()], db, user
        )
        nodes_to_query = [host] if host else [
            node for node in lxd_service.connection_manager.get_node_names() if node not in held
        ]
        # An Incus node has no /1.0/auth and answers 404, which would read as an unreachable
        # cluster. It is listed as unsupported instead, from what its pooled client already says.
        unsupported_nodes = [n for n in nodes_to_query if _pooled_is_incus(lxd_service, n)]
        nodes_to_query = [n for n in nodes_to_query if n not in unsupported_nodes]
        
        # Fetch groups from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_groups_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # A worker with no pooled client only learns the engine inside the fan-out.
        found_incus = [n for n, r in zip(nodes_to_query, results) if isinstance(r, _NoGroupsHere)]
        if found_incus:
            unsupported_nodes += found_incus
            kept = [(n, r) for n, r in zip(nodes_to_query, results) if n not in found_incus]
            nodes_to_query, results = [n for n, _ in kept], [r for _, r in kept]

        # important: two things make a node absent, and both must be named. A node can fail
        # during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while the
        # fleet is whatever Vault has registered. Naming only the first hides a dead cluster.
        unreachable_nodes: List[str] = (
            [] if host else [
                node for node in sorted(set(lxd_service.get_node_names()) - set(nodes_to_query) - set(unsupported_nodes))
                if node not in held
            ]
        )
        fan = collect(nodes_to_query, results, noun="groups")
        unreachable_nodes.extend(fan.unreachable_nodes)
        all_groups = fan.rows
        
        return GroupListResponse(
            total=len(all_groups),
            groups=all_groups,
            unsupported_nodes=unsupported_nodes,
            nodes=[node for node in lxd_service.connection_manager.get_node_names() if node not in held],
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            node_errors=complete_reasons(unreachable_nodes, fan.node_errors, partial=fan.partial_nodes),
        )
    except HTTPException:
        # A refusal or a 503 keeps its own status instead of becoming a 500.
        raise
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
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get details for a specific group."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        _refuse_groups_on_incus(client, host)
        
        response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
        
        if response.status_code == 200:
            group_data = response.json().get('metadata', {})
            identities_dict = group_data.get('identities', {})
            
            # LXD returns identities as nested dict: {auth_type: [ids]}
            # Preserve the structure for frontend
            tls_identities = []
            oidc_identities = []
            if isinstance(identities_dict, dict):
                tls_identities = identities_dict.get('tls', []) or []
                oidc_identities = identities_dict.get('oidc', []) or []
            
            # Handle None values from LXD API
            permissions_list = group_data.get('permissions')
            if permissions_list is None:
                permissions_list = []
            
            return GroupInfo(
                name=group_data.get('name', ''),
                description=group_data.get('description', ''),
                identities=GroupIdentities(tls=tls_identities, oidc=oidc_identities),
                permissions=permissions_list,
                node=host
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
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a new group."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        _refuse_groups_on_incus(client, host)
        
        group_data = {
            "name": name,
            "description": description,
            "permissions": permissions
        }
        
        response = client.api.auth.groups.post(json=group_data, timeout=(5, 30))
        
        if response.status_code in [200, 201, 202]:
            return {
                "success": True,
                "message": f"Group {name} created successfully"
            }
        else:
            # Extract LXD's error detail for the log and the response.
            try:
                lxd_error = response.json()
                error_msg = lxd_error.get("error", "Unknown LXD error")
                error_code = lxd_error.get("error_code", response.status_code)
                logger.error(f"LXD create group failed ({error_code}): {error_msg}")
            except Exception:
                error_msg = response.text or "Unknown error from LXD"
                error_code = response.status_code
                logger.error(f"LXD create group failed ({error_code}): {error_msg}")
            
            raise HTTPException(
                status_code=error_code if 400 <= error_code < 600 else 500,
                detail=error_msg,
            )
    except HTTPException:
        raise
    except LXDAPIException as e:
        # pylxd raises on a 4xx when LXD refuses the request (a permission naming a
        # resource in a project the group cannot view, a duplicate name). Pass LXD's own status
        # and message through instead of answering 500.
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(f"LXD refused creating group {name}: {e}")
        raise HTTPException(status_code=status if status and 400 <= status < 500 else 502, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating group: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class GroupUpdateRequest(BaseModel):
    """Request to update a group."""
    description: Optional[str] = Field(None, description="Group description")
    permissions: Optional[List[Dict[str, Any]]] = Field(None, description="Group permissions")


@groups_router.patch(
    "/{group_name}",
    response_model=dict,
    summary="Update group",
    description="Update an existing group's description and/or permissions.",
)
async def update_group(
    group_name: str,
    request: GroupUpdateRequest,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Update a group's description and/or permissions.
    
    LXD replaces the whole permissions array on write and may persist only one of several
    permissions sent together, so they are cleared and then added one at a time (GET, merge,
    PUT, verify). Project and server permissions go first: LXD validates a nested permission
    only once its project permission exists. A permission LXD refuses is skipped.
    """
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    import asyncio
    
    try:
        client = require_client(await client_for(lxd_service, host), host)
        _refuse_groups_on_incus(client, host)
        
        # Build update data - only include fields that were provided
        update_data = {}
        if request.description is not None:
            update_data["description"] = request.description
        
        if not update_data and request.permissions is None:
            return {
                "success": True,
                "message": f"No changes to update for group {group_name}"
            }
        
        # Permissions are applied one at a time; see the docstring.
        if request.permissions is not None:
            permissions = request.permissions
            
            # Entity types that require project access first (nested resources)
            nested_types = {'instance', 'image', 'profile', 'network', 'network_acl', 
                           'network_zone', 'storage_pool', 'storage_volume', 'storage_bucket'}
            
            # Separate project/server permissions from nested permissions
            project_perms = [p for p in permissions if p.get('entity_type') in ('project', 'server')]
            nested_perms = [p for p in permissions if p.get('entity_type') in nested_types]
            
            # Sort permissions: project/server first, then nested
            # This ensures base permissions exist before dependent nested ones
            sorted_permissions = project_perms + nested_perms
            
            logger.info(f"Updating group {group_name} with {len(sorted_permissions)} permissions using incremental approach")
            logger.info(f"Order: {len(project_perms)} project/server perms first, then {len(nested_perms)} nested perms")
            
            def permission_exists(perm, current_list):
                for existing in current_list:
                    if (existing.get('entity_type') == perm.get('entity_type') and
                        existing.get('url') == perm.get('url') and
                        existing.get('entitlement') == perm.get('entitlement')):
                        return True
                return False
            
            accumulated_perms = []
            
            # First, clear all permissions
            clear_data = {"permissions": []}
            if request.description is not None:
                clear_data["description"] = request.description
            
            logger.info(f"Step 0: Clearing all existing permissions for group {group_name}")
            clear_response = client.api.auth.groups[safe_segment(group_name)].patch(json=clear_data, timeout=(5, 30))
            
            if clear_response.status_code not in [200, 202]:
                error_detail = clear_response.json().get('error', 'Could not clear permissions')
                logger.error(f"LXD returned error when clearing permissions: {error_detail}")
                raise HTTPException(status_code=clear_response.status_code, detail=error_detail)
            
            await asyncio.sleep(0.5)
            
            # LXD replaces the permissions array rather than merging, so each step reads the
            # current list, adds one permission and PUTs the full list, pausing between steps
            # to let LXD persist.
            for idx, perm in enumerate(sorted_permissions):
                accumulated_perms.append(perm)
                logger.info(f"Step {idx + 1}/{len(sorted_permissions)}: Adding permission {perm}")
                
                # Read the current state right before the PUT.
                await asyncio.sleep(0.3)
                get_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
                current_in_lxd = []
                if get_response.status_code == 200:
                    current_in_lxd = get_response.json().get('metadata', {}).get('permissions', [])
                    logger.info(f"Current LXD state has {len(current_in_lxd)} permissions: {current_in_lxd}")
                
                merged_perms = current_in_lxd.copy()
                if not permission_exists(perm, merged_perms):
                    merged_perms.append(perm)
                
                # Use PUT instead of PATCH for complete replacement
                put_data = {"permissions": merged_perms}
                logger.info(f"Sending PUT with {len(merged_perms)} permissions: {merged_perms}")
                
                response = client.api.auth.groups[safe_segment(group_name)].put(json=put_data, timeout=(5, 30))
                
                if response.status_code not in [200, 202]:
                    error_detail = response.json().get('error', f'Could not add permission at step {idx + 1}')
                    logger.error(f"LXD returned error at step {idx + 1}: {error_detail}")
                    # Skip this permission and continue with the rest.
                    logger.warning(f"Permission {perm} failed, but continuing with remaining permissions")
                    accumulated_perms.pop()
                else:
                    # Longer delay between permissions to let LXD fully persist
                    await asyncio.sleep(0.5)
                    
                    # Verify the permission was added with multiple retries
                    max_verify_attempts = 3
                    verified = False
                    
                    for attempt in range(max_verify_attempts):
                        verify_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
                        if verify_response.status_code == 200:
                            current_perms = verify_response.json().get('metadata', {}).get('permissions', [])
                            
                            if permission_exists(perm, current_perms):
                                logger.info(f"Verified: permission {perm.get('entitlement')} on {perm.get('url')} added successfully (attempt {attempt + 1})")
                                verified = True
                                break
                            else:
                                logger.warning(f"Permission {perm} not found after PATCH (attempt {attempt + 1}/{max_verify_attempts})")
                                if attempt < max_verify_attempts - 1:
                                    await asyncio.sleep(0.5)
                                    # Retry the PUT.
                                    retry_response = client.api.auth.groups[safe_segment(group_name)].put(json=put_data, timeout=(5, 30))
                                    if retry_response.status_code not in [200, 202]:
                                        logger.error(f"Retry PATCH failed: {retry_response.json().get('error', 'Unknown error')}")
                                    await asyncio.sleep(0.5)
                    
                    if not verified:
                        logger.error(f"Failed to verify permission {perm} after {max_verify_attempts} attempts")
                        accumulated_perms.pop()
            
            # Final verification
            final_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
            if final_response.status_code == 200:
                final_perms = final_response.json().get('metadata', {}).get('permissions', [])
                logger.info(f"Final state: group {group_name} has {len(final_perms)} permissions")
                
                if len(final_perms) < len(sorted_permissions):
                    logger.warning(f"Some permissions may not have been saved. Expected {len(sorted_permissions)}, got {len(final_perms)}")
            
            # Invalidate cache
            cache = get_cache_manager()
            cache.delete_pattern("api_responses", "groups:*")
            
            return {
                "success": True,
                "message": f"Group {group_name} updated successfully with {len(accumulated_perms)} permissions"
            }
        else:
            # Description only: PUT it with the current permissions, because LXD may ignore a
            # PATCH that sets an empty string.
            logger.info(f"Updating group {group_name} description only to: '{update_data.get('description', '')}'")
            
            get_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
            if get_response.status_code == 200:
                current_group = get_response.json().get('metadata', {})
                current_perms = current_group.get('permissions', [])
                
                put_data = {
                    "description": update_data.get("description", ""),
                    "permissions": current_perms
                }
                
                # Use PUT to replace the entire group config
                response = client.api.auth.groups[safe_segment(group_name)].put(json=put_data, timeout=(5, 30))
            else:
                # Fallback to PATCH if GET fails
                response = client.api.auth.groups[safe_segment(group_name)].patch(json=update_data, timeout=(5, 30))
        
            if response.status_code in [200, 202]:
                # Invalidate cache
                cache = get_cache_manager()
                cache.delete_pattern("api_responses", "groups:*")
                
                return {
                    "success": True,
                    "message": f"Group {group_name} updated successfully"
                }
            else:
                error_detail = response.json().get('error', 'Could not update group')
                logger.error(f"LXD returned error for group update: {error_detail}")
                raise HTTPException(status_code=response.status_code, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating group {group_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@groups_router.delete(
    "/{group_name}",
    response_model=dict,
    summary="Delete group",
    description="Delete a group.",
)
async def delete_group(
    group_name: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Delete a group."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        _refuse_groups_on_incus(client, host)
        
        response = client.api.auth.groups[safe_segment(group_name)].delete(timeout=(5, 30))
        
        if response.status_code in [200, 202]:
            return {
                "success": True,
                "message": f"Group {group_name} deleted successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Could not delete group")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting group {group_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@groups_router.post(
    "/{group_name}/identities",
    response_model=dict,
    summary="Add identity to group",
    description="Add a TLS or OIDC identity to a group.",
)
async def add_identity_to_group(
    group_name: str,
    request: IdentityAddRequest,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Add an identity to a group by updating the identity's groups list."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        _refuse_groups_on_incus(client, host)
        
        # Get current identity data
        auth_type = request.auth_type.lower()
        identity_id = request.identity_id
        
        # Navigate using path segments
        get_response = client.api.auth.identities[safe_segment(auth_type)][safe_segment(identity_id)].get(timeout=(5, 30))
        if get_response.status_code != 200:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
        
        identity_data = get_response.json().get('metadata', {})
        current_groups = identity_data.get('groups', [])
        
        # Check if already in group
        if group_name in current_groups:
            return {
                "success": True,
                "message": f"Identity {identity_id} already exists in group {group_name}"
            }
        
        updated_groups = current_groups + [group_name]
        
        update_data = {"groups": updated_groups}
        
        logger.info(f"Adding identity {identity_id} to group {group_name}. Groups: {updated_groups}")
        response = client.api.auth.identities[safe_segment(auth_type)][safe_segment(identity_id)].patch(json=update_data, timeout=(5, 30))
        
        if response.status_code in [200, 202]:
            # Invalidate cache
            cache = get_cache_manager()
            cache.delete_pattern("api_responses", "groups:*")
            cache.delete_pattern("api_responses", "identities:*")
            
            return {
                "success": True,
                "message": f"Identity {identity_id} added to group {group_name}"
            }
        else:
            error_detail = response.json().get('error', 'Could not add identity to group')
            raise HTTPException(status_code=response.status_code, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding identity to group {group_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@groups_router.delete(
    "/{group_name}/identities/{auth_type}/{identity_id}",
    response_model=dict,
    summary="Remove identity from group",
    description="Remove a TLS or OIDC identity from a group.",
)
async def remove_identity_from_group(
    group_name: str,
    auth_type: str,
    identity_id: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Remove an identity from a group by updating the identity's groups list."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        _refuse_groups_on_incus(client, host)
        
        # Get current identity data
        auth_type_lower = auth_type.lower()
        
        # Navigate using path segments
        get_response = client.api.auth.identities[safe_segment(auth_type_lower)][safe_segment(identity_id)].get(timeout=(5, 30))
        if get_response.status_code != 200:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
        
        identity_data = get_response.json().get('metadata', {})
        current_groups = identity_data.get('groups', [])
        
        # Check if group exists in identity's groups
        if group_name not in current_groups:
            return {
                "success": True,
                "message": f"Identity {identity_id} not found in group {group_name}"
            }
        
        # LXD requires every identity to belong to at least one group
        # Prevent removal if this is the only group
        if len(current_groups) == 1:
            raise HTTPException(
                status_code=400, 
                detail="Cannot remove identity from its only group. Identity must belong to at least one group."
            )
        
        updated_groups = [g for g in current_groups if g != group_name]
        
        update_data = {"groups": updated_groups}
        
        logger.info(f"Removing identity {identity_id} from group {group_name}. Groups: {updated_groups}")
        response = client.api.auth.identities[safe_segment(auth_type_lower)][safe_segment(identity_id)].patch(json=update_data, timeout=(5, 30))
        
        if response.status_code in [200, 202]:
            # Invalidate cache
            cache = get_cache_manager()
            cache.delete_pattern("api_responses", "groups:*")
            cache.delete_pattern("api_responses", "identities:*")
            
            return {
                "success": True,
                "message": f"Identity {identity_id} removed from group {group_name}"
            }
        else:
            error_detail = response.json().get('error', 'Could not remove identity from group')
            raise HTTPException(status_code=response.status_code, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing identity from group {group_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _dashboard_fingerprints(cert_manager: Any, host: str) -> Set[str]:
    """Blocking. The fingerprints of the certificates Vault holds for this dashboard on `host`.

    Current and pending both count. Only "not there" (404) reads as absent: the certificates page
    reader treats every pending-read failure as no pending certificate, which here would leave a
    rotation's new certificate unprotected.
    """
    from app.core.vault_client import VaultClientError
    from app.services.lxd_certs.inspect import parse_certificate_pem

    held: Set[str] = set()
    for leaf in ("certs", "pending_certs"):
        try:
            pem = (cert_manager.vault.vault_read(f"{cert_manager.clusters_path}/{host}/{leaf}") or {}).get("client.crt")
        except VaultClientError as exc:
            if getattr(exc, "status_code", None) == 404:
                continue
            raise
        if pem:
            fingerprint = parse_certificate_pem(pem).get("fingerprint")
            if not fingerprint:
                raise ValueError(f"the {leaf} certificate for {host} could not be read")
            held.add(fingerprint)
    return held


async def _delete_incus_certificate(lxd_service: LXDService, client: Any, host: str, fingerprint: str) -> Dict[str, Any]:
    """Remove one client certificate from an Incus trust store, never the dashboard's own."""
    # important: removing a certificate this dashboard holds cuts it off the cluster, and only shell
    # access to the host could bring it back; deregistration is the intended way to do that. During
    # a regenerate or a rotation, Vault's current and pending certificates count as well as the one
    # this worker presents, and a failed read of them refuses the delete.
    try:
        held = await asyncio.to_thread(_dashboard_fingerprints, lxd_service.cert_manager, host)
    except Exception:  # noqa: BLE001 - logged by the Vault client
        raise HTTPException(status_code=503, detail="The dashboard's own certificates could not be read; nothing was removed.")
    own = {lxd_service.connection_manager.active_certificate_fingerprint(host)} | held
    if fingerprint in own - {None}:
        raise HTTPException(
            status_code=409,
            detail={"error": "own_certificate", "message": "This is a certificate the dashboard uses on this cluster."},
        )
    try:
        entry = client.api.certificates[safe_segment(fingerprint)].get(timeout=(5, 30)).json().get('metadata') or {}
    except LXDAPIException:
        raise HTTPException(status_code=404, detail=f"Identity {fingerprint} not found")
    if entry.get('fingerprint') != fingerprint:
        raise HTTPException(status_code=404, detail=f"Identity {fingerprint} not found")
    if entry.get('type') != 'client':
        # A server entry is a cluster member's own certificate; removing it breaks the cluster.
        raise HTTPException(
            status_code=409,
            detail={"error": "not_a_client", "message": f"{entry.get('name') or fingerprint} is a {entry.get('type')} certificate, not a client."},
        )
    error = client_identities.delete_certificate(client, fingerprint)
    if error:
        raise HTTPException(status_code=502, detail=f"The cluster did not remove the certificate: {error}")
    cache = get_cache_manager()
    cache.delete_pattern("api_responses", "groups:*")
    cache.delete_pattern("api_responses", "identities:*")
    logger.info(f"Deleted Incus certificate {fingerprint} from {host}")
    return {"success": True, "message": f"Identity {fingerprint} deleted successfully"}


@identities_router.delete(
    "/{auth_type}/{identity_id}",
    response_model=dict,
    summary="Delete identity",
    description="Delete a TLS or OIDC identity from the LXD server.",
)
async def delete_identity(
    auth_type: str,
    identity_id: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Delete an identity from the LXD server."""
    verify_cluster_access(host)
    await verify_whole_cluster_access(host, db, user)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        if _known_incus(client):
            return await _delete_incus_certificate(lxd_service, client, host, identity_id)
        
        auth_type_lower = auth_type.lower()
        
        get_response = client.api.auth.identities[safe_segment(auth_type_lower)][safe_segment(identity_id)].get(timeout=(5, 30))
        if get_response.status_code != 200:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
        
        logger.info(f"Deleting identity {identity_id} (type: {auth_type}) from {host}")
        response = client.api.auth.identities[safe_segment(auth_type_lower)][safe_segment(identity_id)].delete(timeout=(5, 30))
        
        if response.status_code in [200, 202]:
            # Invalidate cache
            cache = get_cache_manager()
            cache.delete_pattern("api_responses", "groups:*")
            cache.delete_pattern("api_responses", "identities:*")
            
            return {
                "success": True,
                "message": f"Identity {identity_id} deleted successfully"
            }
        else:
            error_detail = response.json().get('error', 'Could not delete identity')
            raise HTTPException(status_code=response.status_code, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting identity {identity_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
