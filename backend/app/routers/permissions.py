"""
API Router for Identity and Group management (Permissions).
Provides endpoints for managing LXD identities and groups.
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
from app.core.dependencies import check_read_only_mode, require_partner
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.cache import get_cache_manager
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Identities Router
identities_router = APIRouter(
    prefix="/identities",
    tags=["Identities"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Groups Router
groups_router = APIRouter(
    prefix="/groups",
    tags=["Groups"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class IdentityInfo(BaseModel):
    """Identity information."""
    id: str = Field(..., description="Identity ID")
    name: str = Field("", description="Identity name")
    type: str = Field("", description="Identity type (display name)")
    auth_type: str = Field("", description="Authentication type for API (tls or oidc)")
    groups: List[str] = Field(default_factory=list, description="Groups this identity belongs to")
    node: str = Field("", description="Node/cluster this identity belongs to")


class GroupIdentities(BaseModel):
    """Group identities organized by auth type."""
    tls: List[str] = Field(default_factory=list, description="TLS certificate identities")
    oidc: List[str] = Field(default_factory=list, description="OIDC identities")


class GroupInfo(BaseModel):
    """Group information."""
    name: str = Field(..., description="Group name")
    description: str = Field("", description="Group description")
    identities: GroupIdentities = Field(default_factory=GroupIdentities, description="Identities in this group organized by auth type")
    permissions: List[Dict[str, Any]] = Field(default_factory=list, description="Group permissions")
    node: str = Field("", description="Node/cluster this group belongs to")


class IdentityListResponse(BaseModel):
    """Response for listing identities."""
    total: int = Field(..., description="Total number of identities")
    identities: List[IdentityInfo] = Field(..., description="List of identities")
    nodes: List[str] = Field(..., description="List of nodes")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their entries are missing from this list, "
            "so an unreachable node is distinguishable from one that has none."
        ),
    )


class GroupListResponse(BaseModel):
    """Response for listing groups."""
    total: int = Field(..., description="Total number of groups")
    groups: List[GroupInfo] = Field(..., description="List of groups")
    nodes: List[str] = Field(..., description="List of nodes")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their entries are missing from this list, "
            "so an unreachable node is distinguishable from one that has none."
        ),
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


def _fetch_identities_for_node(lxd_service: LXDService, node_name: str) -> List[IdentityInfo]:
    """Fetch identities for a single node - runs in thread pool."""
    identities = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Use recursion=1 to get full details in one call
        response = client.api.auth.identities.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            identities_data = response.json().get('metadata', [])
            for identity_data in identities_data:
                try:
                    # Handle None values from LXD API
                    groups_list = identity_data.get('groups')
                    if groups_list is None:
                        groups_list = []
                    
                    identity_type = identity_data.get('type', '')
                    identities.append(IdentityInfo(
                        id=identity_data.get('id', ''),
                        name=identity_data.get('name', ''),
                        type=identity_type,
                        auth_type=_get_auth_type_from_type(identity_type),
                        groups=groups_list,
                        node=node_name
                    ))
                except Exception as e:
                    logger.error(f"Error parsing identity data: {e}")
                    raise
        else:
            logger.warning(f"Got status {response.status_code} for identities on {node_name}")
    except Exception as e:
        logger.error(f"Error listing identities on {node_name}: {e}")
    return identities


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
    if host:
        verify_cluster_access(host)
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch identities from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_identities_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_identities = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                all_identities.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching identities: {result}")
                unreachable_nodes.append(node_name)
        
        return IdentityListResponse(
            total=len(all_identities),
            identities=all_identities,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
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
    verify_cluster_access(host)
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


def _fetch_groups_for_node(lxd_service: LXDService, node_name: str) -> List[GroupInfo]:
    """Fetch groups for a single node - runs in thread pool."""
    groups = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {node_name} for groups list")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(node_name, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.warning(f"Failed to reconnect to {node_name}: {e}")
                return groups
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                return groups
        
        # Use recursion=1 to get full details in one call
        response = client.api.auth.groups.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            groups_data = response.json().get('metadata', [])
            for group_data in groups_data:
                try:
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
                    
                    groups.append(GroupInfo(
                        name=group_data.get('name', ''),
                        description=group_data.get('description', ''),
                        identities=GroupIdentities(tls=tls_identities, oidc=oidc_identities),
                        permissions=permissions_list,
                        node=node_name  # Include node information
                    ))
                except Exception as e:
                    logger.error(f"Error parsing group data: {e}")
                    raise
        else:
            logger.warning(f"Got status {response.status_code} for groups on {node_name}")
    except Exception as e:
        logger.error(f"Error listing groups on {node_name}: {e}")
    return groups


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
    if host:
        verify_cluster_access(host)
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch groups from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_groups_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_groups = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                all_groups.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching groups: {result}")
                unreachable_nodes.append(node_name)
        
        return GroupListResponse(
            total=len(all_groups),
            groups=all_groups,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
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
    verify_cluster_access(host)
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for group lookup")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
        response = client.api.auth.groups[group_name].get(timeout=(5, 30))
        
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
                node=host  # Include node information
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
    verify_cluster_access(host)
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for create group")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
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
            # extract LXD error detail for debugging
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
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Update an existing group.
    
    CRITICAL LXD LIMITATION:
    LXD's PATCH endpoint for groups has a bug/limitation where sending multiple permissions
    in a single request may only persist one of them. Additionally, PATCH replaces the
    entire permissions array rather than merging.
    
    SOLUTION:
    We use PUT instead of PATCH and apply permissions incrementally:
    1. Clear all existing permissions
    2. For each new permission: GET current state -> merge new permission -> PUT complete list
    3. Verify each step before proceeding
    
    This ensures reliable persistence of multiple permissions.
    
    For permissions that reference projects (like instance, network, image, profile, storage_pool),
    LXD requires the project permission to exist BEFORE the nested permission can be validated.
    This endpoint handles this by applying project permissions first, then nested permissions.
    """
    verify_cluster_access(host)
    import asyncio
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for update group")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
        # Build update data - only include fields that were provided
        update_data = {}
        if request.description is not None:
            update_data["description"] = request.description
        
        if not update_data and request.permissions is None:
            return {
                "success": True,
                "message": f"No changes to update for group {group_name}"
            }
        
        # Handle permissions - INCREMENTAL approach to work around LXD limitation
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
            
            # Helper function to check if permission already exists
            def permission_exists(perm, current_list):
                for existing in current_list:
                    if (existing.get('entity_type') == perm.get('entity_type') and
                        existing.get('url') == perm.get('url') and
                        existing.get('entitlement') == perm.get('entitlement')):
                        return True
                return False
            
            # Start with empty or keep description update
            accumulated_perms = []
            
            # First, clear all permissions
            clear_data = {"permissions": []}
            if request.description is not None:
                clear_data["description"] = request.description
            
            logger.info(f"Step 0: Clearing all existing permissions for group {group_name}")
            clear_response = client.api.auth.groups[group_name].patch(json=clear_data, timeout=(5, 30))
            
            if clear_response.status_code not in [200, 202]:
                error_detail = clear_response.json().get('error', 'Failed to clear permissions')
                logger.error(f"LXD returned error when clearing permissions: {error_detail}")
                raise HTTPException(status_code=clear_response.status_code, detail=error_detail)
            
            await asyncio.sleep(0.5)  # Delay after clear
            
            # LXD PATCH replaces permissions array, NOT merges
            # So we need to GET current, add new permission to list, then PATCH with full list
            # Also add long delay between operations to let LXD database persist
            for idx, perm in enumerate(sorted_permissions):
                accumulated_perms.append(perm)
                logger.info(f"Step {idx + 1}/{len(sorted_permissions)}: Adding permission {perm}")
                
                # CRITICAL: GET current state right before PATCH
                await asyncio.sleep(0.3)  # Small delay before GET
                get_response = client.api.auth.groups[group_name].get(timeout=(5, 30))
                current_in_lxd = []
                if get_response.status_code == 200:
                    current_in_lxd = get_response.json().get('metadata', {}).get('permissions', [])
                    logger.info(f"Current LXD state has {len(current_in_lxd)} permissions: {current_in_lxd}")
                
                # Build merged permission list
                merged_perms = current_in_lxd.copy()
                if not permission_exists(perm, merged_perms):
                    merged_perms.append(perm)
                
                # Use PUT instead of PATCH for complete replacement
                put_data = {"permissions": merged_perms}
                logger.info(f"Sending PUT with {len(merged_perms)} permissions: {merged_perms}")
                
                # Try PUT - which does a complete replacement
                response = client.api.auth.groups[group_name].put(json=put_data, timeout=(5, 30))
                
                if response.status_code not in [200, 202]:
                    error_detail = response.json().get('error', f'Failed to add permission at step {idx + 1}')
                    logger.error(f"LXD returned error at step {idx + 1}: {error_detail}")
                    # Don't fail completely - log and try to continue if possible
                    logger.warning(f"Permission {perm} failed, but continuing with remaining permissions")
                    # Remove failed permission from accumulated list
                    accumulated_perms.pop()
                else:
                    # Longer delay between permissions to let LXD fully persist
                    await asyncio.sleep(0.5)
                    
                    # Verify the permission was added with multiple retries
                    max_verify_attempts = 3
                    verified = False
                    
                    for attempt in range(max_verify_attempts):
                        verify_response = client.api.auth.groups[group_name].get(timeout=(5, 30))
                        if verify_response.status_code == 200:
                            current_perms = verify_response.json().get('metadata', {}).get('permissions', [])
                            
                            # Check if our permission is in the list
                            if permission_exists(perm, current_perms):
                                logger.info(f"Verified: permission {perm.get('entitlement')} on {perm.get('url')} added successfully (attempt {attempt + 1})")
                                verified = True
                                break
                            else:
                                logger.warning(f"Permission {perm} not found after PATCH (attempt {attempt + 1}/{max_verify_attempts})")
                                if attempt < max_verify_attempts - 1:
                                    await asyncio.sleep(0.5)
                                    # Retry the PATCH
                                    # note: mirrors the original call above. This line used to read
                                    # `.patch(json=patch_data, ...)`, and patch_data does not exist
                                    # in this scope, so the retry raised NameError the moment a
                                    # permission failed to verify. Nothing exercised it, which is
                                    # why it survived.
                                    retry_response = client.api.auth.groups[group_name].put(json=put_data, timeout=(5, 30))
                                    if retry_response.status_code not in [200, 202]:
                                        logger.error(f"Retry PATCH failed: {retry_response.json().get('error', 'Unknown error')}")
                                    await asyncio.sleep(0.5)
                    
                    if not verified:
                        logger.error(f"Failed to verify permission {perm} after {max_verify_attempts} attempts")
                        accumulated_perms.pop()
            
            # Final verification
            final_response = client.api.auth.groups[group_name].get(timeout=(5, 30))
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
            # No permissions to update - just description
            # For description-only update, we need to fetch current group first
            # then PUT with current permissions + new description
            # This is because PATCH with empty string may be ignored by LXD
            logger.info(f"Updating group {group_name} description only to: '{update_data.get('description', '')}'")
            
            # Get current group state
            get_response = client.api.auth.groups[group_name].get(timeout=(5, 30))
            if get_response.status_code == 200:
                current_group = get_response.json().get('metadata', {})
                current_perms = current_group.get('permissions', [])
                
                # Build PUT data with new description and existing permissions
                put_data = {
                    "description": update_data.get("description", ""),
                    "permissions": current_perms
                }
                
                # Use PUT to replace the entire group config
                response = client.api.auth.groups[group_name].put(json=put_data, timeout=(5, 30))
            else:
                # Fallback to PATCH if GET fails
                response = client.api.auth.groups[group_name].patch(json=update_data, timeout=(5, 30))
        
            if response.status_code in [200, 202]:
                # Invalidate cache
                cache = get_cache_manager()
                cache.delete_pattern("api_responses", "groups:*")
                
                return {
                    "success": True,
                    "message": f"Group {group_name} updated successfully"
                }
            else:
                error_detail = response.json().get('error', 'Failed to update group')
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
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a group."""
    verify_cluster_access(host)
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for delete group")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
        response = client.api.auth.groups[group_name].delete(timeout=(5, 30))
        
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


@groups_router.post(
    "/{group_name}/identities",
    response_model=dict,
    summary="Add identity to group",
    description="Add a TLS or OIDC identity to a group.",
)
async def add_identity_to_group(
    group_name: str,
    request: IdentityAddRequest,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Add an identity to a group by updating the identity's groups list."""
    verify_cluster_access(host)
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for add identity")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
        # Get current identity data
        auth_type = request.auth_type.lower()
        identity_id = request.identity_id
        
        # Navigate using path segments
        get_response = client.api.auth.identities[auth_type][identity_id].get(timeout=(5, 30))
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
        
        # Add group to identity's groups list
        updated_groups = current_groups + [group_name]
        
        # Update identity with new groups list
        update_data = {"groups": updated_groups}
        
        logger.info(f"Adding identity {identity_id} to group {group_name}. Groups: {updated_groups}")
        response = client.api.auth.identities[auth_type][identity_id].patch(json=update_data, timeout=(5, 30))
        
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
            error_detail = response.json().get('error', 'Failed to add identity to group')
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
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Remove an identity from a group by updating the identity's groups list."""
    verify_cluster_access(host)
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for remove identity")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
        # Get current identity data
        auth_type_lower = auth_type.lower()
        
        # Navigate using path segments
        get_response = client.api.auth.identities[auth_type_lower][identity_id].get(timeout=(5, 30))
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
        
        # Remove group from identity's groups list
        updated_groups = [g for g in current_groups if g != group_name]
        
        # Update identity with new groups list
        update_data = {"groups": updated_groups}
        
        logger.info(f"Removing identity {identity_id} from group {group_name}. Groups: {updated_groups}")
        response = client.api.auth.identities[auth_type_lower][identity_id].patch(json=update_data, timeout=(5, 30))
        
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
            error_detail = response.json().get('error', 'Failed to remove identity from group')
            raise HTTPException(status_code=response.status_code, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing identity from group {group_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Delete Identity Endpoint
@identities_router.delete(
    "/{auth_type}/{identity_id}",
    response_model=dict,
    summary="Delete identity",
    description="Delete a TLS or OIDC identity from the LXD server.",
)
async def delete_identity(
    auth_type: str,
    identity_id: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete an identity from the LXD server."""
    verify_cluster_access(host)
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # If client is None, try to reconnect
        if client is None:
            logger.info(f"Reconnecting to {host} for delete identity")
            try:
                client = lxd_service.cert_manager.connect_with_vault_cert(host, connection_manager=lxd_service.connection_manager)
            except Exception as e:
                logger.error(f"Failed to reconnect to {host}: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
            if client is None:
                # the breaker can refuse the reconnect and return None; without this the
                # next line dereferences None and the caller sees a misleading 404/500.
                raise HTTPException(status_code=503, detail=f"Cannot connect to node {host}")
        
        auth_type_lower = auth_type.lower()
        
        # Check if identity exists
        get_response = client.api.auth.identities[auth_type_lower][identity_id].get(timeout=(5, 30))
        if get_response.status_code != 200:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
        
        # Delete the identity
        logger.info(f"Deleting identity {identity_id} (type: {auth_type}) from {host}")
        response = client.api.auth.identities[auth_type_lower][identity_id].delete(timeout=(5, 30))
        
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
            error_detail = response.json().get('error', 'Failed to delete identity')
            raise HTTPException(status_code=response.status_code, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting identity {identity_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
