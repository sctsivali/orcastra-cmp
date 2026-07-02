"""
API Router for External Integrations.
Provides endpoints for managing API keys, external app connections, and delegated access.
Supports integration with Orcahub and other external applications (soon with Orcalens, Orcasonic, Orcaflux, other Orca product).
"""

import secrets
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Request
from pydantic import BaseModel, Field
import json

from app.services.lxd_service import LXDService, get_lxd_service
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, get_current_allowed_clusters, reconcile_personal_clusters
from app.core.cache import get_cache_manager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete
from app.models.db_models import Organization, OrganizationMember

logger = logging.getLogger(__name__)

# Internal router: JWT-authenticated endpoints for API key management (admin UI)
router = APIRouter(
    prefix="/integrations",
    tags=["Integrations"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# External router: API key-authenticated endpoints for external apps (OrcaHub, etc.)
# No JWT dependency - these endpoints authenticate via X-API-Key-ID/Secret headers or request body
external_router = APIRouter(
    prefix="/integrations",
    tags=["Integrations - External"],
)

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
    external_user_id: str = Field(..., description="User ID from external system (ex;, Authentik)")
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


class SyncOrganization(BaseModel):
    """Organization data pushed from external app during sync."""
    id: str = Field(..., description="External organization UUID")
    name: str = Field(..., description="Organization name")
    slug: str = Field(..., description="Organization slug")
    description: Optional[str] = Field(None, description="Organization description")


class SyncMember(BaseModel):
    """Member data pushed from external app during sync."""
    email: str = Field(..., description="User email")
    name: Optional[str] = Field(None, description="User display name")
    organization_id: str = Field(..., description="External org UUID this member belongs to")
    role: str = Field(..., description="Role in org: PARTNER, TENANT, or USER")
    action: str = Field(default="upsert", description="upsert or remove")


class SyncDataRequest(BaseModel):
    """Request for syncing data."""
    include_clusters: bool = Field(True, description="Include cluster list")
    include_instances: bool = Field(False, description="Include instances (can be large)")
    include_projects: bool = Field(True, description="Include projects")
    cluster_ids: Optional[List[str]] = Field(None, description="Specific clusters to sync")
    # Organization data pushed from external app (ex; OrcaHub)
    organizations: List[SyncOrganization] = Field(
        default_factory=list,
        description="Organizations to sync from external app"
    )
    # Callback URL for on-demand data pull
    app_url: Optional[str] = Field(None, description="External app base URL for callbacks")
    # Member data pushed from external app (ex; OrcaHub)
    members: List[SyncMember] = Field(
        default_factory=list,
        description="Organization members to sync from external app"
    )


class SyncDataResponse(BaseModel):
    """Response with synced data."""
    clusters: List[Dict[str, Any]] = Field(default_factory=list)
    instances: List[Dict[str, Any]] = Field(default_factory=list)
    projects: List[Dict[str, Any]] = Field(default_factory=list)
    synced_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    organizations_synced: int = Field(default=0, description="Number of organizations synced")
    members_synced: int = Field(default=0, description="Number of members synced")
    members_removed: int = Field(default=0, description="Number of members removed")


def _public_location_data(location_data: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = {
        "region_id",
        "region_name",
        "city",
        "country_code",
        "country_name",
        "latitude",
        "longitude",
        "continent",
        "timezone",
        "facility_name",
        "facility_provider",
    }
    return {key: location_data.get(key) for key in allowed_keys if location_data.get(key) is not None}


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
    path: str = Field(..., description="LXD API path, ex;, /1.0/instances")
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


def _get_hmac_key() -> bytes:
    """Get HMAC key from SECRET_KEY for API key hashing."""
    import os
    key = os.environ.get('SECRET_KEY', '')
    if not key:
        raise HTTPException(
            status_code=500,
            detail="SECRET_KEY not configured - required for API key operations"
        )
    return key.encode()


def generate_api_key_pair() -> tuple[str, str, str]:
    """
    Generate a new API key pair.
    Uses HMAC-SHA256 with SECRET_KEY for secure hashing.
    """
    # Generate key ID (public identifier)
    key_id = f"oak_{secrets.token_hex(16)}"  # orcastra api key
    
    # Generate key secret (private, shown only once)
    key_secret = f"oas_{secrets.token_urlsafe(32)}"  # orcastra api secret
    
    # Hash the secret using HMAC-SHA256 (keyed hash prevents rainbow table attacks)
    secret_hash = hmac.new(_get_hmac_key(), key_secret.encode(), hashlib.sha256).hexdigest()
    
    return key_id, key_secret, secret_hash


def verify_api_key_secret(secret: str, stored_hash: str) -> bool:
    """Verify API key secret against stored hash using HMAC-SHA256."""
    computed_hash = hmac.new(_get_hmac_key(), secret.encode(), hashlib.sha256).hexdigest()
    return secrets.compare_digest(computed_hash, stored_hash)


def save_api_key_to_vault(
    vault_client, key_id: str, metadata: Dict[str, Any], secret_hash: str,
    raw_secret: Optional[str] = None,
):
    """Save API key metadata, hash, and optionally raw secret to Vault."""
    try:
        # Store metadata
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata
        )
        # Store secret hash (+ raw secret for callback auth)
        secret_payload: Dict[str, Any] = {"hash": secret_hash}
        if raw_secret:
            secret_payload["raw"] = raw_secret
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret",
            secret_payload
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


def _store_app_url_in_vault(api_key_id: str, app_url: str):
    """Store external app callback URL in Vault alongside API key metadata."""
    try:
        vault_client = get_vault_integrations_client()
        metadata, _ = get_api_key_from_vault(vault_client, api_key_id)
        metadata["app_url"] = app_url
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/metadata",
            metadata
        )
        logger.info(f"Stored app_url for API key {api_key_id}")
    except Exception as e:
        logger.warning(f"Failed to store app_url in Vault: {e}")


async def _upsert_synced_organizations(
    db: AsyncSession,
    organizations: List["SyncOrganization"],
    api_key_id: str,
) -> int:
    """
    Upsert organizations pushed from external app into PostgreSQL.
    Uses (source, external_id) as dedup key.
    Returns count of synced organizations.
    """
    synced = 0
    for org_data in organizations:
        try:
            # check if org already exists by source + external_id
            stmt = select(Organization).where(
                and_(
                    Organization.source == "orcahub",
                    Organization.external_id == org_data.id,
                )
            )
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # update mutable fields
                existing.name = org_data.name
                existing.slug = org_data.slug
                existing.description = org_data.description
                existing.api_key_id = api_key_id
                existing.is_active = True
            else:
                # create new synced org
                new_org = Organization(
                    name=org_data.name,
                    slug=org_data.slug,
                    description=org_data.description,
                    created_by="system",
                    source="orcahub",
                    external_id=org_data.id,
                    api_key_id=api_key_id,
                    is_active=True,
                )
                db.add(new_org)

            synced += 1
        except Exception as e:
            logger.error(f"Failed to upsert organization {org_data.id}: {e}")

    if synced > 0:
        await db.commit()
        logger.info(f"Synced {synced} organizations from OrcaHub (api_key={api_key_id})")

    return synced


# role mapping: OrcaHub OrganizationRole → Orcastra system role (Authentik group)
_ORCAHUB_ROLE_MAP = {
    "PARTNER": "partner",
    "TENANT": "tenant",
    "USER": "tenant",  # legacy alias
}


async def _upsert_synced_members(
    db: AsyncSession,
    members: List["SyncMember"],
    api_key_id: str,
) -> tuple[int, int]:
    """
    Upsert organization members pushed from external app.
    Maps external org UUID → local org id via (source=orcahub, external_id).
    Pushes role changes to Authentik.
    Returns (synced_count, removed_count).
    """
    from app.services.authentik_sync import get_authentik_sync

    synced = 0
    removed = 0
    sync_svc = get_authentik_sync()
    upserted_emails: set[str] = set()

    for m in members:
        try:
            # resolve external org UUID to local org id
            stmt = select(Organization).where(
                and_(
                    Organization.source == "orcahub",
                    Organization.external_id == m.organization_id,
                )
            )
            result = await db.execute(stmt)
            org = result.scalar_one_or_none()
            if not org:
                logger.warning(f"Org not found for external_id={m.organization_id}, skipping member {m.email}")
                continue

            if m.action == "remove":
                # remove membership
                del_stmt = select(OrganizationMember).where(
                    and_(
                        OrganizationMember.organization_id == org.id,
                        OrganizationMember.user_email == m.email,
                    )
                )
                del_result = await db.execute(del_stmt)
                existing = del_result.scalar_one_or_none()
                if existing:
                    await db.delete(existing)
                    removed += 1
                    # remove from Authentik roles
                    try:
                        await sync_svc.remove_user_from_all_roles(m.email)
                    except Exception as e:
                        logger.warning(f"Authentik role removal failed for {m.email}: {e}")
                continue

            # upsert membership
            # map OrcaHub role → local member role
            orcastra_role = _ORCAHUB_ROLE_MAP.get(m.role.upper(), "tenant")

            stmt2 = select(OrganizationMember).where(
                and_(
                    OrganizationMember.organization_id == org.id,
                    OrganizationMember.user_email == m.email,
                )
            )
            result2 = await db.execute(stmt2)
            existing = result2.scalar_one_or_none()

            if existing:
                existing.role = orcastra_role
            else:
                db.add(OrganizationMember(
                    organization_id=org.id,
                    user_email=m.email,
                    role=orcastra_role,
                    invited_by="orcahub-sync",
                ))

            synced += 1
            upserted_emails.add(m.email)

            # push system role to Authentik
            try:
                await sync_svc.sync_user_role(m.email, orcastra_role)
            except Exception as e:
                logger.warning(f"Authentik sync failed for {m.email} → {orcastra_role}: {e}")

        except Exception as e:
            logger.error(f"Failed to sync member {m.email}: {e}")

    if synced > 0 or removed > 0:
        await db.commit()
        logger.info(
            f"Member sync: {synced} upserted, {removed} removed (api_key={api_key_id})"
        )

    # Backstop reconcile: now that these members are linked to their org, bind any
    # clusters they registered while org-less. Best-effort, idempotent, per member.
    for email in upserted_emails:
        try:
            await reconcile_personal_clusters(db, email)
        except Exception as e:
            logger.warning(f"reconcile_personal_clusters failed for {email}: {e}")

    return synced, removed


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
    
    # Backfill raw secret if not already stored (for callback auth)
    try:
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{x_api_key_id}/secret"
        )
        if not secret_data.get("raw"):
            secret_data["raw"] = x_api_key_secret
            vault_client.vault_kv_put(
                f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{x_api_key_id}/secret",
                secret_data,
            )
            logger.info(f"Backfilled raw secret for API key {x_api_key_id}")
    except Exception:
        pass  # non-critical
    
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create a new API key for external integration."""
    vault_client = get_vault_integrations_client()

    # Multi-tenancy guard: a partner may only grant access to clusters they can see
    # (their org's or personally registered). Admin (allowed is None) is unrestricted.
    # Reuses the request-scoped set already resolved by org_scope_dependency - no extra
    # query - and shares the source the cluster picker uses, so it never rejects an
    # option the UI offered.
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        requested = {p.cluster_id for p in request.cluster_permissions}
        invalid = requested - allowed
        if invalid:
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted to grant access to clusters: {sorted(invalid)}",
            )

    try:
        # Generate key pair
        key_id, key_secret, secret_hash = generate_api_key_pair()
        
        # Calculate expiration
        created_at = datetime.utcnow()
        expires_at = None
        if request.expire_days:
            expires_at = created_at + timedelta(days=request.expire_days)
        
        # Prepare metadata (include creator for org-level ownership)
        metadata = {
            "id": key_id,
            "name": request.name,
            "app_name": request.app_name,
            "app_type": request.app_type,
            "created_by": _rbac.email,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "is_active": True,
            "is_revoked": False,
            "last_used_at": None,
            "cluster_permissions": [p.model_dump() for p in request.cluster_permissions],
        }
        
        # Save to Vault (include raw secret for callback authentication)
        save_api_key_to_vault(vault_client, key_id, metadata, secret_hash, raw_secret=key_secret)
        
        # Clear all scoped cache variants
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")

        # If this partner already has an org, bind any clusters they registered while
        # org-less to it now. Best-effort: a failure here must not fail key creation.
        try:
            await reconcile_personal_clusters(db, _rbac.email)
        except Exception as e:
            logger.warning(f"reconcile_personal_clusters failed for {_rbac.email}: {e}")

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
    """List API keys visible to the current user (admin sees all, others see org-scoped)."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Scoped cache key so each org gets its own cached list
        cache = get_cache_manager()
        cache_key = get_scope_cache_key("integrations:api_keys:list")
        cached_data = cache.get("api_responses", cache_key)
        
        if cached_data is None:
            all_metadata = list_api_keys_from_vault(vault_client)
            
            # Filter by org scope: non-admin users only see keys
            # whose cluster_permissions overlap with their allowed clusters
            allowed = get_current_allowed_clusters()
            if allowed is not None:
                filtered = []
                for meta in all_metadata:
                    perms = meta.get("cluster_permissions", [])
                    # show key if ANY of its permitted clusters is in the user's set
                    key_clusters = {p.get("cluster_id", "") for p in perms if isinstance(p, dict)}
                    if key_clusters & allowed:
                        filtered.append(meta)
                all_metadata = filtered
            
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
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
        
        # Clear all scoped cache variants
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
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
        
        # Clear all scoped cache variants for real-time revocation
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete an API key permanently."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify it exists
        get_api_key_from_vault(vault_client, key_id)
        
        # Delete from Vault
        delete_api_key_from_vault(vault_client, key_id)
        
        # Clear all scoped cache variants
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
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
    _rbac: AuthenticatedUser = Depends(require_admin()),
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
# These use API key auth (headers or body), NOT JWT auth
# ============================================

@external_router.post(
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


@external_router.post(
    "/sync",
    response_model=SyncDataResponse,
    summary="Sync data",
    description="Get synchronized data from Orcastra. Requires valid API key.",
)
async def sync_data(
    request: SyncDataRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
):
    """Sync data to external application."""
    try:
        result = SyncDataResponse()
        api_key_id = api_key.get("id", "")

        # ── Sync organizations from external app ──
        if request.organizations:
            orgs_synced = await _upsert_synced_organizations(
                db, request.organizations, api_key_id
            )
            result.organizations_synced = orgs_synced

        # ── Sync members from external app ──
        if request.members:
            members_synced, members_removed = await _upsert_synced_members(
                db, request.members, api_key_id
            )
            result.members_synced = members_synced
            result.members_removed = members_removed

        # ── Store callback URL in Vault API key metadata ──
        if request.app_url:
            _store_app_url_in_vault(api_key_id, request.app_url)
        
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
                        location_data = cluster_info.get("metadata", {}).get("location", {})
                        public_location = _public_location_data(location_data)
                        zone_name = location_data.get("region_name") or location_data.get("city") or None
                        result.clusters.append({
                            "id": cluster_id,
                            "name": cluster_info.get("metadata", {}).get("name", cluster_id),
                            "endpoint": cluster_info.get("metadata", {}).get("endpoint"),
                            "server_type": cluster_info.get("metadata", {}).get("server_type", "lxd"),
                            "registered_at": cluster_info.get("metadata", {}).get("registered_at"),
                            "zone": zone_name,
                            "location_data": public_location or None,
                            "region_id": public_location.get("region_id"),
                            "region_name": public_location.get("region_name"),
                            "city": public_location.get("city"),
                            "country_code": public_location.get("country_code"),
                            "country_name": public_location.get("country_name"),
                            "latitude": public_location.get("latitude"),
                            "longitude": public_location.get("longitude"),
                            "facility_name": public_location.get("facility_name"),
                            "facility_provider": public_location.get("facility_provider"),
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


@external_router.get(
    "/regions",
    summary="Get integration region coordinates",
    description="Return non-sensitive geographic region coordinates for clusters allowed by the API key.",
)
async def get_integration_regions(api_key: Dict[str, Any] = Depends(validate_api_key)):
    """Expose real cluster location metadata to external integrations using API-key auth."""
    try:
        settings = get_settings()
        allowed_clusters = {
            permission["cluster_id"] for permission in api_key.get("cluster_permissions", [])
        }
        regions: Dict[str, Dict[str, Any]] = {}

        if settings.vault_enabled and settings.vault_token:
            from app.services.lxd_cert_manager import LXDCertificateManager
            vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
            cert_manager = LXDCertificateManager(
                vault_client=vault_client,
                clusters_path=settings.vault_clusters_path
            )

            for cluster_id in allowed_clusters:
                try:
                    cluster_info = cert_manager.get_cluster_info(cluster_id)
                    location_data = _public_location_data(
                        cluster_info.get("metadata", {}).get("location", {})
                    )
                    if not location_data.get("latitude") or not location_data.get("longitude"):
                        continue

                    region_id = location_data.get("region_id") or cluster_id
                    if region_id not in regions:
                        regions[region_id] = {
                            "region_id": region_id,
                            "region_name": location_data.get("region_name") or location_data.get("city") or cluster_id,
                            "country_code": location_data.get("country_code") or "XX",
                            "country_name": location_data.get("country_name") or "Unknown",
                            "city": location_data.get("city") or location_data.get("region_name") or "Unknown",
                            "latitude": location_data.get("latitude"),
                            "longitude": location_data.get("longitude"),
                            "continent": location_data.get("continent") or "XX",
                            "facility_name": location_data.get("facility_name"),
                            "facility_provider": location_data.get("facility_provider"),
                            "node_count": 0,
                            "instance_count": 0,
                            "instance_running": 0,
                        }
                    regions[region_id]["node_count"] += 1
                except Exception as e:
                    logger.warning(f"Could not get region info for {cluster_id}: {e}")

        return {
            "regions": list(regions.values()),
            "total_nodes": sum(region["node_count"] for region in regions.values()),
            "total_instances": 0,
            "total_cpu": 0,
            "total_memory": 0,
            "total_storage": 0,
            "total_gpu": 0,
        }
    except Exception as e:
        logger.error(f"Error loading integration regions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@external_router.post(
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


@external_router.post(
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
    summary="List available clusters for current user",
    description="Get list of clusters the current user can assign to API keys. "
                "Filtered by the user's organization scope and excludes pending/unauthorized clusters.",
    dependencies=[Depends(require_partner())],
)
async def list_available_clusters():
    """List clusters available to the current user for API key assignment."""
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
            
            # filter by org scope (partner sees only their clusters)
            allowed = get_current_allowed_clusters()
            if allowed is not None:
                cluster_ids = [cid for cid in cluster_ids if cid in allowed]
            
            for cluster_id in cluster_ids:
                try:
                    cluster_info = cert_manager.get_cluster_info(cluster_id)
                    metadata = cluster_info.get("metadata", {})
                    
                    # skip pending/unauthorized clusters (no trust established)
                    status = metadata.get("status", "").lower()
                    if status in ("pending", "not_authorized", "error"):
                        continue
                    
                    clusters.append({
                        "id": cluster_id,
                        "name": metadata.get("name", cluster_id),
                        "endpoint": metadata.get("endpoint"),
                        "server_type": metadata.get("server_type", "lxd"),
                    })
                except Exception as e:
                    logger.warning(f"Could not get cluster info for {cluster_id}: {e}")
        
        return {"clusters": clusters}
        
    except Exception as e:
        logger.error(f"Error listing available clusters: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# Organization Sync Endpoints
# ============================================

class IntegrationOrgResponse(BaseModel):
    """Organization synced from an external app."""
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    source: str
    external_id: Optional[str] = None
    api_key_id: Optional[str] = None
    app_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@router.get(
    "/organizations",
    response_model=List[IntegrationOrgResponse],
    summary="List synced organizations",
    description="Get organizations synced from external apps (OrcaHub). Requires admin or partner role.",
)
async def list_synced_organizations(
    source: Optional[str] = Query(None, description="Filter by source: orcahub, local, or all"),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """List organizations synced from external applications, scoped by user membership."""
    from app.core.rbac import resolve_roles, get_highest_role, Role

    try:
        if source and source != "all":
            stmt = select(Organization).where(
                and_(Organization.source == source, Organization.is_active == True)
            )
        else:
            # default: return only externally synced orgs (non-local)
            stmt = select(Organization).where(
                and_(Organization.source != "local", Organization.is_active == True)
            )

        # scope by user membership for non-admins
        roles = resolve_roles(user)
        highest = get_highest_role(roles)
        if highest != Role.ADMIN:
            from sqlalchemy import or_ as sa_or
            stmt = stmt.where(
                sa_or(
                    Organization.created_by == user.email,
                    Organization.id.in_(
                        select(OrganizationMember.organization_id).where(
                            OrganizationMember.user_email == user.email,
                        )
                    ),
                )
            )

        result = await db.execute(stmt)
        orgs = result.scalars().all()

        # enrich with app_name from Vault
        vault_client = get_vault_integrations_client()
        app_name_cache: Dict[str, str] = {}

        response = []
        for org in orgs:
            app_name = None
            if org.api_key_id:
                if org.api_key_id not in app_name_cache:
                    try:
                        meta, _ = get_api_key_from_vault(vault_client, org.api_key_id)
                        app_name_cache[org.api_key_id] = meta.get("app_name", "")
                    except Exception:
                        app_name_cache[org.api_key_id] = ""
                app_name = app_name_cache[org.api_key_id] or None

            response.append(IntegrationOrgResponse(
                id=org.id,
                name=org.name,
                slug=org.slug,
                description=org.description,
                source=org.source,
                external_id=org.external_id,
                api_key_id=org.api_key_id,
                app_name=app_name,
                created_at=org.created_at.isoformat() if org.created_at else None,
                updated_at=org.updated_at.isoformat() if org.updated_at else None,
            ))

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing synced organizations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list synced organizations")


@router.post(
    "/refresh-organizations",
    summary="Refresh organizations from external app",
    description="Pull fresh organization data from connected external app (on-demand). Requires partner role.",
    dependencies=[Depends(require_partner())],
)
async def refresh_organizations(
    api_key_id: str = Query(..., description="API key ID of the external app to refresh from"),
    db: AsyncSession = Depends(get_db),
):
    """On-demand pull of organizations from external app via stored callback URL."""
    import httpx

    vault_client = get_vault_integrations_client()

    # Retrieve API key metadata including app_url and credentials
    try:
        metadata, _ = get_api_key_from_vault(vault_client, api_key_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="API key not found")

    app_url = metadata.get("app_url")
    if not app_url:
        raise HTTPException(
            status_code=400,
            detail="No callback URL configured for this API key. Trigger a sync from the external app first."
        )

    # Retrieve API key secret for authenticating the callback
    try:
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/secret"
        )
        secret_hash = secret_data.get("hash", "")
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read API key credentials")

    # Call external app's organizations endpoint
    callback_url = f"{app_url.rstrip('/')}/api/external/organizations"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                callback_url,
                headers={
                    "X-API-Key-ID": api_key_id,
                    "X-API-Key-Secret-Hash": secret_hash,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"External app returned {e.response.status_code}: {e.response.text}")
        raise HTTPException(status_code=502, detail=f"External app returned {e.response.status_code}")
    except Exception as e:
        logger.error(f"Failed to reach external app at {callback_url}: {e}")
        raise HTTPException(status_code=502, detail="Failed to reach external app")

    # Parse and upsert organizations
    orgs_data = data.get("organizations", [])
    if not orgs_data:
        return {"message": "No organizations returned from external app", "organizations_synced": 0}

    sync_orgs = [SyncOrganization(**o) for o in orgs_data]
    synced = await _upsert_synced_organizations(db, sync_orgs, api_key_id)

    return {
        "message": f"Refreshed {synced} organizations from external app",
        "organizations_synced": synced,
    }


# ============================================
# OrcaHub Tenant Access Policy Sync Endpoints
# ============================================

class SyncPolicyRule(BaseModel):
    """Rule within a synced policy."""
    cluster_id: str
    project_name: str = "*"
    instance_name: str = "*"
    permissions: List[str] = ["view"]


class SyncPolicyAssignment(BaseModel):
    """Assignment within a synced policy."""
    user_email: str
    expires_at: Optional[str] = None


class SyncPolicy(BaseModel):
    """Policy to sync from OrcaHub."""
    external_id: str = Field(..., description="UUID from OrcaHub")
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    org_external_id: str = Field(..., description="Organization's external_id")
    rules: List[SyncPolicyRule] = []
    assignments: List[SyncPolicyAssignment] = []


class SyncPoliciesRequest(BaseModel):
    """Request body for syncing policies from OrcaHub."""
    policies: List[SyncPolicy]


@external_router.post(
    "/external/sync-policies",
    summary="Sync tenant access policies from external app",
    description="Push tenant access policies from OrcaHub to Orcastra. Requires valid API key.",
    tags=["Integrations - External"],
)
async def sync_policies_from_external(
    body: SyncPoliciesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Sync tenant access policies from OrcaHub.

    Creates or updates policies based on external_id matching.
    Handles rules and assignments as part of the sync.
    """
    from app.models.db_models import (
        TenantAccessPolicy,
        TenantPolicyRule,
        TenantPolicyAssignment,
        OrganizationMember,
    )

    # authenticate via API key
    api_key_id = request.headers.get("X-API-Key-ID", "") or ""
    api_key_secret = request.headers.get("X-API-Key-Secret", "") or ""

    if not api_key_id or not api_key_secret:
        raise HTTPException(status_code=401, detail="API key credentials required")

    try:
        vault_client = get_vault_integrations_client()
        metadata, stored_hash = get_api_key_from_vault(vault_client, api_key_id)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # verify secret
    computed_hash = hashlib.sha256(api_key_secret.encode()).hexdigest()
    if not hmac.compare_digest(computed_hash, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid API key secret")

    if metadata.get("is_revoked"):
        raise HTTPException(status_code=403, detail="API key has been revoked")

    synced_count = 0
    errors = []

    for sync_policy in body.policies:
        try:
            # find org by external_id
            org_stmt = select(Organization).where(
                and_(
                    Organization.external_id == sync_policy.org_external_id,
                    Organization.is_active == True,
                )
            )
            result = await db.execute(org_stmt)
            org = result.scalar_one_or_none()

            if not org:
                errors.append(f"Organization with external_id '{sync_policy.org_external_id}' not found")
                continue

            # upsert policy by external_id
            existing_stmt = select(TenantAccessPolicy).where(
                and_(
                    TenantAccessPolicy.external_id == sync_policy.external_id,
                    TenantAccessPolicy.organization_id == org.id,
                )
            )
            result = await db.execute(existing_stmt)
            policy = result.scalar_one_or_none()

            if policy:
                # update existing
                policy.name = sync_policy.name
                policy.description = sync_policy.description
                policy.updated_at = datetime.utcnow()
            else:
                # create new
                policy = TenantAccessPolicy(
                    organization_id=org.id,
                    name=sync_policy.name,
                    description=sync_policy.description,
                    source="orcahub",
                    external_id=sync_policy.external_id,
                    created_by=f"orcahub:{api_key_id}",
                )
                db.add(policy)
                await db.flush()

            # replace rules
            await db.execute(
                delete(TenantPolicyRule).where(TenantPolicyRule.policy_id == policy.id)
            )
            for rule_data in sync_policy.rules:
                db.add(TenantPolicyRule(
                    policy_id=policy.id,
                    cluster_id=rule_data.cluster_id,
                    project_name=rule_data.project_name,
                    instance_name=rule_data.instance_name,
                    permissions=rule_data.permissions,
                ))

            # sync assignments (merge - don't remove local ones)
            for assign_data in sync_policy.assignments:
                # find org member
                member_stmt = select(OrganizationMember).where(
                    and_(
                        OrganizationMember.organization_id == org.id,
                        OrganizationMember.user_email == assign_data.user_email,
                    )
                )
                result = await db.execute(member_stmt)
                member = result.scalar_one_or_none()

                if not member:
                    continue  # skip unknown users

                # check existing assignment
                existing_assign = await db.execute(
                    select(TenantPolicyAssignment).where(
                        and_(
                            TenantPolicyAssignment.policy_id == policy.id,
                            TenantPolicyAssignment.org_member_id == member.id,
                        )
                    )
                )
                if not existing_assign.scalar_one_or_none():
                    db.add(TenantPolicyAssignment(
                        policy_id=policy.id,
                        org_member_id=member.id,
                        assigned_by=f"orcahub:{api_key_id}",
                        source="orcahub",
                        external_id=sync_policy.external_id,
                        expires_at=(
                            datetime.fromisoformat(assign_data.expires_at)
                            if assign_data.expires_at else None
                        ),
                    ))

            synced_count += 1

        except Exception as e:
            logger.error(f"Error syncing policy '{sync_policy.name}': {e}")
            errors.append(f"Failed to sync '{sync_policy.name}': {str(e)}")

    await db.commit()

    return {
        "message": f"Synced {synced_count} policies",
        "synced": synced_count,
        "errors": errors,
    }


@external_router.get(
    "/external/policies",
    summary="Read tenant access policies for sync",
    description="Read tenant access policies from Orcastra for OrcaHub pull sync. Requires valid API key.",
    tags=["Integrations - External"],
)
async def get_policies_for_external(
    org_external_id: Optional[str] = Query(None, description="Filter by organization external ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Read policies for external app pull sync."""
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    # authenticate via API key
    api_key_id = request.headers.get("X-API-Key-ID", "") if request else ""
    api_key_secret = request.headers.get("X-API-Key-Secret", "") if request else ""

    if not api_key_id or not api_key_secret:
        raise HTTPException(status_code=401, detail="API key credentials required")

    try:
        vault_client = get_vault_integrations_client()
        metadata, stored_hash = get_api_key_from_vault(vault_client, api_key_id)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid API key")

    computed_hash = hashlib.sha256(api_key_secret.encode()).hexdigest()
    if not hmac.compare_digest(computed_hash, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid API key secret")

    # build query
    stmt = (
        select(TenantAccessPolicy)
        .join(Organization, TenantAccessPolicy.organization_id == Organization.id)
        .where(Organization.is_active == True)
    )

    if org_external_id:
        stmt = stmt.where(Organization.external_id == org_external_id)

    # limit to orgs associated with this API key
    stmt = stmt.where(Organization.api_key_id == api_key_id)

    result = await db.execute(stmt)
    policies = result.scalars().unique().all()

    # build response
    response_policies = []
    for policy in policies:
        # load rules
        rules_stmt = select(TenantPolicyRule).where(TenantPolicyRule.policy_id == policy.id)
        rules_result = await db.execute(rules_stmt)
        rules = rules_result.scalars().all()

        org = await db.get(Organization, policy.organization_id)

        response_policies.append({
            "id": policy.id,
            "external_id": policy.external_id,
            "name": policy.name,
            "description": policy.description,
            "organization_id": policy.organization_id,
            "org_external_id": org.external_id if org else None,
            "source": policy.source,
            "is_default": policy.is_default,
            "rules": [
                {
                    "cluster_id": r.cluster_id,
                    "project_name": r.project_name,
                    "instance_name": r.instance_name,
                    "permissions": r.permissions,
                }
                for r in rules
            ],
            "created_at": policy.created_at.isoformat() if policy.created_at else None,
            "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
        })

    return {"policies": response_policies, "total": len(response_policies)}


# ============================================

# ============================================
# Auto-Provision Tenant Access (called by OrcaHub during provisioning)
# ============================================

class ProvisionTenantAccessRequest(BaseModel):
    """Request from OrcaHub to auto-create tenant access for a provisioned project."""
    organization_external_id: str = Field(..., description="OrcaHub organization UUID")
    cluster_id: str = Field(..., description="Cluster ID where the project was created")
    project_name: str = Field(..., description="LXD project name")
    instance_name: str = Field(default="*", description="Instance name or * for all in project")
    user_emails: List[str] = Field(default_factory=list, description="Tenant user emails to grant access")
    permissions: List[str] = Field(
        default=["view", "operate", "console", "snapshot", "file_transfer"],
        description="Permissions to grant",
    )
    # Auto-creation fields - sent when buyer org might not exist in Orcastra yet
    organization_name: Optional[str] = Field(default=None, description="Org name for auto-creation")
    organization_slug: Optional[str] = Field(default=None, description="Org slug for auto-creation")


class ProvisionTenantAccessResponse(BaseModel):
    """Response for provisioned tenant access."""
    policy_id: int
    policy_name: str
    organization_id: int
    rules_created: int
    assignments_created: int
    assignments_skipped: int
    org_created: bool = False
    members_created: int = 0
    cluster_mapped: bool = False


@external_router.post(
    "/provision-tenant-access",
    response_model=ProvisionTenantAccessResponse,
    summary="Auto-provision tenant access for a new project",
    description="Called by OrcaHub after provisioning to automatically create tenant access policies and assignments. Auto-creates org, members, and cluster mapping if they do not exist.",
)
async def provision_tenant_access(
    body: ProvisionTenantAccessRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-create a tenant access policy + assignments when OrcaHub provisions a new project.
    Idempotent: if policy already exists for this project, updates rules and adds new assignments.
    Auto-creates org, members, and cluster mapping if they do not exist yet.
    """
    api_key_id = api_key.get("id", "")
    org_created = False
    members_created = 0
    cluster_mapped = False

    # Step 1: resolve or auto-create org by external UUID
    stmt = select(Organization).where(
        and_(
            Organization.source == "orcahub",
            Organization.external_id == body.organization_external_id,
            Organization.api_key_id == api_key_id,
        )
    )
    result = await db.execute(stmt)
    org = result.scalar_one_or_none()

    if not org:
        if not body.organization_name or not body.organization_slug:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Organization with external_id={body.organization_external_id} not found. "
                    "Provide organization_name and organization_slug for auto-creation."
                ),
            )
        org = Organization(
            name=body.organization_name,
            slug=body.organization_slug,
            source="orcahub",
            external_id=body.organization_external_id,
            api_key_id=api_key_id,
            is_active=True,
        )
        db.add(org)
        await db.flush()
        org_created = True
        logger.info(f"Auto-created org {body.organization_slug} (external_id={body.organization_external_id})")



    # Step 1b: ensure buyer org inherits partner personalization (runs for new AND existing orgs)
    # note: partner org is found via api_key created_by email -> org membership,
    # because partner orgs created locally have api_key_id=NULL (not synced from OrcaHub)
    try:
        from app.models.db_models import OrganizationPreferences
        existing_prefs_stmt = select(OrganizationPreferences).where(
            OrganizationPreferences.organization_id == org.id
        )
        existing_prefs_result = await db.execute(existing_prefs_stmt)
        existing_prefs = existing_prefs_result.scalar_one_or_none()
        if not existing_prefs:
            partner_org = None

            # primary: find partner org via API key creator email -> org membership
            partner_email = api_key.get("created_by")
            if partner_email:
                partner_member_stmt = (
                    select(OrganizationMember)
                    .where(OrganizationMember.user_email == partner_email)
                    .order_by(OrganizationMember.joined_at.asc())
                    .limit(1)
                )
                partner_member_result = await db.execute(partner_member_stmt)
                partner_member = partner_member_result.scalar_one_or_none()
                if partner_member and partner_member.organization_id != org.id:
                    partner_org_stmt = select(Organization).where(
                        Organization.id == partner_member.organization_id
                    )
                    partner_org_result = await db.execute(partner_org_stmt)
                    partner_org = partner_org_result.scalar_one_or_none()
                    if partner_org:
                        logger.info(f"Found partner org {partner_org.slug} via creator email {partner_email}")

            # fallback: find partner org by shared api_key_id (for orgs synced from OrcaHub)
            if not partner_org:
                partner_stmt = select(Organization).where(
                    and_(
                        Organization.api_key_id == api_key_id,
                        Organization.id != org.id,
                    )
                ).order_by(Organization.created_at.asc()).limit(1)
                partner_result = await db.execute(partner_stmt)
                partner_org = partner_result.scalar_one_or_none()

            if partner_org:
                partner_prefs_stmt = select(OrganizationPreferences).where(
                    OrganizationPreferences.organization_id == partner_org.id
                )
                partner_prefs_result = await db.execute(partner_prefs_stmt)
                partner_prefs = partner_prefs_result.scalar_one_or_none()
                if partner_prefs and partner_prefs.settings:
                    buyer_prefs = OrganizationPreferences(
                        organization_id=org.id,
                        settings=partner_prefs.settings,
                        updated_by="orcahub-provisioning",
                    )
                    db.add(buyer_prefs)
                    await db.flush()
                    logger.info(f"Copied partner personalization to buyer org {org.slug}")
                else:
                    logger.info(f"Partner org {partner_org.slug} has no personalization to copy")
            else:
                logger.info(f"No partner org found for personalization inheritance (api_key_id={api_key_id})")
    except Exception as e:
        logger.warning(f"Failed to copy personalization (non-fatal): {e}")

    # Step 2: ensure cluster is mapped to this org
    from app.models.db_models import OrganizationCluster
    cluster_stmt = select(OrganizationCluster).where(
        and_(
            OrganizationCluster.organization_id == org.id,
            OrganizationCluster.cluster_id == body.cluster_id,
        )
    )
    cluster_result = await db.execute(cluster_stmt)
    if not cluster_result.scalar_one_or_none():
        db.add(OrganizationCluster(
            organization_id=org.id,
            cluster_id=body.cluster_id,
        ))
        await db.flush()
        cluster_mapped = True
        logger.info(f"Auto-mapped cluster {body.cluster_id} to org {org.slug}")

    # Step 3: ensure org members exist for all user_emails
    for email in body.user_emails:
        member_check = await db.execute(
            select(OrganizationMember).where(
                and_(
                    OrganizationMember.organization_id == org.id,
                    OrganizationMember.user_email == email,
                )
            )
        )
        if not member_check.scalar_one_or_none():
            db.add(OrganizationMember(
                organization_id=org.id,
                user_email=email,
                role="tenant",
            ))
            members_created += 1
            logger.info(f"Auto-created tenant member {email} in org {org.slug}")

    await db.flush()

    # validate permissions
    valid_perms = {"view", "operate", "console", "snapshot", "file_transfer"}
    invalid = set(body.permissions) - valid_perms
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid permissions: {invalid}")

    # find or create policy for this project (idempotent)
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment

    policy_name = f"auto:{body.project_name}"
    policy_stmt = select(TenantAccessPolicy).where(
        and_(
            TenantAccessPolicy.organization_id == org.id,
            TenantAccessPolicy.name == policy_name,
        )
    )
    policy_result = await db.execute(policy_stmt)
    policy = policy_result.scalar_one_or_none()

    if not policy:
        policy = TenantAccessPolicy(
            organization_id=org.id,
            name=policy_name,
            description=f"Auto-provisioned access for project {body.project_name}",
            is_default=False,
            source="orcahub",
            external_id=body.organization_external_id,
            created_by="orcahub-provisioning",
        )
        db.add(policy)
        await db.flush()

    # upsert rule for this cluster/project/instance
    rule_stmt = select(TenantPolicyRule).where(
        and_(
            TenantPolicyRule.policy_id == policy.id,
            TenantPolicyRule.cluster_id == body.cluster_id,
            TenantPolicyRule.project_name == body.project_name,
            TenantPolicyRule.instance_name == body.instance_name,
        )
    )
    rule_result = await db.execute(rule_stmt)
    existing_rule = rule_result.scalar_one_or_none()
    rules_created = 0

    if existing_rule:
        existing_rule.permissions = sorted(set(body.permissions))
    else:
        db.add(TenantPolicyRule(
            policy_id=policy.id,
            cluster_id=body.cluster_id,
            project_name=body.project_name,
            instance_name=body.instance_name,
            permissions=sorted(set(body.permissions)),
        ))
        rules_created = 1

    # assign policy to each user email (members now guaranteed to exist)
    assignments_created = 0
    assignments_skipped = 0

    for email in body.user_emails:
        member_stmt = select(OrganizationMember).where(
            and_(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_email == email,
            )
        )
        member_result = await db.execute(member_stmt)
        member = member_result.scalar_one_or_none()

        if not member:
            assignments_skipped += 1
            continue

        existing_assign = await db.execute(
            select(TenantPolicyAssignment).where(
                and_(
                    TenantPolicyAssignment.policy_id == policy.id,
                    TenantPolicyAssignment.org_member_id == member.id,
                )
            )
        )
        if existing_assign.scalar_one_or_none():
            assignments_skipped += 1
            continue

        db.add(TenantPolicyAssignment(
            policy_id=policy.id,
            org_member_id=member.id,
            assigned_by="orcahub-provisioning",
            source="orcahub",
        ))
        assignments_created += 1

    await db.commit()

    # invalidate caches so tenants see access immediately
    try:
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "instances:*")
        cache.delete_pattern("api_responses", "projects:*")
        cache.delete_pattern("api_responses", "monitoring:*")
    except Exception:
        pass

    logger.info(
        f"Tenant access provisioned: policy={policy_name}, org={org.slug}, "
        f"org_created={org_created}, members_created={members_created}, cluster_mapped={cluster_mapped}, "
        f"rules={rules_created}, assignments={assignments_created}, skipped={assignments_skipped}"
    )

    return ProvisionTenantAccessResponse(
        policy_id=policy.id,
        policy_name=policy_name,
        organization_id=org.id,
        rules_created=rules_created,
        assignments_created=assignments_created,
        assignments_skipped=assignments_skipped,
        org_created=org_created,
        members_created=members_created,
        cluster_mapped=cluster_mapped,
    )
