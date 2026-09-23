"""
API Router for External Integrations.
Provides endpoints for managing API keys, external app connections, and delegated access.
Supports integration with Orcahub and other external applications (soon with Orcalens, Orcasonic, Orcaflux, other Orca product).
"""

import asyncio
import re
import secrets
import hashlib
from urllib.parse import urlencode, urlsplit
import hmac
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Optional, List, Dict, Any, Tuple, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
import requests

from app.services.lxd_service import LXDService, get_lxd_service
from app.services import cluster_parties
from app.services.lxd import health
from app.services.lxd.fanout import is_read_timeout
from app.services.lxd.proxy import (
    ALLOWED_METHODS,
    API_KEY_CACHE_TTL,
    AUTH_POOL_WORKERS,
    LAST_USED_WRITE_INTERVAL,
    PROXY_CONNECT_TIMEOUT,
    PROXY_MAX_IN_FLIGHT,
    PROXY_OP_TIMEOUT,
    PROXY_POOL_WORKERS,
    RAW_PROXY_TIMEOUT,
    SYNC_CLUSTER_TIMEOUT,
    VALIDATE_KEY_TIMEOUT,
    auth_executor,
    proxy_executor,
    raw_request,
    socket_timeout_for,
)
from app.core.api_key_permissions import PERMISSION_LEVELS, assert_cluster_permission, granted_level
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.error_sanitizer import sanitize_error
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_org_ids_for_email,
    get_scope_cache_key,
    org_scope_dependency,
    project_is_visible,
    reconcile_personal_clusters,
)
from app.core.cache import get_cache_manager
from app.core.vault_client import get_vault_client, safe_vault_url
from app.core.config import get_settings
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete
from sqlalchemy.exc import IntegrityError
from app.models.db_models import Organization, OrganizationMember
from app.core.lxd_names import safe_segment
from app.core.proxy_paths import (
    assert_body_within_grant,
    assert_project_scope_allows_path,
    assert_proxy_path_allowed,
    assert_server_state_unchanged,
    project_allowed_by_grant,
    query_key_name,
    split_proxy_path,
)
from app.core.type_utils import normalize_cluster_type
from app.core.safe_http import guarded_request
from app.core.url_safety import UnsafeURLError
from app.services.activity_logger import get_activity_logger
from app.services.integration_roles import apply_upsert_role, revoke_if_orphaned
from app.services.orcahub_sync import callback_networks, callback_requires_https, check_callback_url

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

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

# important: one answer for an unknown key id and a wrong secret, on every external endpoint.
# Two texts told a caller which key ids exist. Revoked, inactive and expired keep their own
# texts, because reaching them already needs the correct secret.
INVALID_API_KEY_MESSAGE = "Invalid API key or secret"


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
        description="Permission levels: read, write, admin. At least one is required.",
    )
    projects: List[str] = Field(
        default=["default"],
        description="Allowed projects within the cluster"
    )

    @field_validator("permissions")
    @classmethod
    def _reject_a_grant_that_grants_nothing(cls, value: List[str]) -> List[str]:
        """Refuse an empty or unrecognised grant at the boundary.

        important: this rejects new mistakes, it does not re-validate what Vault already holds.
        Stored keys are read back without passing through this model, so an existing grant with
        an unfamiliar name keeps working and nobody loses access to a running integration.

        An empty list was accepted before and reached a ``max()`` over nothing, which surfaced as
        HTTP 500. The runtime now answers 403 for it either way (app/core/api_key_permissions.py);
        this is the second half, so the mistake is caught where it is made rather than on first
        use. An unrecognised name is worth refusing for a different reason: at runtime it scores
        as "read" rather than as nothing, so a typo silently becomes a read grant.
        """
        if not value:
            raise ValueError(
                "permissions must name at least one of read, write, admin. "
                "An empty list grants nothing, which is never a useful key."
            )
        unknown = sorted({name for name in value if name not in PERMISSION_LEVELS})
        if unknown:
            raise ValueError(
                f"unknown permission level(s): {', '.join(unknown)}. "
                f"Valid levels are {', '.join(PERMISSION_LEVELS)}."
            )
        return value


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
    # important: this value becomes a Vault KV path segment in save_user_mapping_to_vault.
    # ExternalUserMapping stays lenient on purpose: it is rebuilt from stored JSON, and a pattern
    # there would silently drop legacy mappings from listings and counts.
    external_user_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_](?:[A-Za-z0-9._@|-]*[A-Za-z0-9_])?$",
        description="External user ID",
    )
    external_email: str = Field(..., description="External user email")
    external_name: Optional[str] = Field(None, description="External user name")
    cluster_permissions: List[ClusterPermission] = Field(
        default_factory=list,
        description="Per-cluster permissions"
    )


# note: each element costs its own commit, members also cost Authentik calls.
# One real OrcaHub push is its org plus buyer orgs and their members, far below these.
MAX_SYNC_ORGANIZATIONS = 1000
MAX_SYNC_MEMBERS = 5000
MAX_SYNC_POLICIES = 1000
MAX_SYNC_POLICY_RULES = 1000


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
        max_length=MAX_SYNC_ORGANIZATIONS,
        description="Organizations to sync from external app"
    )
    # Callback URL for on-demand data pull
    app_url: Optional[str] = Field(
        None, max_length=2048, description="External app base URL for callbacks"
    )
    # Member data pushed from external app (ex; OrcaHub)
    members: List[SyncMember] = Field(
        default_factory=list,
        max_length=MAX_SYNC_MEMBERS,
        description="Organization members to sync from external app"
    )

    @field_validator("app_url")
    @classmethod
    def _app_url_is_a_safe_callback(cls, value: Optional[str]) -> Optional[str]:
        # important: stored, then fetched by three outbound calls, one carrying the plaintext secret.
        # Use time re-checks it too, so a value stored before this check is not trusted either.
        # note: a value the policy refuses is dropped, not refused. Refusing failed the whole sync
        # (organizations, members, clusters) for an OrcaHub whose BACKEND_URL is plain http, the
        # shape its own env template ships. Dropped, the sync goes through, the stored value is left
        # as it was, and no callback reaches an address the policy refuses.
        if value is None:
            return None
        try:
            return check_callback_url(value)
        except ValueError as exc:
            try:
                parts = urlsplit(value)
                shown = f"{parts.scheme}://{parts.hostname or '?'}"
            except ValueError:
                shown = "an unparsable URL"
            logger.warning(
                "Ignoring the app_url of a sync (%s): %s. OrcaHub callbacks stay off until its "
                "BACKEND_URL is public https or its address is in ORCAHUB_CALLBACK_ALLOWED_NETWORKS",
                shown, exc,
            )
            return None


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
    # important: the list_* operations return a list, not a mapping. Typed as Dict only, every
    # one of them failed response validation and reported success:false, so this endpoint has
    # never returned data for any operation. Widened rather than wrapped so the payload shape
    # matches what the operation naturally produces.
    data: Optional[Union[Dict[str, Any], List[Any]]] = None
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
    key_secret, secret_hash = generate_api_key_secret()
    return key_id, key_secret, secret_hash


def generate_api_key_secret() -> tuple[str, str]:
    """A new secret (shown only once) and its HMAC-SHA256 hash, keyed so a rainbow table is useless."""
    key_secret = f"oas_{secrets.token_urlsafe(32)}"  # orcastra api secret
    secret_hash = hmac.new(_get_hmac_key(), key_secret.encode(), hashlib.sha256).hexdigest()
    return key_secret, secret_hash


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
            safe_vault_url(
                vault_client.vault_addr,
                f"secret/metadata/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/users",
            ),
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


# Process-local API key cache and last-used throttle.
#
# important: this stays in process memory and never reaches Redis. CacheManager writes
# non-sensitive namespaces to Redis, and API key material has no business leaving the
# process. Mirrors the module-level-lock-plus-dict pattern already used by
# app/services/lxd/health.py.
_key_cache_lock = threading.Lock()
# digest -> (cached_at_monotonic, key_id, metadata)
_key_cache: Dict[str, tuple] = {}
# key_id -> monotonic timestamp of the last last_used_at write
_last_used_written: Dict[str, float] = {}


def _api_key_digest(key_id: str, key_secret: str) -> str:
    """Cache key covering BOTH halves, so a wrong secret can never hit a cached entry."""
    return hashlib.sha256(f"{key_id}:{key_secret}".encode()).hexdigest()


# important: the metadata cache is per-process, so a revocation served by one uvicorn worker
# would leave the key valid on the others until their own TTL expired (prod runs
# WEB_CONCURRENCY=4). Revocations are therefore also published as a short-lived shared
# tombstone, which every worker checks before trusting a local hit. Without Redis this falls
# back to the per-process TTL, which is the pre-tombstone behaviour rather than a new hole.
_REVOKED_NS = "apikey_revoked"


def _is_revoked_elsewhere(key_id: str) -> bool:
    try:
        return get_cache_manager().get(_REVOKED_NS, key_id) is not None
    except Exception:  # a cache hiccup must never break authentication
        return False


def _cached_key_metadata(digest: str) -> Optional[Dict[str, Any]]:
    with _key_cache_lock:
        entry = _key_cache.get(digest)
        if not entry:
            return None
        cached_at, key_id, metadata = entry
        if time.monotonic() - cached_at > API_KEY_CACHE_TTL:
            _key_cache.pop(digest, None)
            return None
    if _is_revoked_elsewhere(key_id):
        with _key_cache_lock:
            _key_cache.pop(digest, None)
        return None
    return metadata


def _store_key_metadata(digest: str, key_id: str, metadata: Dict[str, Any]) -> None:
    with _key_cache_lock:
        _key_cache[digest] = (time.monotonic(), key_id, metadata)


def evict_api_key_cache(key_id: str) -> None:
    """Drop cached metadata for a key so revocation takes effect immediately.

    Also publishes a shared tombstone so the other workers drop their copies on the next use
    rather than honouring a revoked key until their own TTL runs out.
    """
    with _key_cache_lock:
        for digest in [d for d, (_, kid, _m) in _key_cache.items() if kid == key_id]:
            _key_cache.pop(digest, None)
        _last_used_written.pop(key_id, None)
    try:
        get_cache_manager().set(
            _REVOKED_NS, key_id, True, ttl=int(API_KEY_CACHE_TTL + VALIDATE_KEY_TIMEOUT) + 5
        )
    except Exception as exc:
        logger.warning("Could not publish API key revocation for %s: %s", key_id, exc)


def _write_last_used_blocking(key_id: str) -> None:
    """Runs on auth_executor.

    important: the Vault client is built here, inside the thread, not by the caller.
    get_vault_integrations_client raises when Vault is unconfigured, and building it on the
    event loop would turn a bookkeeping failure into a failed authentication.
    """
    try:
        update_api_key_last_used(get_vault_integrations_client(), key_id)
    except Exception as e:
        logger.debug(f"last_used_at update skipped for {key_id}: {e}")


def _schedule_last_used_update(key_id: str) -> None:
    """Record last_used_at at most once per interval, off the event loop, without awaiting.

    Fire and forget on purpose: a request must never pay Vault latency, nor fail, for a
    bookkeeping field.
    """
    now = time.monotonic()
    with _key_cache_lock:
        if now - _last_used_written.get(key_id, 0.0) < LAST_USED_WRITE_INTERVAL:
            return
        _last_used_written[key_id] = now
    try:
        auth_executor.submit(_write_last_used_blocking, key_id)
    except RuntimeError:  # pool shutting down
        pass


_PROXY_WAIT_OPERATIONS = frozenset({
    "create_instance", "delete_instance", "start_instance",
    "stop_instance", "restart_instance",
})


def _execute_proxy_operation(
    lxd_service,
    cluster_id: str,
    operation: str,
    params: Dict[str, Any],
    project: str,
    allowed_projects: Optional[List[str]],
) -> Any:
    """Blocking half of proxy_operation. Only ever runs on proxy_executor.

    important: pylxd binds the project at the CLIENT level. Instance.all/get/create and
    Project.all/Image.all take no `project=` kwarg (verified against the pylxd in the runtime
    image), so the previous per-call `project=` raised TypeError before any network I/O and
    every instance branch returned success=False. Project scoping now goes through
    get_project_client, which is what that method exists for.
    """
    # The pooled client first: it populates the cached connection info and temp cert paths
    # that get_project_client reuses, and it is what the reconnect logic maintains.
    client = lxd_service.connection_manager.get_client(cluster_id)
    if not client:
        raise HTTPException(
            status_code=503, detail=f"Cluster {cluster_id} is not connected"
        )

    # Operations that poll an LXD operation to completion need a wider read window than the
    # pooled client's default, and a non-default project needs a scoped client either way.
    needs_scope = (project and project != "default") or operation in _PROXY_WAIT_OPERATIONS
    if needs_scope:
        scoped = lxd_service.connection_manager.get_project_client(
            cluster_id, project or "default", timeout=(3, 30)
        )
        if scoped:
            client = scoped
        elif project and project != "default":
            raise HTTPException(
                status_code=503,
                detail=f"Cannot scope to project {project} on {cluster_id}",
            )

    def _named_instance():
        name = params.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="Instance name required")
        return client.instances.get(safe_segment(name))

    if operation == "list_instances":
        return [
            {"name": i.name, "status": i.status, "type": i.type,
             "architecture": i.architecture}
            for i in client.instances.all()
        ]
    if operation == "get_instance":
        instance = _named_instance()
        return {
            "name": instance.name, "status": instance.status, "type": instance.type,
            "architecture": instance.architecture, "config": dict(instance.config),
        }
    if operation == "create_instance":
        instance = client.instances.create(params, wait=True)
        return {"name": instance.name, "status": instance.status}
    if operation == "delete_instance":
        _named_instance().delete(wait=True)
        return {"deleted": True}
    if operation == "start_instance":
        _named_instance().start(wait=True)
        return {"status": "Running"}
    if operation == "stop_instance":
        _named_instance().stop(force=params.get("force", False), wait=True)
        return {"status": "Stopped"}
    if operation == "restart_instance":
        _named_instance().restart(force=params.get("force", False), wait=True)
        return {"status": "Running"}
    if operation == "list_projects":
        # note: a project-limited key sees only the projects it was granted.
        return [
            {"name": p.name, "description": p.description}
            for p in client.projects.all()
            if project_allowed_by_grant(p.name, allowed_projects)
        ]
    if operation == "list_images":
        return [
            {"fingerprint": img.fingerprint[:12],
             "description": img.properties.get("description", ""),
             "size": img.size}
            for img in client.images.all()
        ]

    raise HTTPException(status_code=400, detail=f"Unknown operation: {operation}")


def _fetch_cluster_info(cert_manager, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the sync cluster-info fan-out. Reads Vault, so it must not run on the
    event loop. Returns a single-item list so it composes with the shared gather helper.

    important: the family comes from `type`, the key save_cluster_metadata and check-auth write.
    This read `server_type`, which nothing writes, so every cluster reached integrators as "lxd".
    Every lookup tolerates odd metadata, because a raise drops the cluster from the payload and
    the integrator then deletes a cluster that still exists.
    """
    cluster_info = cert_manager.get_cluster_info(cluster_id)
    metadata = cluster_info.get("metadata") if isinstance(cluster_info, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    location_data = metadata.get("location")
    location_data = location_data if isinstance(location_data, dict) else {}
    public_location = _public_location_data(location_data)
    zone_name = location_data.get("region_name") or location_data.get("city") or None
    stored_type = metadata.get("type")
    cluster_type = (
        normalize_cluster_type(stored_type)
        if isinstance(stored_type, str) and stored_type.strip()
        else None
    )
    display_name = metadata.get("display_name")
    return [{
        "id": cluster_id,
        # note: `name` stays the id integrators key on. The human name travels as display_name.
        "name": metadata.get("name", cluster_id),
        "display_name": (
            display_name if isinstance(display_name, str) and display_name.strip() else cluster_id
        ),
        "endpoint": metadata.get("endpoint"),
        "server_type": (cluster_type or "LXD").lower(),
        "cluster_type": cluster_type,
        # note: nothing on this line writes `clustered`, so it is reported as unknown.
        "clustered": None,
        "registered_at": metadata.get("registered_at"),
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
    }]


def _key_project_grants(api_key: Dict[str, Any]) -> Dict[str, List[str]]:
    """Each cluster's project grant on a key, read the way raw_proxy reads it."""
    return {
        p["cluster_id"]: p.get("projects", ["default"])
        for p in api_key.get("cluster_permissions", [])
    }


def _fetch_cluster_projects(
    lxd_service, cluster_id: str, grants: Dict[str, List[str]]
) -> List[Dict[str, Any]]:
    """Blocking half of the sync projects fan-out. Only ever runs on proxy_executor.

    note: a project-limited key sees only the projects it was granted.
    """
    client = lxd_service.connection_manager.get_client(cluster_id)
    if not client:
        return []
    allowed = grants.get(cluster_id)
    return [
        {"name": p.name, "cluster_id": cluster_id, "description": p.description}
        for p in client.projects.all()
        if project_allowed_by_grant(p.name, allowed)
    ]


def _fetch_cluster_instances(
    lxd_service, cluster_id: str, grants: Dict[str, List[str]]
) -> List[Dict[str, Any]]:
    """Blocking half of the sync instances fan-out. Only ever runs on proxy_executor.

    note: the pooled client reads the default project only, so a key whose grant does not
    cover `default` gets nothing here rather than another project's instance names.
    """
    if not project_allowed_by_grant("default", grants.get(cluster_id)):
        return []
    client = lxd_service.connection_manager.get_client(cluster_id)
    if not client:
        return []
    return [
        {
            "name": i.name,
            "cluster_id": cluster_id,
            "status": i.status,
            "type": i.type,
        }
        for i in client.instances.all()
    ]


async def _gather_per_cluster(
    fn, ctx, cluster_ids, label: str
) -> List[Dict[str, Any]]:
    """Run a blocking per-cluster fetch across clusters concurrently and off the event loop.

    important: this replaced a serial in-loop fan-out. With one silent cluster that shape
    blocked the whole process for the sum of every cluster's timeout. Measured on dev before
    the change, a single POST /integrations/sync held the loop for 35.7s, which is what made
    /health spike to 11s. Now the cost is the slowest cluster, paid on a worker thread, and a
    cluster the breaker has already tripped is skipped entirely.
    """
    async def one(cluster_id: str) -> List[Dict[str, Any]]:
        if health.is_tripped(cluster_id):
            logger.debug(f"sync: skipping {label} for tripped cluster {cluster_id}")
            return []
        try:
            return await run_blocking(
                fn,
                ctx,
                cluster_id,
                timeout=SYNC_CLUSTER_TIMEOUT,
                label=f"sync_{label}[{cluster_id}]",
                executor=proxy_executor,
                max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
        except BlockingTimeout:
            # important: deliberately does NOT trip the shared breaker. A bulk list timing
            # out means "this cluster has a lot of data or is slow right now", which is not
            # the same as "unreachable". Condemning it here would make every raw_proxy call
            # for that cluster return 503 for a full TTL, so one large tenant could take its
            # own proxy access down.
            logger.warning(
                f"sync: {label} for {cluster_id} timed out after {SYNC_CLUSTER_TIMEOUT}s; "
                "returning partial results for this cluster"
            )
            return []
        except BlockingPoolFull:
            logger.warning(f"sync: {label} for {cluster_id} shed, proxy pool saturated")
            return []
        except Exception as e:
            logger.warning(f"Could not get {label} for {cluster_id}: {e}")
            return []

    batches = await asyncio.gather(*[one(c) for c in cluster_ids])
    return [item for batch in batches for item in batch]


# note: the external auth reads run on the four auth threads that also authenticate every
# API-key request, so an unauthenticated flood may queue only this many before being shed.
EXTERNAL_AUTH_MAX_IN_FLIGHT = AUTH_POOL_WORKERS * 4
REGIONS_UNAVAILABLE = "Cluster regions could not be read in time. Retry shortly."


def _cluster_display_names_blocking(cluster_ids: List[str]) -> Dict[str, str]:
    """Display name per cluster id, from its Vault metadata only. Falls back to the id."""
    names = {cluster_id: cluster_id for cluster_id in cluster_ids}
    settings = get_settings()
    if not cluster_ids or not (settings.vault_enabled and settings.vault_token):
        return names
    vault = get_vault_client(settings.vault_addr, settings.vault_token)
    for cluster_id in cluster_ids:
        try:
            meta = vault.vault_read(f"{settings.vault_clusters_path}/{cluster_id}/metadata")
        except Exception:
            continue
        if isinstance(meta, dict):
            names[cluster_id] = meta.get("display_name") or meta.get("name") or cluster_id
    return names


def _region_rows_blocking(cluster_ids: set) -> Dict[str, Dict[str, Any]]:
    """The /regions rows for the given clusters. Reads Vault, so it only runs off the loop."""
    from app.services.lxd_cert_manager import LXDCertificateManager

    settings = get_settings()
    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
    cert_manager = LXDCertificateManager(
        vault_client=vault_client,
        clusters_path=settings.vault_clusters_path
    )
    regions: Dict[str, Dict[str, Any]] = {}
    for cluster_id in sorted(cluster_ids):
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
    return regions


CLUSTER_INFO_UNREADABLE = (
    "Cluster details could not be read for every cluster this key covers, so this sync "
    "cannot say which clusters exist. Nothing was removed. Retry shortly."
)
CLUSTER_INFO_SATURATED = (
    "The integration proxy is busy, so cluster details could not be read for every cluster "
    "this key covers. Nothing was removed. Retry shortly."
)
# note: the gather shares proxy_executor and its in-flight budget with raw proxy traffic.
# Starting one read per cluster at once shed every read past the free budget, so a key covering
# more clusters than that failed every sync. The pool has this many threads, so reading more at
# once only queued the rest while their timeouts ran.
CLUSTER_INFO_CONCURRENCY = max(1, PROXY_POOL_WORKERS)


async def _gather_cluster_info(cert_manager, cluster_ids) -> List[Dict[str, Any]]:
    """Every covered cluster's info, or a 503. A cluster is left out only once it is gone.

    important: an integrator deletes every cluster a sync omits (OrcaHub deletes its cluster row
    and the service exposures hanging off it), so omitting one that still exists is destructive.
    Cluster info comes from Vault, not the cluster, so the LXD breaker is not consulted. When a
    read fails, the registry decides: a cluster the readable registry no longer lists is gone and
    is omitted as before, anything else answers 503 so the integrator keeps what it has.
    """
    gate = asyncio.Semaphore(CLUSTER_INFO_CONCURRENCY)
    shed: List[str] = []

    async def one(cluster_id: str) -> tuple:
        async with gate:
            try:
                rows = await run_blocking(
                    _fetch_cluster_info,
                    cert_manager,
                    cluster_id,
                    timeout=SYNC_CLUSTER_TIMEOUT,
                    label=f"sync_cluster_info[{cluster_id}]",
                    executor=proxy_executor,
                    max_in_flight=PROXY_MAX_IN_FLIGHT,
                )
                return cluster_id, rows
            except BlockingPoolFull:
                shed.append(cluster_id)
                return cluster_id, None
            except Exception as exc:  # noqa: BLE001 - any failure is decided by the registry below
                logger.warning("Could not read cluster_info for %s: %s", cluster_id, sanitize_error(str(exc)))
                return cluster_id, None

    results = await asyncio.gather(*[one(c) for c in sorted(cluster_ids)])
    if shed:
        logger.warning("sync: cluster_info shed for %d cluster(s), proxy pool saturated", len(shed))
        raise HTTPException(status_code=503, detail=CLUSTER_INFO_SATURATED)
    unreadable = [cluster_id for cluster_id, rows in results if rows is None]
    if unreadable:
        try:
            registry = await run_blocking(
                partial(cert_manager.read_registry, use_cache=False),
                timeout=SYNC_CLUSTER_TIMEOUT,
                label="sync_cluster_registry",
                executor=proxy_executor,
                max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
        except Exception as exc:  # noqa: BLE001 - an unknown registry cannot vouch for a removal
            logger.warning("Could not read the cluster registry for a sync: %s", sanitize_error(str(exc)))
            raise HTTPException(status_code=503, detail=CLUSTER_INFO_UNREADABLE) from None
        registered = set(registry.cluster_ids or [])
        if not registry.readable or any(c in registered for c in unreadable):
            raise HTTPException(status_code=503, detail=CLUSTER_INFO_UNREADABLE)
        logger.info("sync: omitting deregistered cluster(s) %s", sorted(unreadable))
    return [row for _cluster_id, rows in results if rows for row in rows]


def _validate_api_key_blocking(key_id: str, key_secret: str) -> Dict[str, Any]:
    """Every Vault round trip needed to validate a key. Only ever runs on auth_executor.

    Was previously inline on the event loop: 6 to 7 blocking Vault calls per request, each
    (2,3) with Retry(total=2), so a degraded Vault could park a worker for a minute or more.
    """
    vault_client = get_vault_integrations_client()
    metadata, secret_hash = get_api_key_from_vault(vault_client, key_id)

    if not verify_api_key_secret(key_secret, secret_hash):
        raise HTTPException(status_code=401, detail=INVALID_API_KEY_MESSAGE)

    # Backfill raw secret if not already stored (for callback auth)
    try:
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
        )
        # note: only for the hash just verified. A rotation between the two reads has already
        # stored its own raw copy, and writing this one back would revive the old secret.
        if not secret_data.get("raw") and secret_data.get("hash") == secret_hash:
            secret_data["raw"] = key_secret
            vault_client.vault_kv_put(
                f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret",
                secret_data,
            )
            logger.info(f"Backfilled raw secret for API key {key_id}")
    except Exception:
        pass  # non-critical

    return metadata


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
    Persist organizations pushed from OrcaHub. Idempotent by design: this endpoint is
    re-called on every sync (scheduled every ~5 min + manual), so it must never 500 on
    a re-run and must never corrupt data.

    Identity is (source='orcahub', external_id). `slug` is a separate UNIQUE column, and
    OrcaHub itself enforces globally-unique org slugs, so an existing source='orcahub' row
    that shares a slug IS the same logical org (its external_id churned via delete+recreate).

    Two passes so a slug freed by a rename in this same batch is available before any insert:
      Pass 1 - update every known identity in place (may free a renamed slug).
      Pass 2 - for the rest: slug free -> insert; slug held by a source='orcahub' row whose
               external_id is RETIRED (absent from this push) -> re-link it (the deleted
               predecessor of an org that reused the slug); slug held by any still-live row
               (a non-orcahub org, or an orcahub org still present in this push) -> skip +
               warn, never overwrite another org's identity or re-parent its members.

    Each org commits on its own, so one bad/conflicting row is skipped (degraded) instead of
    aborting the whole sync. Returns count of synced organizations.
    """
    synced = 0
    handled: set[str] = set()
    # Identities present in THIS push. A slug-owning orcahub row whose external_id is still
    # in this set is a live org we must not steal the slug from (only genuinely retired
    # identities are safe to re-link).
    incoming_ids = {o.id for o in organizations}

    # Pass 1: reconcile known identities. Updating here frees any slug the org renamed away
    # from, so Pass 2's inserts/re-links see the post-rename slug map.
    for org_data in organizations:
        try:
            existing = (await db.execute(
                select(Organization).where(
                    and_(
                        Organization.source == "orcahub",
                        Organization.external_id == org_data.id,
                    )
                )
            )).scalar_one_or_none()
            if not existing:
                continue
            # important: `api_key_id` records which key synced an org, and the upsert wrote it
            # without ever reading it, so any valid key could claim and rewrite an org another
            # integration owned, including its members' roles. A key may only update what it
            # already owns, or an org not yet claimed by one.
            if existing.api_key_id and existing.api_key_id != api_key_id:
                logger.warning(
                    "Refusing sync of organization %s (external_id=%s): owned by api key %s, "
                    "caller is %s",
                    existing.slug, org_data.id, existing.api_key_id, api_key_id,
                )
                handled.add(org_data.id)
                continue
            existing.name = org_data.name
            existing.slug = org_data.slug
            existing.description = org_data.description
            existing.api_key_id = api_key_id
            existing.is_active = True
            await db.commit()
            handled.add(org_data.id)
            synced += 1
        except Exception as e:
            await db.rollback()
            logger.warning(f"Skipped organization {org_data.id} (slug={org_data.slug}) during sync update: {e}")

    # Pass 2: create or re-link the orgs not matched by identity above.
    for org_data in organizations:
        if org_data.id in handled:
            continue
        try:
            slug_owner = (await db.execute(
                select(Organization).where(Organization.slug == org_data.slug)
            )).scalar_one_or_none()

            if slug_owner is None:
                db.add(Organization(
                    name=org_data.name,
                    slug=org_data.slug,
                    description=org_data.description,
                    created_by="system",
                    source="orcahub",
                    external_id=org_data.id,
                    api_key_id=api_key_id,
                    is_active=True,
                ))
            elif (
                slug_owner.source == "orcahub"
                and slug_owner.external_id not in incoming_ids
                and (not slug_owner.api_key_id or slug_owner.api_key_id == api_key_id)
            ):
                # The row's OrcaHub identity is RETIRED (its external_id is absent from this
                # push) and OrcaHub slugs are globally unique, so this row is the deleted
                # predecessor of the incoming org that reused the slug. Re-link it in place.
                # important: same ownership rule as Pass 1. Without it any key could take another
                # integration's org by pushing its slug under a fresh external_id, and owning it
                # then passes the member sync that pushes roles into Authentik.
                slug_owner.name = org_data.name
                slug_owner.description = org_data.description
                slug_owner.external_id = org_data.id
                slug_owner.api_key_id = api_key_id
                slug_owner.is_active = True
            else:
                # important: the slug is held by a row we must NOT steal - either a non-OrcaHub
                # org (ex; a partner's local org) or another OrcaHub org whose identity is still
                # live in this push (a Pass-1 rename that could not land yet). Overwriting it
                # would hijack that org and re-parent its members. Surface the clash and skip;
                # the next sync retries once the clash is resolved.
                logger.warning(
                    f"Skipped organization {org_data.id}: slug '{org_data.slug}' is held by a live "
                    f"{slug_owner.source} org (id={slug_owner.id}, external_id={slug_owner.external_id}, "
                    f"api_key={slug_owner.api_key_id}). Resolve the slug clash to sync it."
                )
                continue

            await db.commit()
            synced += 1
        except Exception as e:
            await db.rollback()
            logger.warning(f"Skipped organization {org_data.id} (slug={org_data.slug}) during sync insert: {e}")

    if synced > 0:
        logger.info(f"Synced {synced} organizations from OrcaHub (api_key={api_key_id})")

    return synced


# role mapping: OrcaHub OrganizationRole → Orcastra system role (Authentik group)
_ORCAHUB_ROLE_MAP = {
    "PARTNER": "partner",
    "TENANT": "tenant",
    "USER": "tenant",  # legacy alias
}


async def _revoke_role_if_orphaned(db: AsyncSession, sync_svc, email: str) -> None:
    """
    Strip a user's role_tenant only if they no longer belong to ANY org.
    Orcastra roles are global system roles (one per user), so stripping them on leaving a
    single org would wrongly de-role a user still active in another org. Best-effort: the
    caller has already deleted the membership row (which is what actually cuts org-scope
    access), so a failed IdP revoke leaves only an inert role group, logged for cleanup.

    important: an integration never removes role_admin or role_partner here. It used to strip
    every role group, so pushing an Admin into a fresh org and removing them demoted the Admin.
    """
    try:
        await revoke_if_orphaned(db, sync_svc, email)
    except Exception as e:
        logger.warning(f"Authentik role revoke failed for offboarded user {email}: {e}")


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
            org = (await db.execute(
                select(Organization).where(
                    and_(
                        Organization.source == "orcahub",
                        Organization.external_id == m.organization_id,
                    )
                )
            )).scalar_one_or_none()
            if not org:
                logger.warning(f"Org not found for external_id={m.organization_id}, skipping member {m.email}")
                continue
            # important: the org is resolved purely from the caller-supplied external id, so a
            # key that owns nothing could name another integration's org and rewrite its
            # members, which pushes a global Authentik role change for those emails. Same
            # ownership rule as the organisation upsert above.
            if org.api_key_id and org.api_key_id != api_key_id:
                logger.warning(
                    "Refusing member sync into organization %s: owned by api key %s, caller is %s",
                    org.slug, org.api_key_id, api_key_id,
                )
                continue

            if m.action == "remove":
                # remove membership (idempotent: an already-absent member is a no-op success)
                existing = (await db.execute(
                    select(OrganizationMember).where(
                        and_(
                            OrganizationMember.organization_id == org.id,
                            OrganizationMember.user_email == m.email,
                        )
                    )
                )).scalar_one_or_none()
                if existing:
                    await db.delete(existing)
                    await db.commit()
                    removed += 1
                    # Revoke the global role only if this was the user's LAST membership.
                    await _revoke_role_if_orphaned(db, sync_svc, m.email)
                continue

            # upsert membership. map OrcaHub role → local member role.
            # Identity is (organization_id, user_email), so update-if-present / insert-else
            # is unambiguous. Per-member commit isolates a failing row from the rest.
            orcastra_role = _ORCAHUB_ROLE_MAP.get(m.role.upper(), "tenant")

            existing = (await db.execute(
                select(OrganizationMember).where(
                    and_(
                        OrganizationMember.organization_id == org.id,
                        OrganizationMember.user_email == m.email,
                    )
                )
            )).scalar_one_or_none()
            if existing:
                existing.role = orcastra_role
            else:
                db.add(OrganizationMember(
                    organization_id=org.id,
                    user_email=m.email,
                    role=orcastra_role,
                    invited_by="orcahub-sync",
                ))
            await db.commit()

            synced += 1
            upserted_emails.add(m.email)

            # push system role to Authentik
            # important: an integration may grant a role, never demote one or touch role_admin.
            # sync_user_role removed every other role group, so any key could demote an Admin.
            try:
                await apply_upsert_role(db, sync_svc, m.email, orcastra_role, api_key_id)
            except Exception as e:
                logger.warning(f"Authentik sync failed for {m.email} → {orcastra_role}: {e}")

        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to sync member {m.email}: {e}")

    if synced > 0 or removed > 0:
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


# Circuit breaker: no legitimate single sync removes anywhere near this many members. If a push
# would remove more than this (ex; an OrcaHub-side query regression), abort the whole reconcile
# and alert rather than mass-revoke access.
_RECONCILE_MAX_REMOVALS_PER_SYNC = 50


async def _reconcile_synced_members(
    db: AsyncSession,
    organizations: List["SyncOrganization"],
    members: List["SyncMember"],
    api_key_id: str,
    dry_run: bool,
) -> int:
    """
    Offboarding reconcile. OrcaHub is the source of truth and pushes a COMPLETE member roster per
    org it syncs, but never an explicit member-remove, so a member who left an org is otherwise
    never removed here and keeps org-scope access. For every org PRESENT in this push, remove the
    OrganizationMember rows whose email is not in the pushed roster for that org.

    Destructive (deleting a member cuts org-scope access and CASCADEs their TenantPolicyAssignment),
    so it is layered with guards:
      - global: an empty members[] is "no data this push" -> skip.
      - per-org FLOOR: an org present in the push whose roster contribution is EMPTY is suspicious
        (OrcaHub drops an emptied org from the push entirely, it never sends a bare org), so it is
        skipped, never purged. This is the primary protection against a mass delete.
      - CIRCUIT BREAKER: if total intended removals exceed _RECONCILE_MAX_REMOVALS_PER_SYNC, abort
        the whole run and alert (delete nothing).
      - dry_run: log intended removals without deleting (observe-only rollout).
      - per-ROW isolation on the delete pass, so one bad row cannot block the rest.
      - a user removed from their LAST org also has their global role revoked (best-effort).
    Email matching is exact, consistent with the case-sensitive UNIQUE(org, user_email) and the
    upsert write path. Returns the number of members actually removed (0 in dry-run).
    """
    from app.services.authentik_sync import get_authentik_sync

    if not members:
        return 0

    # Pass 1 (read-only): compute intended removals, applying the per-org empty-roster floor.
    intended = []  # (org_slug, member_id, email)
    for org_data in organizations:
        try:
            org = (await db.execute(
                select(Organization).where(
                    and_(
                        Organization.source == "orcahub",
                        Organization.external_id == str(org_data.id),
                    )
                )
            )).scalar_one_or_none()
            if not org:
                continue  # org not synced (ex; slug clash skipped it); nothing to reconcile
            # important: same ownership rule as the member upsert. Without it a key could name
            # another integration's organization and purge its roster.
            if org.api_key_id != api_key_id:
                logger.warning(
                    "Reconcile refused for organization %s: owned by api key %s, caller is %s",
                    org.slug, org.api_key_id, api_key_id,
                )
                continue

            desired_emails = {
                m.email for m in members
                if str(m.organization_id) == str(org_data.id) and m.action != "remove"
            }
            if not desired_emails:
                # important: never purge every member of an org from an empty roster - that is the
                # catastrophic false-positive. A legitimately emptied org drops out of the push.
                logger.warning(
                    f"Reconcile: org {org.slug} is present in the push with an empty roster; "
                    f"skipping (not purging its members)"
                )
                continue

            existing_members = (await db.execute(
                select(OrganizationMember).where(OrganizationMember.organization_id == org.id)
            )).scalars().all()
            for member in existing_members:
                if member.user_email not in desired_emails:
                    intended.append((org.slug, member.id, member.user_email))
        except Exception as e:
            logger.warning(f"Member reconcile scan skipped org {org_data.id}: {e}")

    if not intended:
        return 0

    if len(intended) > _RECONCILE_MAX_REMOVALS_PER_SYNC:
        logger.error(
            f"Member reconcile ABORTED: {len(intended)} intended removals exceed the safety cap "
            f"({_RECONCILE_MAX_REMOVALS_PER_SYNC}); deleting nothing. Investigate the OrcaHub push "
            f"(api_key={api_key_id})"
        )
        return 0

    if dry_run:
        for slug, _mid, email in intended:
            logger.info(f"Reconcile DRY-RUN: would remove member {email} from org {slug}")
        logger.info(
            f"Member reconcile dry-run: {len(intended)} member(s) would be removed (api_key={api_key_id})"
        )
        return 0

    # Pass 2: delete, with PER-ROW isolation so one failure cannot block the rest.
    sync_svc = get_authentik_sync()
    removed = 0
    for slug, mid, email in intended:
        try:
            member = await db.get(OrganizationMember, mid)
            if member is None:
                continue  # already gone (concurrent sync); idempotent
            await db.delete(member)
            await db.commit()
            removed += 1
            logger.info(f"Reconcile: removed member {email} from org {slug} (api_key={api_key_id})")
            await _revoke_role_if_orphaned(db, sync_svc, email)
        except Exception as e:
            await db.rollback()
            logger.warning(f"Reconcile: failed to remove member {email} from org {slug}: {e}")

    if removed > 0:
        logger.info(f"Member reconcile removed {removed} departed member(s) (api_key={api_key_id})")
    return removed


# ============================================
# API Key Validation Dependency
# ============================================

def _api_key_has_expired(expires_at: Any, now: Optional[datetime] = None) -> bool:
    """Whether a stored API key expiry has passed.

    important: only None means the key never expires. The old truthiness test read an empty
    string as never-expiring, and an offset-bearing or unparseable value raised out of the auth
    path. A naive value is UTC, as create_api_key writes it. A value that cannot be read counts
    as expired.
    """
    if expires_at is None:
        return False
    try:
        moment = datetime.fromisoformat(str(expires_at).strip())
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return True
    return (now or datetime.utcnow()) > moment


def _api_key_is_unusable(metadata: Dict[str, Any]) -> bool:
    """A revoked, inactive or expired key. Its integration is gone, so nothing calls it back."""
    return bool(
        metadata.get("is_revoked")
        or not metadata.get("is_active", True)
        or _api_key_has_expired(metadata.get("expires_at"))
    )


async def validate_api_key(
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
    x_api_key_secret: str = Header(..., alias="X-API-Key-Secret"),
) -> Dict[str, Any]:
    """
    Dependency to validate API key from headers.
    Returns the API key metadata if valid.
    """
    digest = _api_key_digest(x_api_key_id, x_api_key_secret)
    metadata = _cached_key_metadata(digest)

    if metadata is None:
        try:
            metadata = await run_blocking(
                _validate_api_key_blocking,
                x_api_key_id,
                x_api_key_secret,
                timeout=VALIDATE_KEY_TIMEOUT,
                label=f"validate_api_key[{x_api_key_id}]",
                executor=auth_executor,
            )
        except BlockingTimeout:
            logger.warning(
                f"API key validation timed out after {VALIDATE_KEY_TIMEOUT}s "
                f"(key={x_api_key_id})"
            )
            raise HTTPException(
                status_code=503, detail="Authentication backend unavailable"
            ) from None
        except HTTPException as exc:
            # get_api_key_from_vault reports a missing key as 404. Externally that is an
            # authentication failure, and must not leak whether the id exists.
            if exc.status_code == 404:
                raise HTTPException(status_code=401, detail=INVALID_API_KEY_MESSAGE) from None
            raise
        _store_key_metadata(digest, x_api_key_id, metadata)

    # note: state checks run on every request, cache hit or not, straight from in-memory
    # metadata. They cost nothing and keep expiry exact even while the entry is cached.

    # Check if revoked
    if metadata.get("is_revoked", False):
        raise HTTPException(status_code=401, detail="API key has been revoked")

    # Check if active
    if not metadata.get("is_active", True):
        raise HTTPException(status_code=401, detail="API key is not active")

    # Check expiration
    if _api_key_has_expired(metadata.get("expires_at")):
        raise HTTPException(status_code=401, detail="API key has expired")

    _schedule_last_used_update(x_api_key_id)

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
    requested_meta = {
        "name": request.name,
        "app_name": request.app_name,
        "app_type": request.app_type,
        "cluster_permissions": [p.model_dump() for p in request.cluster_permissions],
    }

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
            _audit_api_key(
                API_KEY_CREATE, "-", _rbac, requested_meta,
                result="denied", error_code="cluster_out_of_scope",
            )
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted to grant access to clusters: {sorted(invalid)}",
            )
    try:
        await _refuse_a_grant_past_the_project_bound(db, _rbac.email, request.cluster_permissions)
    except HTTPException as exc:
        if exc.status_code == 403:
            _audit_api_key(
                API_KEY_CREATE, "-", _rbac, requested_meta,
                result="denied", error_code="project_out_of_bound",
            )
        raise

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
        _audit_api_key(API_KEY_CREATE, key_id, _rbac, metadata)

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


def _assert_key_in_scope(metadata: Dict[str, Any], allowed: Optional[set[str]]) -> None:
    """The overlap rule list_api_keys uses. 404 so an out-of-scope id reads as a missing one."""
    if not metadata:
        raise HTTPException(status_code=404, detail="API key not found")
    if allowed is None:
        return
    perms = metadata.get("cluster_permissions", []) or []
    key_clusters = {p.get("cluster_id", "") for p in perms if isinstance(p, dict)}
    if not (key_clusters & allowed):
        raise HTTPException(status_code=404, detail="API key not found")


API_KEY_CREATE = "integration.api_key.create"
API_KEY_PERMISSIONS_UPDATE = "integration.api_key.permissions.update"
API_KEY_REVOKE = "integration.api_key.revoke"
API_KEY_ROTATE = "integration.api_key.secret.rotate"
API_KEY_DELETE = "integration.api_key.delete"


def _audit_grants(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The cluster grants of a key, reduced to the three fields an auditor needs."""
    return [
        {
            "cluster_id": p.get("cluster_id"),
            "projects": p.get("projects"),
            "permissions": p.get("permissions"),
        }
        for p in (metadata.get("cluster_permissions") or [])
        if isinstance(p, dict)
    ]


def _audit_api_key(
    action: str,
    key_id: str,
    actor: AuthenticatedUser,
    metadata: Dict[str, Any],
    *,
    result: str = "success",
    error_code: Optional[str] = None,
    before: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Record an API key lifecycle event.

    important: built from an allowlist. The secret, its hash and app_url never reach the log. The
    key id travels as target_id on purpose, because the audit sanitiser blanks any detail whose
    name contains api_key.
    """
    grants = _audit_grants(metadata)
    try:
        activity_logger.log_activity(
            action=action,
            target_type="integration_api_key",
            target_id=key_id,
            result=result,
            error_code=error_code,
            details={
                "actor_email": getattr(actor, "email", None),
                "name": metadata.get("name"),
                "app_name": metadata.get("app_name"),
                "app_type": metadata.get("app_type"),
                "expires_at": metadata.get("expires_at"),
                "clusters": sorted({str(g.get("cluster_id") or "") for g in grants}),
            },
            before_state={"cluster_permissions": before} if before is not None else None,
            after_state={"cluster_permissions": grants} if before is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 - an audit write must never fail the action it records
        logger.warning("Could not audit %s for %s: %s", action, key_id, exc)


def _read_api_key_metadata_blocking(key_id: str) -> Dict[str, Any]:
    """A key's metadata document, or {} when it cannot be read. The cached /metadata path only."""
    try:
        return get_vault_integrations_client().vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
        ) or {}
    except Exception:
        return {}


async def _run_org_keys(db: AsyncSession, email: str) -> set[str]:
    """Integration keys carried by the organizations this user runs. See services.cluster_parties."""
    return await cluster_parties.run_org_keys(db, email)


def _key_clusters(metadata: Dict[str, Any]) -> set[str]:
    return {
        str(p.get("cluster_id") or "")
        for p in metadata.get("cluster_permissions", []) or []
        if isinstance(p, dict)
    }


async def _keys_the_caller_manages(
    db: AsyncSession, user: AuthenticatedUser, keys: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The keys, out of ones already in the caller's cluster scope, that this user may manage.

    important: cluster overlap alone let a Partner on a cluster bound to another organization read,
    revoke and delete that organization's keys, which breaks its OrcaHub sync and provisioning. A
    key granting a cluster another party holds is managed only by its creator, or by whoever runs
    an organization that key syncs. An Admin, and a key whose clusters are all the caller's own,
    keep the old rule.
    """
    from app.core.rbac import Role, get_highest_role, resolve_roles

    if not keys or get_highest_role(resolve_roles(user)) == Role.ADMIN:
        return keys
    email = (user.email or "").lower()
    mine = [k for k in keys if str(k.get("created_by") or "").lower() == email]
    rest = [k for k in keys if str(k.get("created_by") or "").lower() != email]
    if not rest:
        return keys
    held = await _clusters_held_by_another_party(
        db, user.email, set().union(*(_key_clusters(k) for k in rest))
    )
    linked = await _run_org_keys(db, user.email) if held else set()
    kept = [k for k in rest if not (_key_clusters(k) & held) or str(k.get("id") or "") in linked]
    return [k for k in keys if k in mine or k in kept]


async def _authorize_api_key(key_id: str, user: AuthenticatedUser, db: AsyncSession) -> Dict[str, Any]:
    """Return an API key's metadata only if the caller may manage it, else 404.

    important: `list_api_keys` filters by cluster overlap and by who manages the key, and every
    single-key handler takes `key_id` straight from the path, so both apply here too and the two
    surfaces agree. The read runs in a thread (asyncio.to_thread keeps the request's org scope)
    because Vault answers synchronously.
    """
    metadata = await asyncio.to_thread(_read_api_key_metadata_blocking, key_id)
    _assert_key_in_scope(metadata, get_current_allowed_clusters())
    if not await _keys_the_caller_manages(db, user, [metadata]):
        raise HTTPException(status_code=404, detail="API key not found")
    return metadata


async def _clusters_held_by_another_party(
    db: AsyncSession, email: str, cluster_ids: set[str]
) -> set[str]:
    """Clusters some holder of which is not on the caller's side. See services.cluster_parties."""
    return await cluster_parties.clusters_held_by_another_party(db, email, cluster_ids)


async def _refuse_a_grant_past_the_project_bound(
    db: AsyncSession,
    caller_email: str,
    grants: List[ClusterPermission],
    already_granted: Optional[Dict[str, Tuple[set[str], int]]] = None,
) -> None:
    """A Partner may not give a key more of a cluster than they can see there.

    important: creation and update checked cluster ids only. On a cluster another party also
    holds, a Partner could name that party's project, or '*', and the key then acted on it
    through /raw-proxy, /proxy and /provision-tenant-access. A cluster held only by the caller's
    side is unchanged, so the '*' grant an OrcaHub connection carries still mints there. What a
    key already holds on a cluster (its projects and its permission level) is carried over on
    update, so an edit cannot widen a key but never breaks a running one. Admin is not bound.
    """
    if get_current_allowed_clusters() is None:
        return
    held_elsewhere = await _clusters_held_by_another_party(
        db, caller_email, {grant.cluster_id for grant in grants}
    )
    kept = already_granted or {}

    def carried_over(grant: ClusterPermission, project: str) -> bool:
        projects, level = kept.get(grant.cluster_id, (set(), -1))
        return project in projects and granted_level(grant.permissions) <= level

    refused = sorted({
        f"{grant.cluster_id}/{project}"
        for grant in grants
        if grant.cluster_id in held_elsewhere
        for project in grant.projects
        if not carried_over(grant, project)
        and (project == "*" or not project_is_visible(grant.cluster_id, project))
    })
    if refused:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Not permitted to grant these projects: {refused}. Another organization also "
                "holds that cluster, so a key may name only the projects you can see there. "
                "A project the key already holds is kept at its current permission level. "
                "An administrator can issue a wider key."
            ),
        )


@router.get(
    "/api-keys",
    response_model=APIKeyListResponse,
    summary="List all API keys",
    description="Get a list of all API keys for external integrations.",
    # important: the router admits tenants, and a key's grants and mappings are partner data.
    dependencies=[Depends(require_partner())],
)
async def list_api_keys(
    fresh: bool = False,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """List API keys visible to the current user (admin sees all, others see org-scoped)."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Scoped cache key so each org gets its own cached list
        cache = get_cache_manager()
        cache_key = get_scope_cache_key("integrations:api_keys:list")
        cached_data = None if fresh else cache.get("api_responses", cache_key)
        
        if cached_data is None:
            all_metadata = await asyncio.to_thread(list_api_keys_from_vault, vault_client)
            
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
        # note: after the cache, because the cached list is shared by everyone with the same
        # cluster scope while who manages a key depends on the caller.
        all_metadata = await _keys_the_caller_manages(db, user, all_metadata)
        
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
            user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, metadata["id"])
            
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
    dependencies=[Depends(require_partner())],
)
async def get_api_key(
    key_id: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Get API key details."""
    metadata = await _authorize_api_key(key_id, user, db)
    vault_client = get_vault_integrations_client()
    
    try:
        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        
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
    db: AsyncSession = Depends(get_db),
):
    """Update API key cluster permissions."""
    current = await _authorize_api_key(key_id, _rbac, db)

    # important: creation validates the requested clusters against the caller's own set
    # (see create_api_key), and this path did not, so a partner could create a key scoped to
    # their cluster and then widen it to the whole fleet in a second call. Same rule, both doors.
    before = _audit_grants(current)
    requested_meta = {**current, "cluster_permissions": [p.model_dump() for p in permissions]}
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        requested = {p.cluster_id for p in permissions}
        invalid = requested - allowed
        if invalid:
            _audit_api_key(
                API_KEY_PERMISSIONS_UPDATE, key_id, _rbac, requested_meta,
                result="denied", error_code="cluster_out_of_scope", before=before,
            )
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted to grant access to clusters: {sorted(invalid)}",
            )
    # note: the last grant per cluster wins, as it does in assert_cluster_permission and the
    # proxies, and the permission default mirrors theirs.
    already_granted = {
        p.get("cluster_id", ""): (
            set(p.get("projects") or ["default"]),
            granted_level(p.get("permissions", ["read"])),
        )
        for p in (current.get("cluster_permissions") or [])
        if isinstance(p, dict)
    }
    try:
        await _refuse_a_grant_past_the_project_bound(db, _rbac.email, permissions, already_granted)
    except HTTPException as exc:
        if exc.status_code == 403:
            _audit_api_key(
                API_KEY_PERMISSIONS_UPDATE, key_id, _rbac, requested_meta,
                result="denied", error_code="project_out_of_bound", before=before,
            )
        raise

    vault_client = get_vault_integrations_client()
    
    try:
        metadata, secret_hash = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id)
        
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
        # important: also drop the process-local validation cache, otherwise the old
        # cluster_permissions keep authorising proxy calls until its TTL expires.
        evict_api_key_cache(key_id)
        _audit_api_key(API_KEY_PERMISSIONS_UPDATE, key_id, _rbac, metadata, before=before)

        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
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
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key (immediate revocation)."""
    await _authorize_api_key(key_id, _rbac, db)
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, _ = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id)
        
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
        # important: revocation must be immediate. Without this the key keeps working from
        # the process-local validation cache until its TTL expires.
        evict_api_key_cache(key_id)
        _audit_api_key(API_KEY_REVOKE, key_id, _rbac, metadata)

        logger.info(f"API key {key_id} revoked")
        
        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        return APIKeyInfo(**metadata, user_mappings_count=len(user_mappings))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api-keys/{key_id}/rotate-secret",
    response_model=APIKeyCreateResponse,
    summary="Rotate an API key's secret",
    description=(
        "Issue a new secret for the same key id. The old secret stops working at once, on every "
        "worker, and the new one is shown only once."
    ),
)
async def rotate_api_key_secret(
    key_id: str,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Rotate a key's secret in place.

    important: the key id is what every synced organization records as its owner, so rotating by
    minting a new key left all of them refusing the new one and stopped their sync. Keeping the
    id keeps the orgs, the member push and the key's party exactly as they were.
    """
    await _authorize_api_key(key_id, _rbac, db)
    vault_client = get_vault_integrations_client()
    metadata, _hash = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id)
    if _api_key_is_unusable(metadata):
        raise HTTPException(
            status_code=409,
            detail="A revoked, inactive or expired key cannot be rotated. Create a new key instead.",
        )
    # important: everything that could fail runs before the write. Once the new secret is stored
    # the old one is dead, and a failure after it would leave a secret nobody was ever shown.
    user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
    try:
        key_info = APIKeyInfo(**metadata, user_mappings_count=len(user_mappings))
    except ValidationError:
        raise HTTPException(
            status_code=409,
            detail="This key's stored grants no longer validate. Update its permissions, then rotate it.",
        )
    # important: revoking, deleting or narrowing a key someone else minted hands the caller nothing,
    # but a rotation hands them a working secret. A non-admin gets one only for a key they minted
    # (they held its secret already) or a key they could mint now: every cluster inside their scope
    # and every project inside their bound.
    allowed = get_current_allowed_clusters()
    if allowed is not None and str(metadata.get("created_by") or "").lower() != (_rbac.email or "").lower():
        outside = sorted({grant.cluster_id for grant in key_info.cluster_permissions} - set(allowed))
        if outside:
            raise HTTPException(
                status_code=403,
                detail=f"This key also grants {outside}, which you cannot reach. An administrator can rotate it.",
            )
        await _refuse_a_grant_past_the_project_bound(db, _rbac.email, key_info.cluster_permissions)
    key_secret, secret_hash = generate_api_key_secret()
    try:
        await asyncio.to_thread(
            vault_client.vault_kv_put,
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret",
            {"hash": secret_hash, "raw": key_secret},
        )
    except Exception as e:
        # note: the write may have landed before its reply was lost, so every worker still drops
        # its cached copy and checks Vault again
        evict_api_key_cache(key_id)
        logger.error(f"Failed to rotate the secret of API key {key_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="The secret rotation did not confirm. Rotate again before using this key.",
        )
    # note: the validation cache is keyed by id and secret, and the tombstone makes every worker
    # drop its copy, so the old secret fails on the next request wherever it lands
    evict_api_key_cache(key_id)
    cache = get_cache_manager()
    cache.delete_pattern("api_responses", "integrations:api_keys:list*")
    cache.delete("api_responses", f"integrations:api_key:{key_id}")
    _audit_api_key(API_KEY_ROTATE, key_id, _rbac, metadata)
    logger.info(f"API key {key_id} secret rotated")
    return APIKeyCreateResponse(api_key=key_info, api_key_id=key_id, api_key_secret=key_secret)


@router.delete(
    "/api-keys/{key_id}",
    summary="Delete API key",
    description="Permanently delete an API key and all associated user mappings.",
)
async def delete_api_key(
    key_id: str,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Delete an API key permanently."""
    await _authorize_api_key(key_id, _rbac, db)
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify it exists
        metadata, _secret_hash = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id)
        
        # Delete from Vault
        delete_api_key_from_vault(vault_client, key_id)

        # Clear all scoped cache variants
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        evict_api_key_cache(key_id)
        _audit_api_key(API_KEY_DELETE, key_id, _rbac, metadata)

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
    db: AsyncSession = Depends(get_db),
):
    """Add or update a user mapping for an API key."""
    await _authorize_api_key(key_id, _rbac, db)
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify API key exists
        await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id)
        
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
        
        # Mapping data rides along in the cached key metadata, exactly as the four sibling
        # handlers assume when they clear this same pattern.
        get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
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
    dependencies=[Depends(require_partner())],
)
async def list_user_mappings(
    key_id: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """List all user mappings for an API key."""
    await _authorize_api_key(key_id, user, db)
    vault_client = get_vault_integrations_client()
    
    try:
        return await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        
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
        # important: unauthenticated callers reach this, so its Vault reads run on the bounded
        # auth pool. Inline they froze the event loop, and /health with it, for as long as a
        # degraded Vault took to answer.
        metadata, secret_hash = await run_blocking(
            get_api_key_from_vault,
            vault_client,
            request.api_key_id,
            timeout=VALIDATE_KEY_TIMEOUT,
            label="validate_endpoint",
            executor=auth_executor,
            max_in_flight=EXTERNAL_AUTH_MAX_IN_FLIGHT,
        )
        
        # Verify secret
        if not verify_api_key_secret(request.api_key_secret, secret_hash):
            return APIKeyValidateResponse(valid=False, error=INVALID_API_KEY_MESSAGE)
        
        # Check if revoked
        if metadata.get("is_revoked", False):
            return APIKeyValidateResponse(valid=False, error="API key revoked")
        
        # Check if active
        if not metadata.get("is_active", True):
            return APIKeyValidateResponse(valid=False, error="API key not active")
        
        # Check expiration
        if _api_key_has_expired(metadata.get("expires_at")):
            return APIKeyValidateResponse(valid=False, error="API key expired")
        
        # Valid!
        raw_permissions = metadata.get("cluster_permissions", [])
        
        # Enrich cluster permissions with cluster names from Vault
        enriched_permissions = []
        cluster_ids = sorted({
            perm.get("cluster_id") for perm in raw_permissions
            if isinstance(perm, dict) and perm.get("cluster_id")
        })
        try:
            cluster_names = await run_blocking(
                _cluster_display_names_blocking,
                cluster_ids,
                timeout=VALIDATE_KEY_TIMEOUT,
                label="validate_cluster_names",
                executor=auth_executor,
                max_in_flight=EXTERNAL_AUTH_MAX_IN_FLIGHT,
            )
        except Exception as e:  # noqa: BLE001 - names are decoration, the ids still answer
            logger.warning("Could not enrich cluster names: %s", sanitize_error(str(e)))
            cluster_names = {}
        
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
        return APIKeyValidateResponse(valid=False, error=INVALID_API_KEY_MESSAGE)
    except Exception as e:
        # note: a Vault stall (BlockingTimeout or BlockingPoolFull) lands here too.
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

        # ── Offboarding reconcile (flag-gated, default off): drop members no longer in the
        #    pushed roster for each synced org. See _reconcile_synced_members for the guards. ──
        _settings = get_settings()
        if _settings.member_reconcile_enabled and request.organizations:
            result.members_removed += await _reconcile_synced_members(
                db, request.organizations, request.members, api_key_id,
                dry_run=_settings.member_reconcile_dry_run,
            )

        # ── Store callback URL in Vault API key metadata ──
        if request.app_url:
            # note: Vault write, so off the loop like the rest of this handler. Best effort:
            # a bookkeeping write must not fail or slow the sync.
            try:
                await run_blocking(
                    _store_app_url_in_vault,
                    api_key_id,
                    request.app_url,
                    timeout=VALIDATE_KEY_TIMEOUT,
                    label=f"store_app_url[{api_key_id}]",
                    executor=auth_executor,
                )
            except Exception as e:
                logger.warning(f"Could not store app_url for {api_key_id}: {e}")
        
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
                
                result.clusters = await _gather_cluster_info(cert_manager, cluster_ids)
        
        project_grants = _key_project_grants(api_key)
        if request.include_projects:
            result.projects = await _gather_per_cluster(
                partial(_fetch_cluster_projects, grants=project_grants),
                lxd_service, cluster_ids, "projects",
            )

        if request.include_instances:
            result.instances = await _gather_per_cluster(
                partial(_fetch_cluster_instances, grants=project_grants),
                lxd_service, cluster_ids, "instances",
            )

        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _integration_hardware_totals(
    lxd_service: LXDService, allowed_clusters: set
) -> Dict[str, int]:
    """Real hardware totals for the clusters an API key may see.

    important: this reaches the clusters, so it is cached and threaded. The result is keyed by
    the cluster set rather than by the key id, so two keys with the same access share one
    result. Unreachable clusters contribute nothing instead of blocking the response: the
    shared circuit breaker returns them immediately.

    The GPU total counts compute GPUs, matching every internal surface. This endpoint used to
    return a hardcoded zero for every total, which told partners their GPUs did not exist.
    """
    empty = {"cpu": 0, "memory": 0, "storage": 0, "gpu": 0, "gpu_cards": 0}
    if not allowed_clusters:
        return empty

    cache = get_cache_manager()
    cache_key = "integration:hardware:" + ",".join(sorted(allowed_clusters))
    cached = cache.get("api_responses", cache_key)
    if cached is not None:
        return cached

    def _collect() -> Dict[str, int]:
        totals = dict(empty)
        for cluster_id in sorted(allowed_clusters):
            try:
                status = lxd_service.check_node_status(cluster_id)
            except Exception as e:
                logger.debug(f"Could not read hardware for {cluster_id}: {e}")
                continue
            resources = status.resources if status.connected else None
            if not resources:
                continue
            totals["cpu"] += resources.cpu_total
            totals["memory"] += resources.memory_total
            totals["storage"] += resources.storage_total
            totals["gpu"] += resources.gpu_total
            totals["gpu_cards"] += resources.gpu_cards_total
        return totals

    totals = await asyncio.to_thread(_collect)
    cache.set("api_responses", cache_key, totals, ttl=120)
    return totals


@external_router.get(
    "/regions",
    summary="Get integration region coordinates",
    description="Return non-sensitive geographic region coordinates for clusters allowed by the API key.",
)
async def get_integration_regions(
    api_key: Dict[str, Any] = Depends(validate_api_key),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Expose real cluster location metadata to external integrations using API-key auth."""
    try:
        settings = get_settings()
        allowed_clusters = {
            permission["cluster_id"] for permission in api_key.get("cluster_permissions", [])
        }
        regions: Dict[str, Dict[str, Any]] = {}

        if settings.vault_enabled and settings.vault_token:
            # note: proxy_executor, not auth_executor. A loop over many clusters must not hold
            # one of the few threads that authenticate every request.
            try:
                regions = await run_blocking(
                    _region_rows_blocking,
                    allowed_clusters,
                    timeout=SYNC_CLUSTER_TIMEOUT,
                    label="integration_regions",
                    executor=proxy_executor,
                    max_in_flight=PROXY_MAX_IN_FLIGHT,
                )
            except (BlockingTimeout, BlockingPoolFull):
                raise HTTPException(status_code=503, detail=REGIONS_UNAVAILABLE) from None

        totals = await _integration_hardware_totals(lxd_service, allowed_clusters)

        return {
            "regions": list(regions.values()),
            "total_nodes": sum(region["node_count"] for region in regions.values()),
            # note: this endpoint reports hardware, not workload. Instance counts come from a
            # different source and are not enumerated here, so the key stays 0 rather than
            # carrying a number this handler cannot support.
            "total_instances": 0,
            "total_cpu": totals["cpu"],
            "total_memory": totals["memory"],
            "total_storage": totals["storage"],
            "total_gpu": totals["gpu"],
            "total_gpu_cards": totals["gpu_cards"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error loading integration regions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _envelope_error(exc: BaseException) -> str:
    """The `error` text of a proxy envelope, with hosts, URLs, paths and secrets removed.

    important: the envelope is answered with HTTP 200, so the application's 5xx sanitiser never
    sees it, and the raw exception text named the cluster's address, port and TLS detail. Only
    `error` is rewritten. `data` is LXD's own answer and the integrator reads refusals from it.
    """
    return sanitize_error(str(exc), limit=1000)


# note: per worker, like the pool it guards. One slow cluster may hold at most this many proxy
# threads, so a cluster that reads slowly (a read no longer trips the breaker) cannot fill the
# shared pool and fail every other cluster's calls and every concurrent /sync.
CLUSTER_PROXY_SLOTS = max(1, PROXY_POOL_WORKERS // 2)
# note: LXD holds GET /operations/<id>/wait open for `timeout` seconds. Past the pooled read timeout
# (8 s) that is a 504 and a thread held for nothing, so the wait is answered earlier with the
# operation as it stands and the caller polls, which the integrator already does.
OPERATION_WAIT_CAP_SECONDS = 5
# note: a healthy cluster answers in well under a second, so a burst from the integrator (it sends
# 4 to 6 reads to one cluster in parallel) waits its turn briefly. A slot that stays taken this long
# means the cluster is slow, and the caller gets 503 "busy" inside its own 8 s read timeout.
CLUSTER_SLOT_WAIT_SECONDS = 5.0
_OPERATION_WAIT_PATH = re.compile(r"^/operations/[^/]+/wait/?$")
_cluster_slots: Dict[tuple, asyncio.Semaphore] = {}


class NeverSent(BlockingTimeout):
    """The call's cap passed while it was still queued, so it was dropped and never reached the cluster."""


async def _run_unless_abandoned(fn: Any, *args: Any, on_finish: Any = None, **kwargs: Any) -> Any:
    """run_blocking, except that a job still queued when its caller gave up is dropped, never run.

    important: the caller has already answered by then, and a late request (possibly a write)
    must not reach the cluster. Such a timeout is raised as NeverSent, so the caller can say the
    request was not sent and leave the cluster's breaker alone. on_finish runs exactly once,
    when the job ends or when it is dropped.
    """
    state = {"started": False, "abandoned": False}
    lock = threading.Lock()

    def held(*a: Any, **k: Any) -> Any:
        with lock:
            if state["abandoned"]:
                return None
            state["started"] = True
        try:
            return fn(*a, **k)
        finally:
            if on_finish:
                on_finish()

    try:
        return await run_blocking(held, *args, **kwargs)
    except BaseException as exc:
        with lock:
            if not state["started"]:
                state["abandoned"] = True
            abandoned = state["abandoned"]
        if abandoned:
            if on_finish:
                on_finish()
            if isinstance(exc, BlockingTimeout):
                raise NeverSent(exc.label, exc.timeout) from exc
        raise


async def _run_for_cluster(cluster_id: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """run_blocking, holding one of the cluster's proxy slots until the worker thread finishes.

    important: released by the thread, not by the await. run_blocking stops waiting at its cap
    while the thread reads on until its socket gives up, and releasing on the await would let one
    slow cluster take a fresh slot for every call that outlived the cap.
    """
    loop = asyncio.get_running_loop()
    slots = _cluster_slots.setdefault((id(loop), cluster_id), asyncio.Semaphore(CLUSTER_PROXY_SLOTS))
    try:
        await asyncio.wait_for(slots.acquire(), CLUSTER_SLOT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"Cluster {cluster_id} is busy with other requests, retry shortly",
            headers={"Retry-After": "2"},
        ) from None
    released = threading.Event()
    lock = threading.Lock()

    def release_once() -> None:
        with lock:
            if released.is_set():
                return
            released.set()
        loop.call_soon_threadsafe(slots.release)

    return await _run_unless_abandoned(fn, *args, on_finish=release_once, **kwargs)


def _capped_operation_wait(base_path: str, method: str, query_params: Dict[str, str]) -> None:
    """Bound the `timeout` of an operation wait below the pooled read timeout, in place."""
    if method != "GET" or not _OPERATION_WAIT_PATH.match(base_path):
        return
    try:
        requested = int(float(query_params.get("timeout", "")))
    except (ValueError, OverflowError):
        requested = -1
    capped = requested if 0 <= requested <= OPERATION_WAIT_CAP_SECONDS else OPERATION_WAIT_CAP_SECONDS
    query_params["timeout"] = str(capped)


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

        # note: validated out here in the outer try so it surfaces as a real HTTP 400. Raised
        # inside the inner block it would be swallowed by the generic handler and downgraded
        # to HTTP 200 carrying status_code 500.
        if method not in ALLOWED_METHODS:
            raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

        # important: out here in the outer try for the same reason, so a refusal stays a real
        # 400 or 403 and the session is never called.
        assert_proxy_path_allowed(request.path)
        assert_server_state_unchanged(request.path, method)
        assert_project_scope_allows_path(request.path, method, allowed_projects)
        assert_body_within_grant(request.path, method, request.body, allowed_projects)

        required_permission = "read"

        if method in ["POST", "PUT", "PATCH"]:
            required_permission = "write"
        elif method == "DELETE":
            required_permission = "admin"
        
        # note: shared with the image and ISO upload routers. An empty grant raised ValueError
        # here and surfaced as a 500; it is a 403 now. See app/core/api_key_permissions.py.
        assert_cluster_permission(api_key, request.cluster_id, required_permission)
        
        # important: the breaker check sits AFTER the permission checks on purpose, so
        # response timing cannot be used to probe which clusters exist without permission.
        if health.is_tripped(request.cluster_id):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cluster {request.cluster_id} recently unreachable: "
                    f"{health.get_reason(request.cluster_id) or 'unknown'}"
                ),
            )

        # note: get_client blocks too. It revalidates the pooled connection with a live
        # request and may rebuild the client, so it needs the same offload as the verb call
        # below. Wrapping only the verb call would leave this on the event loop.
        try:
            client = await _run_for_cluster(
                request.cluster_id,
                lxd_service.connection_manager.get_client,
                request.cluster_id,
                timeout=PROXY_CONNECT_TIMEOUT,
                label=f"get_client[{request.cluster_id}]",
                executor=proxy_executor,
                max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
        except NeverSent:
            raise HTTPException(
                status_code=503,
                detail="The proxy is busy and the request was not sent, retry shortly",
                headers={"Retry-After": "2"},
            )
        except BlockingTimeout:
            health.record_failure(request.cluster_id, "connect timed out")
            raise HTTPException(
                status_code=503,
                detail=f"Cluster {request.cluster_id} is not reachable (connect timed out)",
            )
        except BlockingPoolFull:
            raise HTTPException(status_code=503, detail="Proxy saturated, retry shortly")

        if not client:
            # note: "not connected" alone covered a tripped breaker, a failed reconnect and an
            # untrusted certificate alike, and an integrator cannot act on any of them unnamed.
            reason = health.get_reason(request.cluster_id)
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cluster {request.cluster_id} is not connected: {reason}"
                    if reason
                    else f"Cluster {request.cluster_id} is not connected "
                    "(the connection could not be re-established)"
                ),
            )

        # important: the path and query come from the same split_proxy_path the guards above
        # decided on, so no spelling is judged in one form and sent in another. The path is
        # relative to client.api's /1.0 endpoint.
        base_path, query_params = split_proxy_path(request.path)

        # important: `project` is authorised above from request.project, so a copy of it
        # inside `path` is not the caller's to set. Dropping it closes a scope bypass: a key
        # allowed only on "default" could otherwise declare project="default" (passing the
        # check, and skipping the override below because it IS default) while sending
        # ?project=<other> in the path, which LXD honours because these certs are registered
        # unrestricted. Every other query key is legitimate passthrough and is left alone.
        # note: matched on the fully decoded name, which removes more than LXD would read as
        # `project` (`%2570roject` included) and never less.
        for key in [name for name in query_params if query_key_name(name) == "project"]:
            query_params.pop(key)

        # Add project parameter if not default
        if project and project != "default":
            query_params["project"] = project
        _capped_operation_wait(base_path, method, query_params)

        # Build endpoint URL directly using client's session
        endpoint = client.api._api_endpoint

        # important: encoded, never joined raw. A name decoded to `x&all-projects` or `a#` and
        # written back as it is adds a key LXD reads or turns the enforced project into a
        # fragment that requests never sends.
        full_path = base_path
        if query_params:
            full_path += "?" + urlencode(list(query_params.items()))

        full_url = f"{endpoint}{full_path}"
        
        # important: this reaches into client.api.session, which bypasses pylxd's _APINode and
        # therefore the timeout _APINode would have injected. The socket timeout is passed
        # explicitly, and the call runs off the event loop: an LXD endpoint that accepts TCP
        # and then goes silent must degrade this one cluster, never the whole process.
        try:
            response = await _run_for_cluster(
                request.cluster_id,
                raw_request,
                client.api.session,
                method,
                full_url,
                request.body,
                socket_timeout_for(client),
                timeout=RAW_PROXY_TIMEOUT,
                label=f"raw_proxy[{method} {request.cluster_id}]",
                executor=proxy_executor,
                max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
            health.clear(request.cluster_id)

            # Parse response
            status_code = response.status_code
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text if hasattr(response, 'text') else str(response)}

            # Log the operation for audit
            logger.info(
                f"Raw proxy: {method} {request.path} on {request.cluster_id}/{project} "
                f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')} "
                f"-> {status_code}"
            )

            # A raw call can be anything, so on a mutating verb the safe assumption is that
            # something a cached list describes has changed. The path is opaque here, which is
            # why this clears broadly: an unnecessary cache miss costs one refetch, whereas a
            # missed invalidation hides an external system's change for the whole TTL.
            if method not in ("GET", "HEAD", "OPTIONS") and 200 <= status_code < 300:
                get_cache_manager().invalidate_resource_cache(
                    "instances", "vms", "networks", "storage_volumes", "storage_pools",
                    "profiles", "projects", "monitoring",
                )

            return RawProxyResponse(
                status_code=status_code,
                data=data,
                headers={},
            )

        except NeverSent:
            raise HTTPException(
                status_code=503,
                detail="The proxy is busy and the request was not sent, retry shortly",
                headers={"Retry-After": "2"},
            )
        except BlockingTimeout as timeout_error:
            # important: a read that outlived the cap is a slow cluster, not an unreachable one.
            # Tripping the breaker for it turned one heavy list into sixty seconds of 503 for
            # every caller of the cluster, provisioning included. A write still trips it.
            if method not in ("GET", "HEAD"):
                health.record_failure(request.cluster_id, str(timeout_error))
            logger.warning(
                f"Raw proxy timeout: {method} {request.path} on {request.cluster_id}/{project} "
                f"after {RAW_PROXY_TIMEOUT}s"
            )
            return RawProxyResponse(
                status_code=504,
                data=None,
                error=_envelope_error(timeout_error),
            )
        except BlockingPoolFull as pool_error:
            return RawProxyResponse(
                status_code=503,
                data=None,
                error=_envelope_error(pool_error),
            )
        except HTTPException:
            # note: re-raised so a real transport status is never downgraded to a 200 envelope
            # by the generic handler below.
            raise
        except Exception as api_error:
            # important: the breaker tracks REACHABILITY, so only a transport failure may trip
            # it. It used to trip on any exception, which meant an LXD 4xx, a decode error or a
            # bug in our own response handling marked the cluster dead for 60s. That now costs
            # more than it did: connection.get_client consults the same breaker, so a wrongly
            # tripped cluster disappears from the dashboard's own read paths too.
            # important: a read timeout means TCP and TLS completed and LXD is still working, so
            # it answers 504 and leaves the breaker alone. Not isinstance(ReadTimeout): the
            # pooled session mounts Retry(read=0), so a GET read timeout arrives as
            # ConnectionError(MaxRetryError(ReadTimeoutError)) and a stalled body as
            # ConnectionError(ReadTimeoutError). is_read_timeout names every shape and still
            # refuses a connect failure, which trips the breaker below.
            if is_read_timeout(api_error):
                logger.warning(
                    f"Raw proxy read timeout: {method} {request.path} on "
                    f"{request.cluster_id}/{project}"
                )
                return RawProxyResponse(
                    status_code=504,
                    data=None,
                    error=_envelope_error(api_error),
                )
            if isinstance(api_error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                health.record_failure(request.cluster_id, str(api_error))
            logger.error(f"LXD API error: {api_error}")
            return RawProxyResponse(
                status_code=500,
                data=None,
                error=_envelope_error(api_error),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in raw proxy: {e}", exc_info=True)
        return RawProxyResponse(
            status_code=500,
            data=None,
            error=_envelope_error(e),
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
        
        # important: the breaker check sits AFTER the permission checks so response timing
        # cannot be used to probe which clusters exist without permission.
        if health.is_tripped(request.cluster_id):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cluster {request.cluster_id} recently unreachable: "
                    f"{health.get_reason(request.cluster_id) or 'unknown'}"
                ),
            )

        # important: create_instance sends params to LXD as the instance body, so a project-limited
        # key is held to the same body rule as the raw proxy: no other project, nothing host-reaching
        if operation == "create_instance":
            assert_body_within_grant("/1.0/instances", "POST", request.params or {}, allowed_projects)

        operation_id = None

        # Every branch below is a blocking pylxd call, and the wait=True ones poll an LXD
        # operation to completion. Inline on the loop that is the same failure mode as the
        # raw proxy, with a longer tail.
        try:
            result_data = await _run_unless_abandoned(
                _execute_proxy_operation,
                lxd_service,
                request.cluster_id,
                operation,
                request.params or {},
                request.project,
                allowed_projects,
                timeout=PROXY_OP_TIMEOUT,
                label=f"proxy_operation[{operation} {request.cluster_id}]",
                executor=proxy_executor,
                max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
            health.clear(request.cluster_id)
        except NeverSent:
            return ProxyResponse(
                success=False, error="The proxy is busy and the operation was not sent, retry shortly"
            )
        except BlockingTimeout as timeout_error:
            health.record_failure(request.cluster_id, str(timeout_error))
            logger.warning(
                f"Proxy operation timeout: {operation} on {request.cluster_id} "
                f"after {PROXY_OP_TIMEOUT}s"
            )
            # This path mutates cluster state on behalf of an external system, where nobody is
            # watching a UI to notice a stale list. Drop the cached reads it just invalidated.
            get_cache_manager().invalidate_resource_cache("instances", "vms", "monitoring")
            return ProxyResponse(success=False, error=_envelope_error(timeout_error))
        except BlockingPoolFull as pool_error:
            return ProxyResponse(success=False, error=_envelope_error(pool_error))
        
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
            error=_envelope_error(e),
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
        app_name_cache: Dict[str, str] = {}

        response = []
        for org in orgs:
            app_name = None
            if org.api_key_id:
                if org.api_key_id not in app_name_cache:
                    meta = await asyncio.to_thread(_read_api_key_metadata_blocking, org.api_key_id)
                    app_name_cache[org.api_key_id] = meta.get("app_name", "")
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


_UNUSABLE_ORG_LIST = "External app returned an unusable organization list"
REFRESH_RESPONSE_MAX_BYTES = 1024 * 1024


def _parse_refreshed_organizations(data: Any) -> List[SyncOrganization]:
    """The organization list a refresh fetched, under the same cap a pushed sync has.

    important: the body comes from a URL the integration chose. A malformed entry raised out of
    the handler as a 500, and an oversized list cost one commit per entry with no ceiling.
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=_UNUSABLE_ORG_LIST)
    orgs_data = data.get("organizations") or []
    if not isinstance(orgs_data, list) or len(orgs_data) > MAX_SYNC_ORGANIZATIONS:
        raise HTTPException(status_code=502, detail=_UNUSABLE_ORG_LIST)
    try:
        return [SyncOrganization(**entry) for entry in orgs_data]
    except (TypeError, ValidationError):
        raise HTTPException(status_code=502, detail=_UNUSABLE_ORG_LIST) from None


@router.post(
    "/refresh-organizations",
    summary="Refresh organizations from external app",
    description="Pull fresh organization data from connected external app (on-demand). Requires partner role.",
    dependencies=[Depends(require_partner())],
)
async def refresh_organizations(
    api_key_id: str = Query(
        ...,
        pattern=r"^[A-Za-z0-9_-]{1,128}$",
        description="API key ID of the external app to refresh from",
    ),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_partner()),
):
    """On-demand pull of organizations from external app via stored callback URL."""
    # note: the scope is read here on the loop and the check runs here too, so it never depends on
    # what a worker thread can see.
    allowed = get_current_allowed_clusters()
    try:
        metadata, secret_hash = await run_blocking(
            get_api_key_from_vault, get_vault_integrations_client(), api_key_id,
            timeout=VALIDATE_KEY_TIMEOUT, label=f"refresh_orgs[{api_key_id}]",
            executor=auth_executor,
        )
    except (BlockingTimeout, BlockingPoolFull):
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    _assert_key_in_scope(metadata, allowed)
    if not await _keys_the_caller_manages(db, user, [metadata]):
        raise HTTPException(status_code=404, detail="API key not found")
    # important: the member push and the webhook already skip such a key. Refreshing it would send
    # its stored credential hash to its callback URL and write the answer under its ownership.
    if _api_key_is_unusable(metadata):
        raise HTTPException(
            status_code=409,
            detail="This API key is revoked, inactive or expired, so it cannot be refreshed",
        )

    app_url = metadata.get("app_url")
    if not app_url:
        raise HTTPException(
            status_code=400,
            detail="No callback URL configured for this API key. Trigger a sync from the external app first."
        )

    # Call external app's organizations endpoint
    callback_url = f"{app_url.rstrip('/')}/api/external/organizations"
    try:
        resp = await guarded_request(
            "GET",
            callback_url,
            headers={
                "X-API-Key-ID": api_key_id,
                "X-API-Key-Secret-Hash": secret_hash,
            },
            max_bytes=REFRESH_RESPONSE_MAX_BYTES,
            max_redirects=0,
            require_https=callback_requires_https(app_url),
            allowed_networks=callback_networks(),
            content_type_prefix="application/json",
            raise_for_status=False,
            timeout=15.0,
        )
    except UnsafeURLError as e:
        logger.warning("Refresh refused the stored callback URL of api key %s: %s", api_key_id, e)
        raise HTTPException(status_code=502, detail="The stored callback URL is not allowed")
    except Exception as e:
        logger.error(
            "Failed to reach external app for api key %s: %s", api_key_id, sanitize_error(str(e))
        )
        raise HTTPException(status_code=502, detail="Failed to reach external app")
    if not 200 <= resp.status_code < 300:
        # note: status only. The body is whatever the stored URL answered, and the status in a
        # client-facing detail would make this endpoint a scanner of that URL.
        logger.error("External app returned HTTP %s for api key %s", resp.status_code, api_key_id)
        raise HTTPException(status_code=502, detail="External app returned an error")
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail=_UNUSABLE_ORG_LIST) from None

    sync_orgs = _parse_refreshed_organizations(data)
    if not sync_orgs:
        return {"message": "No organizations returned from external app", "organizations_synced": 0}

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
    rules: List[SyncPolicyRule] = Field(default_factory=list, max_length=MAX_SYNC_POLICY_RULES)
    assignments: List[SyncPolicyAssignment] = Field(
        default_factory=list, max_length=MAX_SYNC_MEMBERS
    )


class SyncPoliciesRequest(BaseModel):
    """Request body for syncing policies from OrcaHub."""
    policies: List[SyncPolicy] = Field(..., max_length=MAX_SYNC_POLICIES)


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
        metadata, stored_hash = await run_blocking(
            get_api_key_from_vault,
            vault_client,
            api_key_id,
            timeout=VALIDATE_KEY_TIMEOUT,
            label="external_policies_auth",
            executor=auth_executor,
            max_in_flight=EXTERNAL_AUTH_MAX_IN_FLIGHT,
        )
    except (BlockingTimeout, BlockingPoolFull):
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    except HTTPException:
        raise HTTPException(status_code=401, detail=INVALID_API_KEY_MESSAGE)

    # verify secret
    # important: plain sha256 never matches the HMAC every key since RC1 stores, so this answers
    # 401 to all current keys. A repair must also check each rule's cluster_id against the key grant.
    computed_hash = hashlib.sha256(api_key_secret.encode()).hexdigest()
    if not hmac.compare_digest(computed_hash, stored_hash):
        raise HTTPException(status_code=401, detail=INVALID_API_KEY_MESSAGE)

    if metadata.get("is_revoked"):
        raise HTTPException(status_code=403, detail="API key has been revoked")

    synced_count = 0
    errors = []

    for sync_policy in body.policies:
        try:
            # find org by external_id
            org_stmt = select(Organization).where(
                and_(
                    Organization.source == "orcahub",
                    Organization.external_id == sync_policy.org_external_id,
                    Organization.is_active == True,
                )
            )
            result = await db.execute(org_stmt)
            org = result.scalar_one_or_none()

            if not org:
                errors.append(f"Organization with external_id '{sync_policy.org_external_id}' not found")
                continue
            # important: the target org comes straight from the body, and this endpoint writes
            # tenant access policies, which are authorization data. Without binding the key to
            # the org, any valid key could write policies into another integration's tenants.
            # Reported as not-found so a caller cannot probe which external ids exist.
            # important: a NULL owner is not "anyone's". Tenant policies are authorization data.
            if org.api_key_id != api_key_id:
                logger.warning(
                    "Refusing policy sync into organization %s: owned by api key %s, caller is %s",
                    org.slug, org.api_key_id or "<none>", api_key_id,
                )
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

    # This path mutates cluster state on behalf of an external system, where nobody is
    # watching a UI to notice a stale list. Drop the cached reads it just invalidated.
    get_cache_manager().invalidate_resource_cache("instances", "projects", "monitoring")
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
        metadata, stored_hash = await run_blocking(
            get_api_key_from_vault,
            vault_client,
            api_key_id,
            timeout=VALIDATE_KEY_TIMEOUT,
            label="external_policies_auth",
            executor=auth_executor,
            max_in_flight=EXTERNAL_AUTH_MAX_IN_FLIGHT,
        )
    except (BlockingTimeout, BlockingPoolFull):
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    except HTTPException:
        raise HTTPException(status_code=401, detail=INVALID_API_KEY_MESSAGE)

    computed_hash = hashlib.sha256(api_key_secret.encode()).hexdigest()
    if not hmac.compare_digest(computed_hash, stored_hash):
        raise HTTPException(status_code=401, detail=INVALID_API_KEY_MESSAGE)

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
    # important: a provisioned grant names the one project that was bought. '*' would reach
    # every project on the cluster, including another organization's, and OrcaHub never sends it.
    project_name: str = Field(
        ..., min_length=1, max_length=63,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$",
        description="LXD project name",
    )
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


async def _refuse_a_project_another_party_holds(
    db: AsyncSession, api_key: Dict[str, Any], cluster_id: str, project_name: str
) -> None:
    """Provisioning may not grant a project another organization's policies already name.

    important: a '*' grant covers every project on the cluster, including another
    organization's. The rule written here widens the Partner bound of whoever minted the key
    (a new org is created_by them) and hands the listed emails that project. The key's own side
    is the organizations it synced or provisioned and those its creator belongs to or created.
    A project nobody names yet, which is every freshly created one, still provisions.

    note: org_scope.project_is_visible cannot stand in for this. The external router carries no
    user scope, so it answers True for every project here.
    """
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    holders = (await db.execute(
        select(Organization.id, Organization.api_key_id)
        .join(TenantAccessPolicy, TenantAccessPolicy.organization_id == Organization.id)
        .join(TenantPolicyRule, TenantPolicyRule.policy_id == TenantAccessPolicy.id)
        .where(and_(
            TenantPolicyRule.cluster_id == cluster_id,
            TenantPolicyRule.project_name == project_name,
        ))
        .distinct()
    )).all()
    if not holders:
        return
    key_id = api_key.get("id", "")
    creator = api_key.get("created_by") or ""
    own_orgs = await get_org_ids_for_email(creator, db) if creator else set()
    if any(org_key != key_id and org_id not in own_orgs for org_id, org_key in holders):
        raise HTTPException(
            status_code=403,
            detail=(
                f"No permission for project {project_name}: another organization holds it "
                f"on cluster {cluster_id}"
            ),
        )


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
    # important: this writes an OrganizationCluster row and grants operate/console from a
    # caller-supplied cluster_id, so it needs the same allow-list every other external
    # endpoint applies. Without it any valid key can bind another partner's cluster to its
    # own org and hand its tenants real access. Same check as revoke_tenant_access.
    cluster_permissions = {p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])}
    if body.cluster_id not in cluster_permissions:
        raise HTTPException(status_code=403, detail=f"No permission for cluster {body.cluster_id}")
    allowed_projects = cluster_permissions[body.cluster_id].get("projects", ["default"])
    if body.project_name not in allowed_projects and "*" not in allowed_projects:
        raise HTTPException(status_code=403, detail=f"No permission for project {body.project_name}")
    await _refuse_a_project_another_party_holds(db, api_key, body.cluster_id, body.project_name)

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
        # important: the lookup above folds api_key_id in, so a caller whose key does not own
        # an existing org simply misses it and falls through to this create. The database
        # then refuses on uq_org_source_external_id or ix_organizations_slug, which is the
        # right outcome by the wrong route: the caller gets a conflict about a slug when the
        # actual reason is that another integration owns the organisation. sync() states this
        # plainly at its own ownership guard, so state it here too rather than letting the
        # constraint speak for it. This denies nothing new, the constraints already did.
        owner_stmt = select(Organization).where(
            and_(
                Organization.source == "orcahub",
                Organization.external_id == body.organization_external_id,
            )
        )
        owner = (await db.execute(owner_stmt)).scalar_one_or_none()
        if owner is not None and owner.api_key_id and owner.api_key_id != api_key_id:
            logger.warning(
                "Refusing provision of organization %s (external_id=%s): owned by api key %s, "
                "caller is %s",
                owner.slug, body.organization_external_id, owner.api_key_id, api_key_id,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Organization '{owner.slug}' is owned by a different integration key. "
                    "A key may only provision for organizations it already owns, or ones no "
                    "key has claimed."
                ),
            )

        org = Organization(
            name=body.organization_name,
            slug=body.organization_slug,
            source="orcahub",
            external_id=body.organization_external_id,
            api_key_id=api_key_id,
            is_active=True,
            # important: created_by is NOT NULL. Omitting it made this INSERT fail, so a brand-new
            # buyer org silently received no ACL at purchase (500, no policy). Stamp the provisioning key.
            created_by=api_key.get("created_by") or "orcahub-provisioning",
        )
        db.add(org)
        try:
            await db.flush()
        except IntegrityError as exc:
            # important: the lookup above keys on (source, external_id, api_key_id), but slug
            # carries its own unique index. A slug already held by a different external_id
            # misses that lookup and violates ix_organizations_slug on INSERT, which escaped
            # as a 500 with an asyncpg traceback. Roll back explicitly: the request ends here,
            # so a savepoint would buy nothing and the session must not stay poisoned.
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Organization slug '{body.organization_slug}' already belongs to a "
                    "different organization. Slugs are unique across the control plane, so "
                    "provisioning cannot claim one that is taken."
                ),
            ) from exc
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


# ============================================
# Revoke Tenant Access (called by OrcaHub when a project's paid term ends)
# ============================================

class RevokeTenantAccessRequest(BaseModel):
    """Request from OrcaHub to remove tenant access for a project (term ended / cancelled)."""
    organization_external_id: str = Field(..., description="OrcaHub organization UUID")
    cluster_id: str = Field(..., description="Cluster ID of the project")
    project_name: str = Field(..., description="LXD project name")
    user_emails: List[str] = Field(
        default_factory=list,
        description="If given, revoke only these users' assignments; otherwise revoke the whole project policy",
    )


class RevokeTenantAccessResponse(BaseModel):
    """Response for revoked tenant access. Idempotent: already-absent reports zero deletions."""
    policy_deleted: bool = False
    policy_id: Optional[int] = None
    rules_deleted: int = 0
    assignments_deleted: int = 0
    organization_id: Optional[int] = None


@external_router.post(
    "/revoke-tenant-access",
    response_model=RevokeTenantAccessResponse,
    summary="Revoke tenant access for a project",
    description=(
        "Called by OrcaHub when a project's paid term ends. Deletes the auto-provisioned "
        "`auto:{project}` policy (its rules + assignments go with it via CASCADE), or only "
        "specific users' assignments. Idempotent: an already-absent org/policy is a success."
    ),
)
async def revoke_tenant_access(
    body: RevokeTenantAccessRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Inverse of provision-tenant-access. Reactivation re-grants via provision (find-or-create)."""
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment

    # Scope check: same cluster/project allow-list the raw proxy uses.
    cluster_permissions = {p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])}
    if body.cluster_id not in cluster_permissions:
        raise HTTPException(status_code=403, detail=f"No permission for cluster {body.cluster_id}")
    allowed_projects = cluster_permissions[body.cluster_id].get("projects", ["default"])
    if body.project_name not in allowed_projects and "*" not in allowed_projects:
        raise HTTPException(status_code=403, detail=f"No permission for project {body.project_name}")

    def _invalidate_tenant_caches():
        try:
            cache = get_cache_manager()
            cache.delete_pattern("api_responses", "instances:*")
            cache.delete_pattern("api_responses", "projects:*")
            cache.delete_pattern("api_responses", "monitoring:*")
        except Exception:
            pass

    # Resolve org as provision does; absent org = nothing to revoke (idempotent success).
    caller_key_id = api_key.get("id", "")
    org = (await db.execute(select(Organization).where(and_(
        Organization.source == "orcahub",
        Organization.external_id == body.organization_external_id,
        Organization.api_key_id == caller_key_id,
    )))).scalar_one_or_none()
    if not org:
        # important: that lookup folds api_key_id in, so it misses for two different
        # reasons and the single `if not org` merged them. No such organisation is an
        # idempotent success and stays one. An organisation owned by another key is not:
        # answering 200 told the caller a revoke succeeded while nothing was touched.
        # provision refuses this exact condition by name, so revoke must agree with it.
        owner = (await db.execute(select(Organization).where(and_(
            Organization.source == "orcahub",
            Organization.external_id == body.organization_external_id,
        )))).scalar_one_or_none()
        if owner is not None and owner.api_key_id and owner.api_key_id != caller_key_id:
            logger.warning(
                "Refusing revoke of organization %s (external_id=%s): owned by api key %s, "
                "caller is %s",
                owner.slug, body.organization_external_id, owner.api_key_id, caller_key_id,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Organization '{owner.slug}' is owned by a different integration key. "
                    "A key may only revoke for organizations it already owns."
                ),
            )
        return RevokeTenantAccessResponse()

    policy_name = f"auto:{body.project_name}"
    policy = (await db.execute(select(TenantAccessPolicy).where(and_(
        TenantAccessPolicy.organization_id == org.id,
        TenantAccessPolicy.name == policy_name,
    )))).scalar_one_or_none()
    if not policy:
        return RevokeTenantAccessResponse(organization_id=org.id)

    rule_count = len((await db.execute(
        select(TenantPolicyRule).where(TenantPolicyRule.policy_id == policy.id)
    )).scalars().all())
    assignment_rows = (await db.execute(
        select(TenantPolicyAssignment).where(TenantPolicyAssignment.policy_id == policy.id)
    )).scalars().all()

    # Project-level revoke (no user_emails) OR user-level that removes the last assignment:
    # delete the whole policy and let CASCADE take its rules + assignments.
    if not body.user_emails:
        policy_id = policy.id
        await db.delete(policy)
        await db.commit()
        _invalidate_tenant_caches()
        logger.info(f"Tenant access revoked (policy): {policy_name}, org={org.slug}, rules={rule_count}, assignments={len(assignment_rows)}")
        return RevokeTenantAccessResponse(
            policy_deleted=True, policy_id=policy_id,
            rules_deleted=rule_count, assignments_deleted=len(assignment_rows),
            organization_id=org.id,
        )

    member_ids = {m.id for m in (await db.execute(select(OrganizationMember).where(and_(
        OrganizationMember.organization_id == org.id,
        OrganizationMember.user_email.in_(body.user_emails),
    )))).scalars().all()}
    to_delete = [a for a in assignment_rows if a.org_member_id in member_ids]

    if to_delete and len(to_delete) == len(assignment_rows):
        policy_id = policy.id
        await db.delete(policy)
        await db.commit()
        _invalidate_tenant_caches()
        logger.info(f"Tenant access revoked (last users -> policy): {policy_name}, org={org.slug}")
        return RevokeTenantAccessResponse(
            policy_deleted=True, policy_id=policy_id,
            rules_deleted=rule_count, assignments_deleted=len(to_delete),
            organization_id=org.id,
        )

    for a in to_delete:
        await db.delete(a)
    await db.commit()
    _invalidate_tenant_caches()
    logger.info(f"Tenant access revoked (users): {policy_name}, org={org.slug}, assignments={len(to_delete)}")
    return RevokeTenantAccessResponse(
        policy_deleted=False, policy_id=policy.id,
        rules_deleted=0, assignments_deleted=len(to_delete),
        organization_id=org.id,
    )
