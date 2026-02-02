"""
API Router for External Integrations.
Provides endpoints for managing API keys, external app connections, and delegated access.
Supports integration with Orcahub and other external applications (soon with Orcalens, Orcasonic, Orcaflux, other Orca product).
"""

import secrets
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Request
from pydantic import BaseModel, Field
import json

from app.services.lxd_service import LXDService, get_lxd_service
from app.core.dependencies import check_read_only_mode
from app.core.cache import get_cache_manager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Integrations"])

# Vault path for integration data
VAULT_INTEGRATIONS_PATH = "integrations"


# ============================================
# Pydantic Models
# ============================================

class ClusterPermission(BaseModel):
    """Permission for a specific cluster."""
    model_config = {"extra": "allow"}
    
    cluster_id: str = Field(..., description="Cluster ID to grant access to")
    cluster_name: Optional[str] = Field(None, description="Cluster name for display")
    permissions: List[str] = Field(
        default=["read"],
        description="Permission levels: read, write, admin"
    )
    projects: List[str] = Field(
        default=["default"],
        description="Allowed projects within the cluster"
    )


class ExternalUserMapping(BaseModel):
    """Maps external user to Orcastra identity."""
    external_user_id: str = Field(..., description="User ID from external system (e.g., Authentik)")
    external_email: str = Field(..., description="User email from external system")
    external_name: Optional[str] = Field(None, description="User display name")
    orcastra_identity_id: Optional[str] = Field(None, description="Mapped Orcastra identity ID")
    permissions: List[ClusterPermission] = Field(
        default_factory=list,
        description="Per-cluster permissions for this user"
    )
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class APIKeyInfo(BaseModel):
    """API Key information returned to clients."""
    id: str = Field(..., description="Unique API key identifier")
    name: str = Field(..., description="Descriptive name for the API key")
    app_name: str = Field(..., description="Name of the connected application")
    app_type: str = Field(default="orcahub", description="Application type: orcahub, custom")
    created_at: str = Field(..., description="Creation timestamp")
    expires_at: Optional[str] = Field(None, description="Expiration timestamp")
    is_active: bool = Field(True, description="Whether the key is active")
    is_revoked: bool = Field(False, description="Whether the key has been revoked")
    last_used_at: Optional[str] = Field(None, description="Last usage timestamp")
    cluster_permissions: List[ClusterPermission] = Field(
        default_factory=list,
        description="Cluster access permissions"
    )
    user_mappings_count: int = Field(0, description="Number of mapped users")


class APIKeyCreateRequest(BaseModel):
    """Request to create a new API key."""
    name: str = Field(..., min_length=1, max_length=100, description="API key name")
    app_name: str = Field(..., min_length=1, max_length=100, description="Application name")
    app_type: str = Field(default="orcahub", description="Application type")
    cluster_permissions: List[ClusterPermission] = Field(
        default_factory=list,
        description="Initial cluster permissions"
    )
    expire_days: Optional[int] = Field(
        None, 
        ge=1, 
        le=3650, 
        description="Expiration in days (None for no expiration)"
    )


class APIKeyCreateResponse(BaseModel):
    """Response after creating an API key - includes the secret (shown only once)."""
    api_key: APIKeyInfo
    api_key_id: str = Field(..., description="The API Key ID (public, can be stored)")
    api_key_secret: str = Field(..., description="The API Key Secret (STORE SECURELY, shown only once)")


class APIKeyListResponse(BaseModel):
    """Response for listing API keys."""
    total: int
    active: int
    revoked: int
    keys: List[APIKeyInfo]


class APIKeyValidateRequest(BaseModel):
    """Request to validate an API key."""
    api_key_id: str = Field(..., description="API Key ID")
    api_key_secret: str = Field(..., description="API Key Secret")


class APIKeyValidateResponse(BaseModel):
    """Response for API key validation."""
    valid: bool
    app_name: Optional[str] = None
    app_type: Optional[str] = None
    cluster_permissions: List[ClusterPermission] = Field(default_factory=list)
    error: Optional[str] = None


class UserMappingRequest(BaseModel):
    """Request to create/update user mapping."""
    external_user_id: str = Field(..., description="External user ID")
    external_email: str = Field(..., description="External user email")
    external_name: Optional[str] = Field(None, description="External user name")
    cluster_permissions: List[ClusterPermission] = Field(
        default_factory=list,
        description="Per-cluster permissions"
    )


class SyncDataRequest(BaseModel):
    """Request for syncing data."""
    include_clusters: bool = Field(True, description="Include cluster list")
    include_instances: bool = Field(False, description="Include instances (can be large)")
    include_projects: bool = Field(True, description="Include projects")
    cluster_ids: Optional[List[str]] = Field(None, description="Specific clusters to sync")


class SyncDataResponse(BaseModel):
    """Response with synced data."""
    clusters: List[Dict[str, Any]] = Field(default_factory=list)
    instances: List[Dict[str, Any]] = Field(default_factory=list)
    projects: List[Dict[str, Any]] = Field(default_factory=list)
    synced_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ProxyRequest(BaseModel):
    """Request to proxy an LXD operation."""
    cluster_id: str = Field(..., description="Target cluster ID")
    operation: str = Field(..., description="Operation type: list_instances, create_instance, etc.")
    project: str = Field(default="default", description="LXD project")
    params: Dict[str, Any] = Field(default_factory=dict, description="Operation parameters")
    external_user_id: Optional[str] = Field(None, description="External user making the request")
    external_user_email: Optional[str] = Field(None, description="External user email")


class RawProxyRequest(BaseModel):
    """Request to proxy a raw HTTP request to LXD."""
    cluster_id: str = Field(..., description="Target cluster ID")
    method: str = Field(..., description="HTTP method: GET, POST, PUT, DELETE, PATCH")
    path: str = Field(..., description="LXD API path, e.g., /1.0/instances")
    body: Optional[Dict[str, Any]] = Field(None, description="Request body for POST/PUT/PATCH")
    project: Optional[str] = Field(None, description="LXD project (added to query params)")
    external_user_id: Optional[str] = Field(None, description="External user making the request")
    external_user_email: Optional[str] = Field(None, description="External user email")


class RawProxyResponse(BaseModel):
    """Response from raw proxy operation."""
    status_code: int = Field(..., description="HTTP status code from LXD")
    data: Optional[Dict[str, Any]] = Field(None, description="Response data from LXD")
    headers: Dict[str, str] = Field(default_factory=dict, description="Response headers")
    error: Optional[str] = Field(None, description="Error message if request failed")


class ProxyResponse(BaseModel):
    """Response from proxied operation."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    operation_id: Optional[str] = None


# ============================================
# Vault Helper Functions
# ============================================

def get_vault_integrations_client():
    """Get Vault client for integrations."""
    settings = get_settings()
    if not settings.vault_enabled or not settings.vault_token:
        raise HTTPException(
            status_code=503,
            detail="Vault is not configured. Enable Vault for integrations."
        )
    return get_vault_client(settings.vault_addr, settings.vault_token)


def generate_api_key_pair() -> tuple[str, str, str]:
    """
    Generate a new API key pair.
    """
    # Generate key ID (public identifier)
    key_id = f"oak_{secrets.token_hex(16)}"  # orcastra api key
    
    # Generate key secret (private, shown only once)
    key_secret = f"oas_{secrets.token_urlsafe(32)}"  # orcastra api secret
    
    # Hash the secret for storage
    secret_hash = hashlib.sha256(key_secret.encode()).hexdigest()
    
    return key_id, key_secret, secret_hash


def verify_api_key_secret(secret: str, stored_hash: str) -> bool:
    """Verify API key secret against stored hash."""
    computed_hash = hashlib.sha256(secret.encode()).hexdigest()
    return secrets.compare_digest(computed_hash, stored_hash)


def save_api_key_to_vault(vault_client, key_id: str, metadata: Dict[str, Any], secret_hash: str):
    """Save API key metadata and hash to Vault."""
    try:
        # Store metadata
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata
        )
        # Store secret hash separately
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret",
            {"hash": secret_hash}
        )
        logger.info(f"Saved API key {key_id} to Vault")
    except Exception as e:
        logger.error(f"Failed to save API key to Vault: {e}")
        raise HTTPException(status_code=500, detail="Failed to save API key")


def get_api_key_from_vault(vault_client, key_id: str) -> tuple[Dict[str, Any], str]:
    """Get API key metadata and secret hash from Vault."""
    try:
        metadata = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
        )
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
        )
        return metadata, secret_data.get("hash", "")
    except Exception as e:
        logger.error(f"Failed to get API key from Vault: {e}")
        raise HTTPException(status_code=404, detail="API key not found")


def list_api_keys_from_vault(vault_client) -> List[Dict[str, Any]]:
    """List all API key metadata from Vault."""
    try:
        # List keys in the api_keys path
        response = vault_client.session.request(
            "LIST",
            f"{vault_client.vault_addr}/v1/secret/metadata/{VAULT_INTEGRATIONS_PATH}/api_keys",
            headers=vault_client.headers,
            timeout=(2, 5)
        )
        
        if response.status_code == 404:
            return []
        
        response.raise_for_status()
        keys = response.json().get("data", {}).get("keys", [])
        
        # Fetch metadata for each key
        all_metadata = []
        for key_id in keys:
            key_id = key_id.rstrip("/")
            try:
                metadata = vault_client.vault_read(
                    f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
                )
                all_metadata.append(metadata)
            except Exception:
                continue
        
        return all_metadata
    except Exception as e:
        logger.warning(f"Failed to list API keys from Vault: {e}")
        return []


def delete_api_key_from_vault(vault_client, key_id: str):
    """Delete API key from Vault."""
    try:
        vault_client.vault_delete(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
        )
        vault_client.vault_delete(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
        )
        logger.info(f"Deleted API key {key_id} from Vault")
    except Exception as e:
        logger.warning(f"Failed to delete API key from Vault: {e}")


def save_user_mapping_to_vault(vault_client, api_key_id: str, user_mapping: ExternalUserMapping):
    """Save user mapping to Vault."""
    try:
        path = f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/users/{user_mapping.external_user_id}"
        vault_client.vault_kv_put(path, user_mapping.model_dump())
        logger.info(f"Saved user mapping {user_mapping.external_user_id} for API key {api_key_id}")
    except Exception as e:
        logger.error(f"Failed to save user mapping to Vault: {e}")
        raise HTTPException(status_code=500, detail="Failed to save user mapping")


def get_user_mappings_from_vault(vault_client, api_key_id: str) -> List[ExternalUserMapping]:
    """Get all user mappings for an API key."""
    try:
        response = vault_client.session.request(
            "LIST",
            f"{vault_client.vault_addr}/v1/secret/metadata/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/users",
            headers=vault_client.headers,
            timeout=(2, 5)
        )
        
        if response.status_code == 404:
            return []
        
        response.raise_for_status()
        user_ids = response.json().get("data", {}).get("keys", [])
        
        mappings = []
        for user_id in user_ids:
            user_id = user_id.rstrip("/")
            try:
                data = vault_client.vault_read(
                    f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/users/{user_id}"
                )
                mappings.append(ExternalUserMapping(**data))
            except Exception:
                continue
        
        return mappings
    except Exception as e:
        logger.warning(f"Failed to list user mappings: {e}")
        return []


def update_api_key_last_used(vault_client, key_id: str):
    """Update the last_used_at timestamp for an API key."""
    try:
        metadata, _ = get_api_key_from_vault(vault_client, key_id)
        metadata["last_used_at"] = datetime.utcnow().isoformat()
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata
        )
    except Exception as e:
        logger.warning(f"Failed to update last_used_at: {e}")


# ============================================
# API Key Validation Dependency
# ============================================

async def validate_api_key(
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
    x_api_key_secret: str = Header(..., alias="X-API-Key-Secret"),
) -> Dict[str, Any]:
    """
    Dependency to validate API key from headers.
    Returns the API key metadata if valid.
    """
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, secret_hash = get_api_key_from_vault(vault_client, x_api_key_id)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Verify secret
    if not verify_api_key_secret(x_api_key_secret, secret_hash):
        raise HTTPException(status_code=401, detail="Invalid API key secret")
    
    # Check if revoked
    if metadata.get("is_revoked", False):
        raise HTTPException(status_code=401, detail="API key has been revoked")
    
    # Check if active
    if not metadata.get("is_active", True):
        raise HTTPException(status_code=401, detail="API key is not active")
    
    # Check expiration
    expires_at = metadata.get("expires_at")
    if expires_at:
        exp_date = datetime.fromisoformat(expires_at)
        if datetime.utcnow() > exp_date:
            raise HTTPException(status_code=401, detail="API key has expired")
    
    # Update last used (async, don't block)
    try:
        update_api_key_last_used(vault_client, x_api_key_id)
    except Exception:
        pass
    
    return metadata


# ============================================
# API Endpoints - Management (Internal)
# ============================================

@router.post(
    "/api-keys",
    response_model=APIKeyCreateResponse,
    summary="Create new API key",
    description="Generate a new API key for external application integration. The secret is shown only once.",
)
async def create_api_key(
    request: APIKeyCreateRequest,
    _: None = Depends(check_read_only_mode),
):
    """Create a new API key for external integration."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Generate key pair
        key_id, key_secret, secret_hash = generate_api_key_pair()
        
        # Calculate expiration
        created_at = datetime.utcnow()
        expires_at = None
        if request.expire_days:
            expires_at = created_at + timedelta(days=request.expire_days)
        
        # Prepare metadata
        metadata = {
            "id": key_id,
            "name": request.name,
            "app_name": request.app_name,
            "app_type": request.app_type,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "is_active": True,
            "is_revoked": False,
            "last_used_at": None,
            "cluster_permissions": [p.model_dump() for p in request.cluster_permissions],
        }
        
        # Save to Vault
        save_api_key_to_vault(vault_client, key_id, metadata, secret_hash)
        
        # Clear cache
        cache = get_cache_manager()
        cache.delete("api_responses", "integrations:api_keys:list")
        
        return APIKeyCreateResponse(
            api_key=APIKeyInfo(**metadata, user_mappings_count=0),
            api_key_id=key_id,
            api_key_secret=key_secret,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/api-keys",
    response_model=APIKeyListResponse,
    summary="List all API keys",
    description="Get a list of all API keys for external integrations.",
)
async def list_api_keys():
    """List all API keys."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Try cache first
        cache = get_cache_manager()
        cache_key = "integrations:api_keys:list"
        cached_data = cache.get("api_responses", cache_key)
        
        if cached_data is None:
            all_metadata = list_api_keys_from_vault(vault_client)
            cache.set("api_responses", cache_key, all_metadata, ttl=60)
        else:
            all_metadata = cached_data
        
        # Process and count
        keys = []
        active_count = 0
        revoked_count = 0
        
        for metadata in all_metadata:
            if metadata.get("is_revoked", False):
                revoked_count += 1
            elif metadata.get("is_active", True):
                active_count += 1
            
            # Get user mapping count
            user_mappings = get_user_mappings_from_vault(vault_client, metadata["id"])
            
            keys.append(APIKeyInfo(
                **metadata,
                user_mappings_count=len(user_mappings)
            ))
        
        return APIKeyListResponse(
            total=len(keys),
            active=active_count,
            revoked=revoked_count,
            keys=keys,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing API keys: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/api-keys/{key_id}",
    response_model=APIKeyInfo,
    summary="Get API key details",
    description="Get details of a specific API key.",
)
async def get_api_key(key_id: str):
    """Get API key details."""
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, _ = get_api_key_from_vault(vault_client, key_id)
        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        
        return APIKeyInfo(**metadata, user_mappings_count=len(user_mappings))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/api-keys/{key_id}/permissions",
    response_model=APIKeyInfo,
    summary="Update API key permissions",
    description="Update cluster permissions for an API key.",
)
async def update_api_key_permissions(
    key_id: str,
    permissions: List[ClusterPermission],
    _: None = Depends(check_read_only_mode),
):
    """Update API key cluster permissions."""
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, secret_hash = get_api_key_from_vault(vault_client, key_id)
        
        # Update permissions
        metadata["cluster_permissions"] = [p.model_dump() for p in permissions]
        
        # Save updated metadata
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata
        )
        
        # Clear cache
        cache = get_cache_manager()
        cache.delete("api_responses", "integrations:api_keys:list")
        
        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        return APIKeyInfo(**metadata, user_mappings_count=len(user_mappings))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key permissions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/api-keys/{key_id}/revoke",
    response_model=APIKeyInfo,
    summary="Revoke API key",
    description="Immediately revoke an API key. This action cannot be undone.",
)
async def revoke_api_key(
    key_id: str,
    _: None = Depends(check_read_only_mode),
):
    """Revoke an API key (immediate revocation)."""
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, _ = get_api_key_from_vault(vault_client, key_id)
        
        # Mark as revoked
        metadata["is_revoked"] = True
        metadata["is_active"] = False
        metadata["revoked_at"] = datetime.utcnow().isoformat()
        
        # Save updated metadata
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata
        )
        
        # Clear cache immediately for real-time revocation
        cache = get_cache_manager()
        cache.delete("api_responses", "integrations:api_keys:list")
        cache.delete("api_responses", f"integrations:api_key:{key_id}")
        
        logger.info(f"API key {key_id} revoked")
        
        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        return APIKeyInfo(**metadata, user_mappings_count=len(user_mappings))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/api-keys/{key_id}",
    summary="Delete API key",
    description="Permanently delete an API key and all associated user mappings.",
)
async def delete_api_key(
    key_id: str,
    _: None = Depends(check_read_only_mode),
):
    """Delete an API key permanently."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify it exists
        get_api_key_from_vault(vault_client, key_id)
        
        # Delete from Vault
        delete_api_key_from_vault(vault_client, key_id)
        
        # Clear cache
        cache = get_cache_manager()
        cache.delete("api_responses", "integrations:api_keys:list")
        
        return {"success": True, "message": f"API key {key_id} deleted"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# User Mapping Endpoints
# ============================================

@router.post(
    "/api-keys/{key_id}/users",
    response_model=ExternalUserMapping,
    summary="Add user mapping",
    description="Map an external user to Orcastra with specific permissions.",
)
async def add_user_mapping(
    key_id: str,
    request: UserMappingRequest,
    _: None = Depends(check_read_only_mode),
):
    """Add or update a user mapping for an API key."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify API key exists
        get_api_key_from_vault(vault_client, key_id)
        
        # Create user mapping
        now = datetime.utcnow().isoformat()
        user_mapping = ExternalUserMapping(
            external_user_id=request.external_user_id,
            external_email=request.external_email,
            external_name=request.external_name,
            permissions=request.cluster_permissions,
            created_at=now,
            updated_at=now,
        )
        
        # Save to Vault
        save_user_mapping_to_vault(vault_client, key_id, user_mapping)
        
        return user_mapping
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding user mapping: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/api-keys/{key_id}/users",
    response_model=List[ExternalUserMapping],
    summary="List user mappings",
    description="Get all user mappings for an API key.",
)
async def list_user_mappings(key_id: str):
    """List all user mappings for an API key."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify API key exists
        get_api_key_from_vault(vault_client, key_id)
        
        # Get mappings
        return get_user_mappings_from_vault(vault_client, key_id)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing user mappings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/api-keys/{key_id}/users/{user_id}",
    summary="Remove user mapping",
    description="Remove a user mapping from an API key.",
)
async def delete_user_mapping(
    key_id: str,
    user_id: str,
    _: None = Depends(check_read_only_mode),
):
    """Delete a user mapping."""
    vault_client = get_vault_integrations_client()
    
    try:
        path = f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/users/{user_id}"
        vault_client.vault_delete(path)
        
        return {"success": True, "message": f"User mapping {user_id} deleted"}
        
    except Exception as e:
        logger.error(f"Error deleting user mapping: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# External API Endpoints (for Orcahub/External Apps)
# ============================================

@router.post(
    "/validate",
    response_model=APIKeyValidateResponse,
    summary="Validate API key",
    description="Validate an API key and return its permissions. Used by external apps.",
)
async def validate_api_key_endpoint(request: APIKeyValidateRequest):
    """Validate an API key (public endpoint for external apps)."""
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, secret_hash = get_api_key_from_vault(vault_client, request.api_key_id)
        
        # Verify secret
        if not verify_api_key_secret(request.api_key_secret, secret_hash):
            return APIKeyValidateResponse(valid=False, error="Invalid secret")
        
        # Check if revoked
        if metadata.get("is_revoked", False):
            return APIKeyValidateResponse(valid=False, error="API key revoked")
        
        # Check if active
        if not metadata.get("is_active", True):
            return APIKeyValidateResponse(valid=False, error="API key not active")
        
        # Check expiration
        expires_at = metadata.get("expires_at")
        if expires_at:
            exp_date = datetime.fromisoformat(expires_at)
            if datetime.utcnow() > exp_date:
                return APIKeyValidateResponse(valid=False, error="API key expired")
        
        # Valid!
        raw_permissions = metadata.get("cluster_permissions", [])
        
        # Enrich cluster permissions with cluster names from Vault
        enriched_permissions = []
        settings = get_settings()
        
        # Build cluster name lookup
        cluster_names = {}
        if settings.vault_enabled and settings.vault_token:
            try:
                from app.services.lxd_cert_manager import LXDCertificateManager
                vault = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault,
                    clusters_path=settings.vault_clusters_path
                )
                
                for perm in raw_permissions:
                    cluster_id = perm.get("cluster_id")
                    if cluster_id and cluster_id not in cluster_names:
                        try:
                            cluster_info = cert_manager.get_cluster_info(cluster_id)
                            meta = cluster_info.get("metadata", {})
                            cluster_names[cluster_id] = meta.get("display_name") or meta.get("name") or cluster_id
                        except Exception:
                            cluster_names[cluster_id] = cluster_id
            except Exception as e:
                logger.warning(f"Could not enrich cluster names: {e}")
        
        # Build enriched permissions with cluster names
        for perm in raw_permissions:
            cluster_id = perm.get("cluster_id")
            enriched_perm = dict(perm)
            enriched_perm["cluster_name"] = cluster_names.get(cluster_id, cluster_id)
            enriched_permissions.append(ClusterPermission(**enriched_perm))
        
        return APIKeyValidateResponse(
            valid=True,
            app_name=metadata.get("app_name"),
            app_type=metadata.get("app_type"),
            cluster_permissions=enriched_permissions,
        )
        
    except HTTPException:
        return APIKeyValidateResponse(valid=False, error="API key not found")
    except Exception as e:
        logger.error(f"Error validating API key: {e}")
        return APIKeyValidateResponse(valid=False, error="Validation failed")


@router.post(
    "/sync",
    response_model=SyncDataResponse,
    summary="Sync data",
    description="Get synchronized data from Orcastra. Requires valid API key.",
)
async def sync_data(
    request: SyncDataRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Sync data to external application."""
    try:
        result = SyncDataResponse()
        
        # Get allowed cluster IDs from API key permissions
        allowed_clusters = {
            p["cluster_id"] for p in api_key.get("cluster_permissions", [])
        }
        
        # Filter requested clusters
        requested_clusters = set(request.cluster_ids) if request.cluster_ids else allowed_clusters
        cluster_ids = allowed_clusters.intersection(requested_clusters)
        
        if request.include_clusters:
            # Get cluster info
            settings = get_settings()
            if settings.vault_enabled and settings.vault_token:
                from app.services.lxd_cert_manager import LXDCertificateManager
                vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault_client,
                    clusters_path=settings.vault_clusters_path
                )
                
                for cluster_id in cluster_ids:
                    try:
                        cluster_info = cert_manager.get_cluster_info(cluster_id)
                        # Return only non-sensitive info
                        result.clusters.append({
                            "id": cluster_id,
                            "name": cluster_info.get("metadata", {}).get("name", cluster_id),
                            "endpoint": cluster_info.get("metadata", {}).get("endpoint"),
                            "server_type": cluster_info.get("metadata", {}).get("server_type", "lxd"),
                            "registered_at": cluster_info.get("metadata", {}).get("registered_at"),
                        })
                    except Exception as e:
                        logger.warning(f"Could not get cluster info for {cluster_id}: {e}")
        
        if request.include_projects:
            # Get projects from allowed clusters
            for cluster_id in cluster_ids:
                try:
                    client = lxd_service.connection_manager.get_client(cluster_id)
                    if client:
                        for project in client.projects.all():
                            result.projects.append({
                                "name": project.name,
                                "cluster_id": cluster_id,
                                "description": project.description,
                            })
                except Exception as e:
                    logger.warning(f"Could not get projects for {cluster_id}: {e}")
        
        if request.include_instances:
            # Get instances from allowed clusters
            for cluster_id in cluster_ids:
                try:
                    client = lxd_service.connection_manager.get_client(cluster_id)
                    if client:
                        for instance in client.instances.all():
                            result.instances.append({
                                "name": instance.name,
                                "cluster_id": cluster_id,
                                "status": instance.status,
                                "type": instance.type,
                            })
                except Exception as e:
                    logger.warning(f"Could not get instances for {cluster_id}: {e}")
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/raw-proxy",
    response_model=RawProxyResponse,
    summary="Raw HTTP proxy to LXD",
    description="Execute a raw HTTP request through Orcastra to LXD API. More flexible than the operation-based proxy.",
)
async def raw_proxy(
    request: RawProxyRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Raw HTTP proxy for external applications to access LXD API."""
    try:
        # Check cluster permission
        cluster_permissions = {
            p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])
        }
        
        if request.cluster_id not in cluster_permissions:
            raise HTTPException(
                status_code=403,
                detail=f"No permission for cluster {request.cluster_id}"
            )
        
        perm = cluster_permissions[request.cluster_id]
        
        # Check project permission if specified
        project = request.project or "default"
        allowed_projects = perm.get("projects", ["default"])
        if project not in allowed_projects and "*" not in allowed_projects:
            raise HTTPException(
                status_code=403,
                detail=f"No permission for project {project}"
            )
        
        # Determine required permission based on method
        method = request.method.upper()
        required_permission = "read"
        
        if method in ["POST", "PUT", "PATCH"]:
            required_permission = "write"
        elif method == "DELETE":
            required_permission = "admin"
        
        allowed_permissions = perm.get("permissions", ["read"])
        permission_hierarchy = {"read": 0, "write": 1, "admin": 2}
        
        max_permission = max(
            permission_hierarchy.get(p, 0) for p in allowed_permissions
        )
        required_level = permission_hierarchy.get(required_permission, 0)
        
        if max_permission < required_level:
            raise HTTPException(
                status_code=403,
                detail=f"Operation requires {required_permission} permission"
            )
        
        # Get LXD client for the cluster
        client = lxd_service.connection_manager.get_client(request.cluster_id)
        if not client:
            raise HTTPException(
                status_code=503,
                detail=f"Cluster {request.cluster_id} is not connected"
            )
        
        # Use pylxd client's API directly
        # The path should be relative to /1.0, ex;, "/1.0/instances" or just "/instances"
        path = request.path
        
        # Normalize path - remove leading /1.0 if present since client.api is already at /1.0
        if path.startswith("/1.0"):
            path = path[4:]  # Remove "/1.0"
        if not path.startswith("/"):
            path = "/" + path
        
        # Parse query parameters from path
        base_path = path
        query_params = {}
        if "?" in path:
            base_path, query_string = path.split("?", 1)
            for param in query_string.split("&"):
                if "=" in param:
                    key, value = param.split("=", 1)
                    query_params[key] = value
        
        # Add project parameter if not default
        if project and project != "default":
            query_params["project"] = project
        
        # Build endpoint URL directly using client's session
        endpoint = client.api._api_endpoint
        
        # Reconstruct full URL
        full_path = base_path
        if query_params:
            full_path += "?" + "&".join(f"{k}={v}" for k, v in query_params.items())
        
        full_url = f"{endpoint}{full_path}"
        
        # Make the request using client's session (which has certs configured)
        try:
            session = client.api.session
            
            if method == "GET":
                response = session.get(full_url, verify=False)
            elif method == "POST":
                response = session.post(full_url, json=request.body, verify=False)
            elif method == "PUT":
                response = session.put(full_url, json=request.body, verify=False)
            elif method == "PATCH":
                response = session.patch(full_url, json=request.body, verify=False)
            elif method == "DELETE":
                response = session.delete(full_url, verify=False)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")
            
            # Parse response
            status_code = response.status_code
            try:
                data = response.json()
            except:
                data = {"raw": response.text if hasattr(response, 'text') else str(response)}
            
            # Log the operation for audit
            logger.info(
                f"Raw proxy: {method} {request.path} on {request.cluster_id}/{project} "
                f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')} "
                f"-> {status_code}"
            )
            
            return RawProxyResponse(
                status_code=status_code,
                data=data,
                headers={},
            )
            
        except Exception as api_error:
            logger.error(f"LXD API error: {api_error}")
            return RawProxyResponse(
                status_code=500,
                data=None,
                error=str(api_error),
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in raw proxy: {e}", exc_info=True)
        return RawProxyResponse(
            status_code=500,
            data=None,
            error=str(e),
        )


@router.post(
    "/proxy",
    response_model=ProxyResponse,
    summary="Proxy LXD operation",
    description="Execute an LXD operation through Orcastra proxy. Requires valid API key with appropriate permissions.",
)
async def proxy_operation(
    request: ProxyRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Proxy an LXD operation for external application."""
    try:
        # Check cluster permission
        cluster_permissions = {
            p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])
        }
        
        if request.cluster_id not in cluster_permissions:
            raise HTTPException(
                status_code=403,
                detail=f"No permission for cluster {request.cluster_id}"
            )
        
        perm = cluster_permissions[request.cluster_id]
        
        # Check project permission
        allowed_projects = perm.get("projects", ["default"])
        if request.project not in allowed_projects and "*" not in allowed_projects:
            raise HTTPException(
                status_code=403,
                detail=f"No permission for project {request.project}"
            )
        
        # Check operation permission
        operation = request.operation
        required_permission = "read"
        
        write_operations = [
            "create_instance", "delete_instance", "start_instance", 
            "stop_instance", "restart_instance", "update_instance"
        ]
        admin_operations = [
            "create_network", "delete_network", "create_storage_pool",
            "delete_storage_pool", "create_project", "delete_project"
        ]
        
        if operation in write_operations:
            required_permission = "write"
        elif operation in admin_operations:
            required_permission = "admin"
        
        allowed_permissions = perm.get("permissions", ["read"])
        if required_permission not in allowed_permissions:
            raise HTTPException(
                status_code=403,
                detail=f"Operation requires {required_permission} permission"
            )
        
        # Get LXD client for cluster
        client = lxd_service.connection_manager.get_client(request.cluster_id)
        if not client:
            raise HTTPException(
                status_code=503,
                detail=f"Cluster {request.cluster_id} is not connected"
            )
        
        # Execute operation
        result_data = None
        operation_id = None
        
        if operation == "list_instances":
            instances = client.instances.all(project=request.project)
            result_data = [
                {
                    "name": i.name,
                    "status": i.status,
                    "type": i.type,
                    "architecture": i.architecture,
                }
                for i in instances
            ]
        
        elif operation == "get_instance":
            name = request.params.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Instance name required")
            instance = client.instances.get(name, project=request.project)
            result_data = {
                "name": instance.name,
                "status": instance.status,
                "type": instance.type,
                "architecture": instance.architecture,
                "config": dict(instance.config),
            }
        
        elif operation == "create_instance":
            config = request.params
            instance = client.instances.create(config, project=request.project, wait=True)
            result_data = {"name": instance.name, "status": instance.status}
        
        elif operation == "delete_instance":
            name = request.params.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Instance name required")
            instance = client.instances.get(name, project=request.project)
            instance.delete(wait=True)
            result_data = {"deleted": True}
        
        elif operation == "start_instance":
            name = request.params.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Instance name required")
            instance = client.instances.get(name, project=request.project)
            instance.start(wait=True)
            result_data = {"status": "Running"}
        
        elif operation == "stop_instance":
            name = request.params.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Instance name required")
            instance = client.instances.get(name, project=request.project)
            force = request.params.get("force", False)
            instance.stop(force=force, wait=True)
            result_data = {"status": "Stopped"}
        
        elif operation == "restart_instance":
            name = request.params.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Instance name required")
            instance = client.instances.get(name, project=request.project)
            force = request.params.get("force", False)
            instance.restart(force=force, wait=True)
            result_data = {"status": "Running"}
        
        elif operation == "list_projects":
            projects = client.projects.all()
            result_data = [
                {"name": p.name, "description": p.description}
                for p in projects
            ]
        
        elif operation == "list_images":
            images = client.images.all()
            result_data = [
                {
                    "fingerprint": img.fingerprint[:12],
                    "description": img.properties.get("description", ""),
                    "size": img.size,
                }
                for img in images
            ]
        
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown operation: {operation}"
            )
        
        # Log the operation for audit
        logger.info(
            f"Proxy operation: {operation} on {request.cluster_id}/{request.project} "
            f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')}"
        )
        
        return ProxyResponse(
            success=True,
            data=result_data,
            operation_id=operation_id,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in proxy operation: {e}", exc_info=True)
        return ProxyResponse(
            success=False,
            error=str(e),
        )


# ============================================
# Available Clusters Endpoint
# ============================================

@router.get(
    "/available-clusters",
    summary="List available clusters",
    description="Get list of clusters that can be assigned to API keys.",
)
async def list_available_clusters():
    """List all available clusters for integration."""
    try:
        settings = get_settings()
        clusters = []
        
        if settings.vault_enabled and settings.vault_token:
            from app.services.lxd_cert_manager import LXDCertificateManager
            vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
            cert_manager = LXDCertificateManager(
                vault_client=vault_client,
                clusters_path=settings.vault_clusters_path
            )
            
            cluster_ids = cert_manager.list_clusters()
            
            for cluster_id in cluster_ids:
                try:
                    cluster_info = cert_manager.get_cluster_info(cluster_id)
                    clusters.append({
                        "id": cluster_id,
                        "name": cluster_info.get("metadata", {}).get("name", cluster_id),
                        "endpoint": cluster_info.get("metadata", {}).get("endpoint"),
                        "server_type": cluster_info.get("metadata", {}).get("server_type", "lxd"),
                    })
                except Exception as e:
                    logger.warning(f"Could not get cluster info for {cluster_id}: {e}")
        
        return {"clusters": clusters}
        
    except Exception as e:
        logger.error(f"Error listing available clusters: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
