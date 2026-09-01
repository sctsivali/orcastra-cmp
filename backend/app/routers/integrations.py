"""
API Router for External Integrations.
Provides endpoints for managing API keys, external app connections, and delegated access.
Supports integration with Orcahub and other external applications (soon with Orcalens, Orcasonic, Orcaflux, other Orca product).
"""

import asyncio
import secrets
import hashlib
from urllib.parse import unquote
import hmac
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, NamedTuple, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header
from pydantic import BaseModel, Field, field_validator
import requests

from app.services.lxd_service import LXDService, get_lxd_service
from app.services.lxd import health
from app.services.lxd import status as lxd_status
from app.models.schemas import AvailableCluster, AvailableClustersResponse
from app.services.lxd.proxy import (
    ALLOWED_METHODS,
    API_KEY_CACHE_TTL,
    LAST_USED_WRITE_INTERVAL,
    PROXY_CONNECT_TIMEOUT,
    EXEC_MAX_IN_FLIGHT,
    EXEC_TIMEOUT,
    PROXY_MAX_IN_FLIGHT,
    PROXY_OP_TIMEOUT,
    RAW_PROXY_TIMEOUT,
    SYNC_CLUSTER_TIMEOUT,
    VALIDATE_KEY_TIMEOUT,
    auth_executor,
    exec_executor,
    proxy_executor,
    raw_request,
    socket_timeout_for,
)
from app.services.activity_logger import get_activity_logger
from app.services.lxd.availability import require_client, unreachable_detail
from app.services.lxd.proxy_operations import (
    CLUSTER_WIDE_OPERATIONS,
    GUEST_ACCESS_OPERATIONS,
    PROXY_OPERATION_LEVELS,
    PROXY_WAIT_OPERATIONS,
    classify_proxy_error,
    execute_proxy_operation,
    fetch_operation_status,
    instance_detail,
    instance_summary,
    status_name,
)
from app.core.api_key_expiry import (
    days_remaining,
    expiry_state,
    is_expired,
    now_utc,
    tally_keys,
    utc_isoformat,
)
from app.core.api_key_permissions import PERMISSION_LEVELS, assert_cluster_permission
from app.core.api_key_capabilities import (
    ELEVATED_REASONS,
    GUEST_ACCESS_CAPABILITIES,
    assert_explicit_capability,
    assert_guest_access_allowed,
    config_reaches_inside,
    guest_access_capability_for,
    normalise_project_grant,
    proxy_destructive_action_for,
    project_grant_is_unrestricted,
    APP_TYPE_CAPABILITIES,
    APP_TYPES,
    CAPABILITIES,
    ELEVATED_CAPABILITIES,
    assert_capability,
    assert_project_scope_allows_path,
    assert_proxy_path_allowed,
    default_capabilities_for,
    granted_capabilities,
)
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.url_safety import assert_url_is_fetchable
from app.middleware.security import consume_api_key_budget
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, get_scope_cache_key, get_current_allowed_clusters, reconcile_personal_clusters
from app.core.cache import get_cache_manager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete
from sqlalchemy.exc import IntegrityError
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

activity_logger = get_activity_logger()


# ============================================
# Pydantic Models
# ============================================

class ClusterPermission(BaseModel):
    """Permission for a specific cluster, as it is STORED and as it is read back.

    important: deliberately lenient, with no validators at all. Every stored grant is read back
    straight into this model (`_key_info` does `APIKeyInfo(**metadata)` on raw Vault JSON, and
    `/whoami` and `/validate` construct it from stored dicts), so a validator here does not guard
    a boundary, it re-judges history. One legacy key with a shape a newer rule dislikes would
    raise inside the list comprehension in `list_api_keys` and 500 the whole key list, hiding
    every other key. `APIKeyInfo.app_type` above carries the same note for the same reason.

    New grants are checked by `ClusterPermissionRequest` below, which is what every request model
    uses. Tighten that one, never this one.
    """
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


class ClusterPermissionRequest(ClusterPermission):
    """A grant as it arrives from a client. Adds validators and no fields.

    Adding no fields is load-bearing: `model_dump()` produces exactly the same keys as the base,
    so what reaches Vault is byte-identical to what the lenient model would have written.
    """

    @field_validator("permissions")
    @classmethod
    def _reject_a_grant_that_grants_nothing(cls, value: List[str]) -> List[str]:
        """Refuse an empty or unrecognised grant at the boundary.

        An empty list was accepted before and reached a ``max()`` over nothing, which surfaced as
        HTTP 500. The runtime now answers 403 for it either way (app/core/api_key_permissions.py);
        this is the second half, so the mistake is caught where it is made rather than on first
        use. An unrecognised name is worth refusing for a different reason: at runtime it scores
        as "read" rather than as nothing, so a typo silently becomes a read grant.

        note: this lives on the request model, not on ClusterPermission. It sat on the base with a
        docstring claiming stored keys never passed through it, which was not true: `_key_info`
        builds `APIKeyInfo(**metadata)` from raw Vault JSON, so the rule ran over history and a
        single legacy grant could 500 the entire key list.
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

    @field_validator("projects")
    @classmethod
    def _normalise_the_project_allow_list(cls, value: List[str]) -> List[str]:
        """Trim, dedupe and charset-check the projects this grant names.

        A project name that names nothing produces a key that authenticates, reads as scoped on
        every screen, and is refused by every project-bounded call for the rest of its life. The
        console now picks these from the cluster's real project list; this is the same guarantee
        for anything that posts to the API directly.
        """
        return normalise_project_grant(value)


class ExternalUserMapping(BaseModel):
    """Maps external user to Orcastra identity."""
    # important: this value is interpolated into a Vault KV path by save_user_mapping_to_vault.
    # Unconstrained, `../../../../clusters/<id>` reached Vault, whose HTTP layer normalises the
    # path, and the mapping JSON overwrote a cluster's stored certificate material. The charset
    # is the boundary half of the fix; vault_client percent-encodes the segment as the other.
    external_user_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_](?:[A-Za-z0-9._@|-]*[A-Za-z0-9_])?$",
        description="User ID from external system (ex;, Authentik)",
    )
    external_email: str = Field(..., description="User email from external system")
    external_name: Optional[str] = Field(None, description="User display name")
    orcastra_identity_id: Optional[str] = Field(None, description="Mapped Orcastra identity ID")
    # Lenient like the rest of this model: `get_user_mappings_from_vault` builds it with
    # `ExternalUserMapping(**data)` straight from stored JSON, and that call sits inside the same
    # comprehension that builds the API-key list, so a rule applied here would 500 that list for
    # a legacy mapping. `UserMappingRequest` is the boundary.
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
    # note: deliberately an unconstrained str, unlike the create request. Keys already in Vault
    # are read back straight into this model, so validating here would turn an unfamiliar stored
    # value into a 500 on a list that used to render.
    app_type: str = Field(default="orcahub", description=f"Application type: {', '.join(APP_TYPES)}")
    capabilities: Optional[List[str]] = Field(
        default=None,
        description=(
            "Endpoint families this key may use. null means a legacy key issued before "
            "capabilities existed, which retains unrestricted access."
        ),
    )
    created_at: str = Field(..., description="Creation timestamp")
    expires_at: Optional[str] = Field(None, description="Expiration timestamp")
    is_active: bool = Field(True, description="Whether the key is active")
    is_revoked: bool = Field(False, description="Whether the key has been revoked")
    # note: derived from expires_at on every read, never stored. Expiry is a fact about a
    # timestamp, so writing an is_expired flag would need a writer, and the only path that reads
    # a key often enough to notice is the authentication hot path, where a Vault write does not
    # belong. `expiry_state` is what the card renders; `is_expired` is the one an integration
    # branches on.
    is_expired: bool = Field(False, description="Whether the key is past its expiration")
    expiry_state: str = Field(
        default="never",
        description="never, active, expiring (inside the warning window) or expired",
    )
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
    app_type: str = Field(
        default="orcahub",
        description=f"Application type, one of: {', '.join(APP_TYPES)}",
    )
    capabilities: Optional[List[str]] = Field(
        default=None,
        description=(
            "Endpoint families this key may use. Omit to take the application type's defaults."
        ),
    )
    cluster_permissions: List[ClusterPermissionRequest] = Field(
        default_factory=list,
        description="Initial cluster permissions"
    )
    expire_days: Optional[int] = Field(
        None,
        ge=1,
        le=3650,
        description="Expiration in days (None for no expiration)"
    )

    @field_validator("app_type")
    @classmethod
    def _reject_an_unknown_application_type(cls, value: str) -> str:
        """Refuse a type the capability defaults cannot answer for.

        The field used to accept any string because nothing read it. Now it seeds the key's
        capabilities, so an unrecognised value would quietly produce a key that grants nothing.
        Boundary-only, like ClusterPermission.permissions: stored keys never pass through here.
        """
        if value not in APP_TYPES:
            raise ValueError(
                f"unknown application type '{value}'. Valid types are {', '.join(APP_TYPES)}."
            )
        return value

    @field_validator("capabilities")
    @classmethod
    def _reject_a_capability_set_that_grants_nothing(
        cls, value: Optional[List[str]]
    ) -> Optional[List[str]]:
        """Refuse an empty or unrecognised capability set at the boundary.

        None is fine and means "use the type's defaults". An explicit empty list is not: it
        produces a key that authenticates and can then do nothing, which is never what the
        operator meant. An unrecognised name is refused for the same reason the permission
        validator refuses one, so a typo is caught where it is made rather than on first use.
        """
        if value is None:
            return None
        if not value:
            raise ValueError(
                "capabilities must name at least one entry. An empty list grants nothing, "
                "which is never a useful key. Omit the field to take the type's defaults."
            )
        unknown = sorted({name for name in value if name not in CAPABILITIES})
        if unknown:
            raise ValueError(
                f"unknown capability/capabilities: {', '.join(unknown)}. "
                f"Valid capabilities are {', '.join(CAPABILITIES)}."
            )
        return value


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
    # note: additive. `active` used to include expired keys, so the two numbers together used to
    # be one number that was wrong; a client reading only `active` now gets the honest figure and
    # one reading both can show the split.
    expired: int = 0
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
    # note: an integration could not previously find out when its own key lapses. Nothing warned
    # before expiry, in the UI or the logs, so the first signal was a 401 in production. These two
    # let an unattended caller check itself on the call it already makes to prove it is connected.
    expires_at: Optional[str] = Field(
        default=None, description="When this key stops working, or null if it never does"
    )
    expires_in_days: Optional[int] = Field(
        default=None,
        description="Whole days until expiry, negative once passed, null if it never expires",
    )
    capabilities: Optional[List[str]] = Field(
        default=None,
        description="Granted capabilities, or null for a legacy key with unrestricted access",
    )
    cluster_permissions: List[ClusterPermission] = Field(default_factory=list)
    error: Optional[str] = None


class UserMappingRequest(BaseModel):
    """Request to create/update user mapping."""
    external_user_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_](?:[A-Za-z0-9._@|-]*[A-Za-z0-9_])?$",
        description="External user ID",
    )
    external_email: str = Field(..., description="External user email")
    external_name: Optional[str] = Field(None, description="External user name")
    cluster_permissions: List[ClusterPermissionRequest] = Field(
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
    # important: bounded. Each element costs a SELECT plus its own commit, and members also
    # cost an Authentik call each, so an unbounded list is a single request that holds a worker
    # and the connection pool for as long as it takes. No real push approaches these numbers.
    organizations: List[SyncOrganization] = Field(
        default_factory=list,
        max_length=1000,
        description="Organizations to sync from external app"
    )
    # Callback URL for on-demand data pull
    app_url: Optional[str] = Field(None, description="External app base URL for callbacks")

    @field_validator("app_url")
    @classmethod
    def _refuse_a_callback_this_server_must_not_call(cls, value: Optional[str]) -> Optional[str]:
        """`app_url` is stored and then fetched by this server, so it is an SSRF sink.

        Three separate outbound requests are built from it: the member push in
        `orcahub_sync.push_members_to_orcahub`, which sends the key's own PLAINTEXT secret; the
        organization pull in `refresh_organizations`, whose response is fed straight into
        organization creation; and the lifecycle webhook, which sends a valid HMAC. Pointed at
        169.254.169.254 or at this network's own Vault, each of those becomes a credential
        delivery or an injection channel.

        The capability gate on this field answers "may you set it", never "is this value safe".
        https is required because every one of those requests carries a credential.
        """
        if value is None:
            return None
        return assert_url_is_fetchable(value, field="app_url", require_https=True)
    # Member data pushed from external app (ex; OrcaHub)
    members: List[SyncMember] = Field(
        default_factory=list,
        max_length=5000,
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
    # note: the default keeps every existing caller synchronous. wait=false is for work that
    # can outlive PROXY_OP_TIMEOUT, ex; a create that has to pull an image. It returns the LXD
    # operation id immediately and the caller polls /integrations/operations/{id}.
    wait: bool = Field(
        default=True,
        description="Wait for the LXD operation to finish. False returns an operation id to poll.",
    )
    external_user_id: Optional[str] = Field(None, description="External user making the request")
    external_user_email: Optional[str] = Field(None, description="External user email")


class RawProxyRequest(BaseModel):
    """Request to proxy a raw HTTP request to LXD."""
    cluster_id: str = Field(..., description="Target cluster ID")
    method: str = Field(..., description="HTTP method: GET, POST, PUT, DELETE, PATCH")
    path: str = Field(
        ..., max_length=2048, description="LXD API path, ex;, /1.0/instances"
    )
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
    # note: additive, and only set on failure. The endpoint answers 200 with success=false for
    # a failure inside the cluster, so without a code an unattended caller has to read English
    # to tell "give up, it is not there" from "retry, the cluster is down".
    error_code: Optional[str] = None
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
            _REVOKED_NS, key_id, True, ttl=int(API_KEY_CACHE_TTL) + 5
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


def _fetch_cluster_info(cert_manager, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the sync cluster-info fan-out. Reads Vault, so it must not run on the
    event loop. Returns a single-item list so it composes with the shared gather helper.
    """
    cluster_info = cert_manager.get_cluster_info(cluster_id)
    metadata = cluster_info.get("metadata", {})
    location_data = metadata.get("location", {})
    public_location = _public_location_data(location_data)
    zone_name = location_data.get("region_name") or location_data.get("city") or None
    return [{
        "id": cluster_id,
        "name": metadata.get("name", cluster_id),
        "endpoint": metadata.get("endpoint"),
        "server_type": metadata.get("server_type", "lxd"),
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


def _fetch_cluster_projects(lxd_service, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the projects fan-out. Only ever runs on proxy_executor.

    note: raises rather than returning empty for an unreachable cluster. _gather_per_cluster
    records the reason and still answers with what the other clusters gave, which is the
    contract the rest of the fan-out helpers already follow. Returning empty here made a dead
    cluster and an empty cluster identical in the response.
    """
    client = require_client(
        lxd_service.connection_manager.get_client(cluster_id), cluster_id
    )
    return [
        {"name": p.name, "cluster_id": cluster_id, "description": p.description}
        for p in client.projects.all()
    ]


def _fetch_cluster_instances(lxd_service, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the instances fan-out. Only ever runs on proxy_executor.

    important: this read ``client.instances.all()`` off the pooled client, which pylxd binds
    to the default project. Every instance in every other project was therefore missing from
    the answer entirely rather than merely unlabelled, and nothing in the response said so, so
    an integration that provisions into its own project saw an empty fleet. The instance
    manager's per-node fetch already walks every project with ``recursion=1`` and resolves
    addresses for running instances, so this reuses it instead of growing a second copy that
    would drift the same way.

    note: reusing it also inherits its reconnect path, which reads Vault for the cluster's
    certificate and records a breaker failure when the cluster does not answer. That is a real
    change of behaviour on the API-key surface, where the old read was an in-memory client plus
    one call. It is the right trade: the breaker is already shared with the dashboard, which
    reaches the same function through list_all_vms, and a cluster that genuinely will not answer
    is what the breaker exists to record. The alternative is a second implementation that does
    not reconnect, which is exactly the duplication that hid the default-project bug.
    """
    vms, connected = lxd_service.instance_manager.fetch_node_instances(cluster_id)
    if not connected:
        # The manager reports reachability separately from the result, and discarding it made
        # an unreachable cluster indistinguishable from one holding no instances.
        raise HTTPException(status_code=503, detail=unreachable_detail(cluster_id))
    return [
        instance_summary(
            name=vm.name,
            cluster_id=cluster_id,
            project=vm.project,
            status=status_name(vm.status_code, vm.status),
            status_code=vm.status_code,
            instance_type=vm.type,
            architecture=vm.architecture,
            config=vm.config,
            expanded_devices=vm.expanded_devices,
            ip_addresses=vm.ip_addresses,
        )
        for vm in vms
    ]


def _fetch_instance_detail(
    lxd_service,
    cluster_id: str,
    name: str,
    project: Optional[str],
    granted: Optional[Dict[str, Optional[set]]] = None,
) -> List[Dict[str, Any]]:
    """Blocking half of the instance detail read. Only ever runs on proxy_executor.

    Returns a single-item list, or an empty one when the name is not on this cluster, so it
    composes with the shared gather helper like every other fetch here.
    """
    from app.services.lxd.utils import find_instance_in_projects

    client = require_client(
        lxd_service.connection_manager.get_client(cluster_id), cluster_id
    )
    # important: search only what the key may see. find_instance_in_projects with project=None
    # lists every project on the cluster and then does one GET per project, so a miss cost
    # 1 + N round trips on the shared proxy pool for a key that may be allowed to see one of
    # them, and the caller chooses the name. Narrowing is both the cheap and the correct answer.
    search = [project] if project else sorted((granted or {}).get(cluster_id) or []) or [None]
    instance = found_in = None
    for candidate in search:
        instance, found_in = find_instance_in_projects(client, name, candidate)
        if instance is not None:
            break
    if instance is None:
        return []
    return [
        instance_detail(
            name=instance.name,
            cluster_id=cluster_id,
            project=found_in,
            status=status_name(
                getattr(instance, "status_code", None), getattr(instance, "status", None)
            ),
            status_code=getattr(instance, "status_code", None),
            instance_type=getattr(instance, "type", None),
            architecture=getattr(instance, "architecture", None),
            config=dict(getattr(instance, "config", {}) or {}),
            devices=dict(getattr(instance, "devices", {}) or {}),
            expanded_devices=dict(getattr(instance, "expanded_devices", {}) or {}),
            profiles=list(getattr(instance, "profiles", []) or []),
            location=getattr(instance, "location", None),
            description=getattr(instance, "description", None),
            created_at=getattr(instance, "created_at", None),
            last_used_at=getattr(instance, "last_used_at", None),
        )
    ]


class ClusterFanout(NamedTuple):
    """What a fan-out actually returned, and what it could not reach.

    important: the second half is not decoration. Every failure path below contributes an
    empty list, so a fleet read over a tripped cluster used to come back short with a `total`
    that looked authoritative. A caller polling for its own instances cannot distinguish that
    from the instances having been deleted, which is the worst possible ambiguity to hand an
    unattended agent. Reporting the clusters that did not answer makes a partial answer say so.

    note: this deliberately does not reuse the field name the dashboard routers report the same
    idea under. Theirs is a `List[str]` of names, because a banner only has to say which nodes
    are missing. This carries a reason per cluster, because an integration has to choose between
    retrying and giving up. One name over two shapes would be worse for a client reading both
    endpoints than two names are.
    """

    items: List[Dict[str, Any]]
    unreachable: List[Dict[str, str]]

    @property
    def partial(self) -> bool:
        return bool(self.unreachable)


async def _gather_per_cluster(
    fn, ctx, cluster_ids, label: str, *extra
) -> ClusterFanout:
    """Run a blocking per-cluster fetch across clusters concurrently and off the event loop.

    important: this replaced a serial in-loop fan-out. With one silent cluster that shape
    blocked the whole process for the sum of every cluster's timeout. Measured on dev before
    the change, a single POST /integrations/sync held the loop for 35.7s, which is what made
    /health spike to 11s. Now the cost is the slowest cluster, paid on a worker thread, and a
    cluster the breaker has already tripped is skipped entirely.
    """
    misses: List[Dict[str, str]] = []

    async def one(cluster_id: str) -> List[Dict[str, Any]]:
        if health.is_tripped(cluster_id):
            logger.debug(f"sync: skipping {label} for tripped cluster {cluster_id}")
            misses.append({
                "cluster_id": cluster_id,
                "reason": health.get_reason(cluster_id) or "recently unreachable",
            })
            return []
        try:
            return await run_blocking(
                fn,
                ctx,
                cluster_id,
                *extra,
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
            misses.append({
                "cluster_id": cluster_id,
                "reason": f"timed out after {SYNC_CLUSTER_TIMEOUT:g}s",
            })
            return []
        except BlockingPoolFull:
            logger.warning(f"sync: {label} for {cluster_id} shed, proxy pool saturated")
            misses.append({"cluster_id": cluster_id, "reason": "proxy pool saturated"})
            return []
        except Exception as e:
            logger.warning(f"Could not get {label} for {cluster_id}: {e}")
            # note: str(HTTPException) renders as "503: <detail>". The status is already
            # implied by the row being in `unreachable`, and the doubled prefix made two
            # clusters with the same cause read as two different failures.
            misses.append({
                "cluster_id": cluster_id, "reason": getattr(e, "detail", None) or str(e)
            })
            return []

    batches = await asyncio.gather(*[one(c) for c in cluster_ids])
    return ClusterFanout([item for batch in batches for item in batch], misses)


async def _assert_granted_projects_exist(
    lxd_service: LXDService, permissions: List[ClusterPermissionRequest]
) -> None:
    """Refuse a grant that names a project no selected cluster has, when we can tell.

    The charset rule upstream catches a malformed name. It cannot catch a well-formed one that
    names nothing, which is the failure this whole feature exists to remove: the key
    authenticates, reads as scoped on every screen, and is refused by every project-bounded call
    for the rest of its life.

    important: strict only where it can prove the negative. A name is refused when every cluster
    this request names answered and none of them reports it. If even one stayed silent the check
    is skipped, because the name might live only there, and because narrowing an over-broad key
    is exactly what an operator does during an outage. Blocking that would be backwards.

    important: the union across all selected clusters, not per cluster. The console fans one
    projects list out to every selected cluster, so a project that lives on only some of them is
    a deliberate, warned-about choice rather than an error. The console says so before submit.

    The error names only what the caller sent. Echoing the cluster's real project list back would
    let a partner enumerate another tenant's project names through a rejected write.
    """
    wanted: Dict[str, set] = {}
    for grant in permissions:
        if project_grant_is_unrestricted(grant.projects):
            continue
        named = {name for name in grant.projects if name != "*"}
        if named:
            wanted.setdefault(grant.cluster_id, set()).update(named)

    if not wanted:
        return

    cluster_ids = sorted(wanted)
    fanout = await _gather_per_cluster(
        _fetch_cluster_projects, lxd_service, cluster_ids, "projects"
    )
    silent = {miss.get("cluster_id") for miss in fanout.unreachable} & set(cluster_ids)
    if silent:
        # Inconclusive is not guilty. A name this request wants might live only on the cluster
        # that did not answer, and the console offers manual entry for exactly that case, so
        # refusing here would turn an outage into an operator error on the one path built to
        # survive it.
        logger.info(
            "Not checking project names for %s: %s did not answer",
            cluster_ids,
            sorted(silent),
        )
        return

    known = {item.get("name") for item in fanout.items}
    requested = set().union(*wanted.values())
    unknown = sorted(requested - known)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No selected cluster has these projects: {', '.join(unknown)}. "
                "Check the spelling, or select a cluster that has them."
            ),
        )


def _validate_api_key_blocking(key_id: str, key_secret: str) -> Dict[str, Any]:
    """Every Vault round trip needed to validate a key. Only ever runs on auth_executor.

    Was previously inline on the event loop: 6 to 7 blocking Vault calls per request, each
    (2,3) with Retry(total=2), so a degraded Vault could park a worker for a minute or more.
    """
    vault_client = get_vault_integrations_client()
    metadata, secret_hash = get_api_key_from_vault(vault_client, key_id)

    if not verify_api_key_secret(key_secret, secret_hash):
        raise HTTPException(status_code=401, detail="Invalid API key secret")

    # Backfill raw secret if not already stored (for callback auth)
    try:
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
        )
        if not secret_data.get("raw"):
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
                #
                # important: the ownership term is load-bearing and was missing. Pass 1 refuses to
                # touch an organisation owned by another key, with a comment saying why; this
                # branch reassigns `api_key_id` outright, so without the same test any key could
                # take over another integration's organisation by pushing an org that merely
                # reused its slug with a fresh external_id. Owning it then passes every later
                # check, including the member sync that pushes roles into Authentik.
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
                    f"{slug_owner.source} org (id={slug_owner.id}, external_id={slug_owner.external_id}); "
                    f"resolve the slug clash to sync it"
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
    Revoke a user's GLOBAL Authentik role only if they no longer belong to ANY org.
    Orcastra roles are global system roles (one per user), so stripping them on leaving a
    single org would wrongly de-role a user still active in another org. Best-effort: the
    caller has already deleted the membership row (which is what actually cuts org-scope
    access), so a failed IdP revoke leaves only an inert role group, logged for cleanup.
    """
    remaining = len((await db.execute(
        select(OrganizationMember.id).where(OrganizationMember.user_email == email)
    )).scalars().all())
    if remaining > 0:
        return
    try:
        await sync_svc.remove_user_from_all_roles(email)
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
            try:
                await sync_svc.sync_user_role(m.email, orcastra_role)
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
                raise HTTPException(status_code=401, detail="Invalid API key") from None
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
    #
    # important: this used to parse with `fromisoformat` and compare against a naive
    # `datetime.utcnow()`, outside any try. An offset-bearing stored value raised TypeError and a
    # malformed one raised ValueError, both of which left this as a 500 on every request the key
    # made, including for a key whose expiry was years away. An empty string took the other
    # branch entirely and was accepted. All four measured against the running deployment.
    if is_expired(metadata.get("expires_at")):
        raise HTTPException(status_code=401, detail="API key has expired")

    # note: after the secret verifies, never before. The key id is public, so counting it in the
    # middleware would let anyone holding a victim's id spend that integration's budget with a
    # wrong secret. See consume_api_key_budget.
    if not consume_api_key_budget(x_api_key_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this API key. Please try again later.",
            headers={"Retry-After": "30"},
        )

    _schedule_last_used_update(x_api_key_id)

    return metadata


def require_capability(capability: str):
    """Authenticate the key, then refuse it unless it carries ``capability``.

    Every handler on `external_router` must depend on one of these rather than on
    `validate_api_key` directly. Authentication alone used to be the whole gate, so a key issued
    for one job could do every other job on this router; the capability is what separates them.
    `tests/test_every_external_endpoint_declares_a_capability.py` is the ratchet that keeps a new
    endpoint from quietly skipping it.

    note: FastAPI caches a dependency per request, so the nested `validate_api_key` still costs
    exactly one Vault round trip even where a handler also takes the key for its own use.
    """
    async def _dep(api_key: Dict[str, Any] = Depends(validate_api_key)) -> Dict[str, Any]:
        assert_capability(api_key, capability)
        return api_key

    # Name it after the capability so a traceback, a dependency-graph dump and the OpenAPI
    # schema all say which one was required, rather than reporting every gate as `_dep`.
    _dep.__name__ = f"require_{capability.replace('.', '_')}"
    _dep.__qualname__ = _dep.__name__
    return _dep


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
    lxd_service: LXDService = Depends(get_lxd_service),
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

    # After the scope guard on purpose: this reaches the clusters, so it must never run for a
    # cluster the caller was just told they may not touch.
    await _assert_granted_projects_exist(lxd_service, request.cluster_permissions)

    try:
        # Generate key pair
        key_id, key_secret, secret_hash = generate_api_key_pair()
        
        # Calculate expiration
        # note: aware, so the stored string carries an offset and stops being UTC by convention
        # alone. Values already in Vault are naive and keep working, because parse_expiry reads a
        # naive value as UTC, which is what every writer in this repo meant by it.
        created_at = now_utc()
        expires_at = None
        # `is not None` rather than truthiness: `ge=1` makes 0 unreachable today, but the two
        # spellings disagree the moment that constraint changes, and this is the line that
        # decides whether a key ever expires.
        if request.expire_days is not None:
            expires_at = created_at + timedelta(days=request.expire_days)
        
        # important: always written as a concrete list, never left absent. An absent field means
        # "legacy, unrestricted" when the key is read back, so a new key that omitted it would be
        # issued with more access than the operator chose. A caller that names no capabilities
        # gets the application type's defaults instead.
        capabilities = sorted(
            request.capabilities
            if request.capabilities is not None
            else default_capabilities_for(request.app_type)
        )
        if not capabilities:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Application type '{request.app_type}' has no default capabilities, so the "
                    f"request must name them. Valid capabilities are {', '.join(CAPABILITIES)}."
                ),
            )

        # Prepare metadata (include creator for org-level ownership)
        metadata = {
            "id": key_id,
            "name": request.name,
            "app_name": request.app_name,
            "app_type": request.app_type,
            "capabilities": capabilities,
            "created_by": _rbac.email,
            "created_at": utc_isoformat(created_at),
            "expires_at": utc_isoformat(expires_at) if expires_at else None,
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
            api_key=_key_info(metadata),
            api_key_id=key_id,
            api_key_secret=key_secret,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _authorize_api_key(vault_client, key_id: str) -> Dict[str, Any]:
    """Return an API key's metadata only if it overlaps the caller's clusters.

    important: `list_api_keys` already filters by `cluster_permissions` overlap, but every
    single-key handler took `key_id` straight from the path and looked it up in Vault with no
    such check. A caller who learned any key id could read its permissions and user mappings,
    or revoke and delete it, across organisations. The overlap rule here is the same one the
    list endpoint uses, so the two surfaces agree.
    """
    metadata, _secret_hash = get_api_key_from_vault(vault_client, key_id)
    if not metadata:
        raise HTTPException(status_code=404, detail="API key not found")
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        perms = metadata.get("cluster_permissions", []) or []
        key_clusters = {p.get("cluster_id", "") for p in perms if isinstance(p, dict)}
        if not (key_clusters & allowed):
            # Same shape as a missing key, so an out-of-scope id cannot be probed apart from a
            # non-existent one.
            raise HTTPException(status_code=404, detail="API key not found")
    return metadata


@router.get(
    "/api-keys",
    response_model=APIKeyListResponse,
    summary="List all API keys",
    description="Get a list of all API keys for external integrations.",
)
async def list_api_keys(fresh: bool = False):
    """List API keys visible to the current user (admin sees all, others see org-scoped)."""
    vault_client = get_vault_integrations_client()
    
    try:
        # Scoped cache key so each org gets its own cached list
        cache = get_cache_manager()
        cache_key = get_scope_cache_key("integrations:api_keys:list")
        cached_data = None if fresh else cache.get("api_responses", cache_key)
        
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
        # important: an expired key is not active. It was counted here, so the dashboard
        # reported six lapsed keys among its active ones and an operator had to read each date
        # to find out otherwise. The counting lives in `tally_keys` rather than in this loop so
        # it can be asserted on fixtures: an earlier structural test only checked that the
        # counter was incremented, and stayed green when the branch bodies were swapped.
        records = [m for m in all_metadata if isinstance(m, dict)]
        counts = tally_keys(records)

        keys = [
            _key_info(metadata, len(get_user_mappings_from_vault(vault_client, metadata["id"])))
            for metadata in records
        ]

        return APIKeyListResponse(
            total=len(keys),
            active=counts["active"],
            revoked=counts["revoked"],
            expired=counts["expired"],
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
    _authorize_api_key(get_vault_integrations_client(), key_id)
    vault_client = get_vault_integrations_client()
    
    try:
        metadata, _ = get_api_key_from_vault(vault_client, key_id)
        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        
        return _key_info(metadata, len(user_mappings))
        
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
    permissions: List[ClusterPermissionRequest],
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Update API key cluster permissions."""
    _authorize_api_key(get_vault_integrations_client(), key_id)

    # important: creation validates the requested clusters against the caller's own set
    # (see create_api_key), and this path did not, so a partner could create a key scoped to
    # their cluster and then widen it to the whole fleet in a second call. Same rule, both doors.
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        requested = {p.cluster_id for p in permissions}
        invalid = requested - allowed
        if invalid:
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted to grant access to clusters: {sorted(invalid)}",
            )

    await _assert_granted_projects_exist(lxd_service, permissions)

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
        # important: also drop the process-local validation cache, otherwise the old
        # cluster_permissions keep authorising proxy calls until its TTL expires.
        evict_api_key_cache(key_id)

        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        return _key_info(metadata, len(user_mappings))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key permissions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class APIKeyCapabilitiesRequest(BaseModel):
    """Request to replace an API key's capability set."""
    capabilities: List[str] = Field(..., min_length=1)

    @field_validator("capabilities")
    @classmethod
    def _reject_unknown_capabilities(cls, value: List[str]) -> List[str]:
        unknown = sorted({name for name in value if name not in CAPABILITIES})
        if unknown:
            raise ValueError(
                f"unknown capability/capabilities: {', '.join(unknown)}. "
                f"Valid capabilities are {', '.join(CAPABILITIES)}."
            )
        return value


@router.put(
    "/api-keys/{key_id}/capabilities",
    response_model=APIKeyInfo,
    summary="Set an API key's capabilities",
    description="Narrow or widen what an existing key may do, without reissuing it.",
)
async def update_api_key_capabilities(
    key_id: str,
    request: APIKeyCapabilitiesRequest,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Give a key a capability set it can be narrowed with.

    important: without this there was no migration path at all. A key issued before capabilities
    existed carries no field, which means unrestricted, and nothing could add one; the only way
    to narrow such a key was to revoke it and have the integration reconfigured. That is a strong
    incentive to leave the legacy keys alone, which is the opposite of what the capability model
    is for.

    Read-modify-write like every other metadata writer here, so the field this adds is the only
    one that changes. The cache is evicted because the old capability set would otherwise keep
    authorising requests until its TTL expired.
    """
    vault_client = get_vault_integrations_client()
    _authorize_api_key(vault_client, key_id)

    try:
        metadata, _secret_hash = get_api_key_from_vault(vault_client, key_id)
        was_legacy = "capabilities" not in metadata
        metadata["capabilities"] = sorted(set(request.capabilities))
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata,
        )
        logger.info(
            "Set capabilities on API key %s to %s (was %s)",
            key_id, metadata["capabilities"], "legacy/unrestricted" if was_legacy else "a set",
        )

        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        evict_api_key_cache(key_id)

        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        return _key_info(metadata, len(user_mappings))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key capabilities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update capabilities")


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
    _authorize_api_key(get_vault_integrations_client(), key_id)
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
        # important: revocation must be immediate. Without this the key keeps working from
        # the process-local validation cache until its TTL expires.
        evict_api_key_cache(key_id)

        logger.info(f"API key {key_id} revoked")
        
        user_mappings = get_user_mappings_from_vault(vault_client, key_id)
        return _key_info(metadata, len(user_mappings))
        
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
    _authorize_api_key(get_vault_integrations_client(), key_id)
    vault_client = get_vault_integrations_client()
    
    try:
        # Verify it exists
        get_api_key_from_vault(vault_client, key_id)
        
        # Delete from Vault
        delete_api_key_from_vault(vault_client, key_id)

        # Clear all scoped cache variants
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        evict_api_key_cache(key_id)

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
    _authorize_api_key(get_vault_integrations_client(), key_id)
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
)
async def list_user_mappings(key_id: str):
    """List all user mappings for an API key."""
    _authorize_api_key(get_vault_integrations_client(), key_id)
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
            return APIKeyValidateResponse(valid=False, error="Invalid API key or secret")
        
        # Check if revoked
        if metadata.get("is_revoked", False):
            return APIKeyValidateResponse(valid=False, error="API key revoked")
        
        # Check if active
        if not metadata.get("is_active", True):
            return APIKeyValidateResponse(valid=False, error="API key not active")
        
        # Check expiration. Same decision as the header dependency, and for the same reason:
        # this handler's broad `except Exception` below would otherwise turn a TypeError from a
        # naive/aware comparison into a generic "Validation failed", which tells an integrator
        # nothing about why its key stopped working.
        expires_at = metadata.get("expires_at")
        if is_expired(expires_at):
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
        
        # note: this endpoint takes credentials in the body, so it never passes through
        # validate_api_key and would otherwise be the one metered path a valid key could spend
        # without limit. Counted here, after the secret verified, for the same reason.
        if not consume_api_key_budget(request.api_key_id):
            return APIKeyValidateResponse(
                valid=False, error="Rate limit exceeded for this API key"
            )

        # note: granted_capabilities returns None for a legacy key, which is exactly what the
        # response means by null. An integrator reads this to discover what it may call before
        # it calls it, so it must not be flattened into an empty list.
        granted = granted_capabilities(metadata)
        return APIKeyValidateResponse(
            valid=True,
            app_name=metadata.get("app_name"),
            app_type=metadata.get("app_type"),
            capabilities=sorted(granted) if granted is not None else None,
            expires_at=metadata.get("expires_at"),
            expires_in_days=days_remaining(metadata.get("expires_at")),
            cluster_permissions=enriched_permissions,
        )
        
    except HTTPException:
        # note: the same wording as a wrong secret. This endpoint takes no authentication, so
        # distinguishing the two turned it into a free oracle for which key ids exist.
        return APIKeyValidateResponse(valid=False, error="Invalid API key or secret")
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
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
):
    """Sync data to external application."""
    try:
        result = SyncDataResponse()
        api_key_id = api_key.get("id", "")

        # important: this handler does two unrelated jobs. Reading inventory back is what most
        # integrations want and is covered by the dependency above. Pushing organizations and
        # members is an authorization write: it creates orgs and hands members a role that is
        # propagated to Authentik as a global system role. Gate that half separately so a key
        # issued to read inventory cannot mint an organization.
        if request.organizations or request.members:
            assert_capability(api_key, "org.sync")

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
            # important: this is the third write in this handler, not bookkeeping. `app_url` is
            # the base that `refresh_organizations` fetches `/api/external/organizations` from,
            # and it feeds the response straight into _upsert_synced_organizations. A key that
            # could set it without org.sync would therefore get organization creation through the
            # back door, which is the exact power this handler gates above, and would also aim an
            # authenticated server-side request at a host of its choosing.
            assert_capability(api_key, "org.sync")
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
                
                result.clusters = (await _gather_per_cluster(
                    _fetch_cluster_info, cert_manager, cluster_ids, "cluster_info"
                )).items
        
        if request.include_projects:
            projects = await _gather_per_cluster(
                _fetch_cluster_projects, lxd_service, cluster_ids, "projects"
            )
            result.projects = _within_granted_projects(
                api_key, projects.items, project_field="name"
            )

        if request.include_instances:
            instances = await _gather_per_cluster(
                _fetch_cluster_instances, lxd_service, cluster_ids, "instances"
            )
            result.instances = _within_granted_projects(api_key, instances.items)

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


# ============================================
# Resource-shaped reads for integrations
# ============================================
#
# important: these exist because the key-authenticated surface was proxy-shaped and nothing else.
# Reading instances meant POST /integrations/raw-proxy with a body naming an HTTP verb and a
# cluster id the key had no way to discover, while GET /instances, /nodes and /projects all
# answered 401 because they are the JWT routers. An integrator's first call therefore failed and
# read as "this needs a login", which is exactly the conclusion that was drawn.
#
# They add no reach. Each one is the data POST /integrations/sync already returns to the same
# caller, through the same fetch helpers, narrowed by the same intersection with the key's
# cluster_permissions. What changes is the shape.


# Which audit action a raw-proxy path belongs under. A call that reaches inside a guest is the
# same kind of event as opening a console, so it is named and rated like one; everything else is
# ordinary proxy traffic.
_PROXY_AUDIT_ACTIONS: Dict[Optional[str], str] = {
    "instance.exec": "integration.instance.exec",
    "instance.files": "integration.instance.files",
}


def _proxy_audit_action(method: str, path: str) -> str:
    """What to call this raw-proxy call in the audit trail.

    Guest-reaching calls are named first, because reaching inside a guest is the more serious
    fact about a call that also happens to delete something. A destructive call comes next: a
    DELETE through the proxy removes the same resource the dashboard's own endpoint would, and
    naming it `integration.proxy.request` put it outside HIGH_SEVERITY_ACTIONS, so anything
    alerting on `image.delete` never saw this route. Everything else is ordinary proxy traffic.
    """
    guest = _PROXY_AUDIT_ACTIONS.get(guest_access_capability_for(path))
    if guest:
        return guest
    return proxy_destructive_action_for(method, path) or "integration.proxy.request"


def _audit_key_action(
    api_key: Dict[str, Any],
    *,
    action: str,
    target_id: str,
    cluster_id: str,
    project: Optional[str],
    result: str = "success",
    details: Optional[Dict[str, Any]] = None,
    external_user_email: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Record what a key did, in the index a human reviews rather than only the app log.

    important: this surface had no audit record at all. The one `logger.info` beside it names the
    method and path but never the body, is not routed to the audit index, and is subject to
    `LogDeduplicationFilter`, so a hundred different commands against the same instance inside a
    minute collapsed to a single line. For a path that runs code inside a customer's guest that
    is not a trail.

    important: the actor is the KEY, not `external_user_email`. That field is supplied by the
    caller on the request body and is never verified, so treating it as identity would let the
    caller write its own audit entry. It is recorded as an untrusted annotation instead.
    """
    try:
        activity_logger.log_activity(
            action=action,
            target_type="instance" if action.startswith("integration.instance.") else "cluster",
            target_id=target_id,
            target_host=cluster_id,
            target_project=project,
            result=result,
            error_message=error_message,
            details={
                **(details or {}),
                "api_key_id": api_key.get("id"),
                "app_name": api_key.get("app_name"),
                "app_type": api_key.get("app_type"),
                "claimed_user_email": external_user_email,
            },
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the request
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


def _key_info(metadata: Dict[str, Any], user_mappings_count: int = 0) -> "APIKeyInfo":
    """One builder for every APIKeyInfo, so the derived fields cannot be right on one path only.

    important: five handlers construct this model and only the list passed the two derived
    fields, so `GET /integrations/api-keys/{id}` on a key that lapsed in 2020 answered
    `expiry_state: "never"` beside its own past date. Deriving them here means a new handler
    cannot forget.

    note: `is_expired` is derived from the timestamp, NOT from `classify_key`. The classifier
    ranks revocation above expiry so the badge reads "Revoked", which is what an operator needs,
    but the field is documented as "past its expiration" and an integration branches on it. A
    key that is both would otherwise report `is_expired: false` next to `expiry_state: expired`.
    """
    return APIKeyInfo(
        **metadata,
        is_expired=is_expired(metadata.get("expires_at")),
        expiry_state=expiry_state(metadata.get("expires_at")),
        user_mappings_count=user_mappings_count,
    )


def _audit_raw_proxy(
    api_key: Dict[str, Any],
    request: Any,
    method: str,
    project: Optional[str],
    result: str,
    *,
    lxd_status: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """One record per raw-proxy call, whatever became of it.

    note: a call that timed out or was shed still reached the cluster, or may have. Recording
    only the ones that answered would leave the slowest, which are the ones worth reviewing,
    out of the index entirely.
    """
    _audit_key_action(
        api_key,
        action=_proxy_audit_action(method, request.path),
        target_id=request.path[:512],
        cluster_id=request.cluster_id,
        project=project,
        result=result,
        details={"method": method, "lxd_status": lxd_status},
        external_user_email=request.external_user_email,
        error_message=error,
    )


def _clusters_for_key(api_key: Dict[str, Any], cluster_id: Optional[str]) -> set:
    """The clusters this request may touch: the key's grant, optionally narrowed by one id.

    Intersection, not a lookup, and the same one sync_data performs. A caller-supplied cluster
    that is not in the grant simply is not in the result, so an unknown id and an unauthorised
    one are indistinguishable and neither can be probed.
    """
    allowed = {
        grant["cluster_id"]
        for grant in api_key.get("cluster_permissions", [])
        if isinstance(grant, dict) and "cluster_id" in grant
    }
    if cluster_id:
        return allowed & {cluster_id}
    return allowed


def _granted_projects(api_key: Dict[str, Any], cluster_id: str) -> Optional[set]:
    """The projects this key may see on one cluster, or None for the `*` wildcard.

    The same read the proxy, the raw proxy and both upload routers perform. Absent means
    `["default"]`, which is what ClusterPermission declares as its default.
    """
    for grant in api_key.get("cluster_permissions", []):
        if isinstance(grant, dict) and grant.get("cluster_id") == cluster_id:
            projects = grant.get("projects", ["default"])
            return None if "*" in projects else set(projects)
    return set()


def _within_granted_projects(
    api_key: Dict[str, Any], rows: List[Dict[str, Any]], *, project_field: str = "project"
) -> List[Dict[str, Any]]:
    """Drop rows for a project the key was not granted on that row's cluster.

    important: the fan-out reads every project on a cluster, because that is the only way to
    see instances outside `default`. Narrowing back to the grant therefore has to happen here
    or a key scoped to one project enumerates all of them. The proxy paths have always
    enforced this; these read endpoints did not, which was a hole in the newer surface rather
    than in the older one.

    note: `project_field` exists because a project row names itself in `name` while an instance
    row names its project in `project`. Defaulting to `project` silently kept every row for a
    project listing, so the filter looked applied and did nothing. Callers state the field.
    """
    allowed_by_cluster: Dict[str, Optional[set]] = {}
    kept = []
    for row in rows:
        cluster = row.get("cluster_id")
        if cluster not in allowed_by_cluster:
            allowed_by_cluster[cluster] = _granted_projects(api_key, cluster)
        allowed = allowed_by_cluster[cluster]
        if allowed is None or (row.get(project_field) or "default") in allowed:
            kept.append(row)
    return kept


@external_router.get(
    "/whoami",
    response_model=APIKeyValidateResponse,
    summary="Describe the presented API key",
    description="What this key is and what it may do, authenticated by the usual headers.",
)
async def whoami(api_key: Dict[str, Any] = Depends(validate_api_key)):
    """The natural first call, and the one that makes a copy-pasted credential prove itself.

    `/validate` already answers this, but it takes the credentials in the BODY, so an integrator
    who sends the headers every other endpoint wants gets a 422 and reasonably concludes the key
    is broken. This is the same answer through the same headers as everything else.

    No capability is required, and it is exempt in the capability ratchet for the reason
    `/validate` carries: it grants nothing and reports only what the presented key already holds.
    """
    granted = granted_capabilities(api_key)
    return APIKeyValidateResponse(
        valid=True,
        app_name=api_key.get("app_name"),
        app_type=api_key.get("app_type"),
        capabilities=sorted(granted) if granted is not None else None,
        expires_at=api_key.get("expires_at"),
        expires_in_days=days_remaining(api_key.get("expires_at")),
        cluster_permissions=[
            ClusterPermission(**grant)
            for grant in api_key.get("cluster_permissions", [])
            if isinstance(grant, dict) and "cluster_id" in grant
        ],
    )


@external_router.get(
    "/clusters",
    summary="List the clusters this key may reach",
    description="The clusters in the key's grant, with their live status.",
)
async def list_integration_clusters(
    cluster_id: Optional[str] = Query(None, description="Narrow to one cluster"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
):
    """The answer to "which cluster_id do I pass", which nothing previously told a key holder."""
    settings = get_settings()
    if not (settings.vault_enabled and settings.vault_token):
        raise HTTPException(status_code=503, detail="Vault is not configured")

    from app.services.lxd_cert_manager import LXDCertificateManager

    cert_manager = LXDCertificateManager(
        vault_client=get_vault_client(settings.vault_addr, settings.vault_token),
        clusters_path=settings.vault_clusters_path,
    )
    fanout = await _gather_per_cluster(
        _fetch_cluster_info, cert_manager, _clusters_for_key(api_key, cluster_id), "cluster_info"
    )
    return {
        "clusters": fanout.items,
        "total": len(fanout.items),
        "partial": fanout.partial,
        "unreachable": fanout.unreachable,
    }


@external_router.get(
    "/projects",
    summary="List projects across the clusters this key may reach",
)
async def list_integration_projects(
    cluster_id: Optional[str] = Query(None, description="Narrow to one cluster"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    fanout = await _gather_per_cluster(
        _fetch_cluster_projects, lxd_service, _clusters_for_key(api_key, cluster_id), "projects"
    )
    projects = _within_granted_projects(api_key, fanout.items, project_field="name")
    return {
        "projects": projects,
        "total": len(projects),
        "partial": fanout.partial,
        "unreachable": fanout.unreachable,
    }


@external_router.get(
    "/instances",
    summary="List instances across the clusters this key may reach",
    description=(
        "A summary per instance: status, type, address and the CPU, memory and root disk it "
        "was given. Every project the key is granted is included. Use "
        "/integrations/instances/{name} for the full specification."
    ),
)
async def list_integration_instances(
    cluster_id: Optional[str] = Query(None, description="Narrow to one cluster"),
    project: Optional[str] = Query(None, description="Narrow to one LXD project"),
    status: Optional[str] = Query(None, description="Narrow to one status, ex; Running"),
    limit: int = Query(200, ge=1, le=1000, description="Maximum instances to return"),
    offset: int = Query(0, ge=0, description="Instances to skip, for paging"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    fanout = await _gather_per_cluster(
        _fetch_cluster_instances, lxd_service, _clusters_for_key(api_key, cluster_id), "instances"
    )
    rows = _within_granted_projects(api_key, fanout.items)
    if project:
        rows = [row for row in rows if (row.get("project") or "default") == project]
    if status:
        rows = [row for row in rows if (row.get("status") or "").lower() == status.lower()]
    rows.sort(key=lambda row: (row.get("cluster_id") or "", row.get("project") or "", row["name"]))
    window = rows[offset : offset + limit]
    return {
        "instances": window,
        "total": len(rows),
        "count": len(window),
        "limit": limit,
        "offset": offset,
        # important: a caller that sees fewer instances than it expects must be able to tell
        # "this cluster did not answer" from "they were deleted". Without this the two are
        # identical, and an unattended agent will pick the wrong one.
        "partial": fanout.partial,
        "unreachable": fanout.unreachable,
    }


@external_router.get(
    "/instances/{name}",
    summary="The full specification of one instance",
    description=(
        "Configuration, devices after profile expansion, profiles and placement. Searches "
        "every project the key is granted unless one is named."
    ),
)
async def get_integration_instance(
    name: str,
    cluster_id: Optional[str] = Query(None, description="Narrow to one cluster"),
    project: Optional[str] = Query(None, description="Look only in this LXD project"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    clusters = _clusters_for_key(api_key, cluster_id)
    fanout = await _gather_per_cluster(
        _fetch_instance_detail,
        lxd_service,
        clusters,
        "instance_detail",
        name,
        project,
        {target: _granted_projects(api_key, target) for target in clusters},
    )
    matches = _within_granted_projects(api_key, fanout.items)
    if not matches:
        # note: 404 whether the name does not exist or sits in a project the key was not
        # granted. Splitting the two would let a caller enumerate other tenants' instance
        # names one guess at a time.
        raise HTTPException(
            status_code=404,
            detail=(
                f"No instance named {name} in the clusters and projects this key may reach"
                + (
                    " (these clusters did not answer, so the name may exist on one of them: "
                    + ", ".join(m["cluster_id"] for m in fanout.unreachable) + ")"
                    if fanout.unreachable
                    else ""
                )
            ),
        )
    if len(matches) > 1:
        # A name is unique per project, not per fleet. Say so rather than picking one.
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} exists on more than one cluster or project: "
                + ", ".join(f"{m['cluster_id']}/{m['project']}" for m in matches)
                + ". Narrow the request with cluster_id and project."
            ),
        )
    return matches[0]


@external_router.get(
    "/operations/{operation_id}",
    summary="The state of a long-running cluster operation",
    description=(
        "What happened to an operation started with \"wait\": false, or one that outlived the "
        "response budget. Poll this instead of retrying the request."
    ),
)
async def get_integration_operation(
    operation_id: str,
    cluster_id: str = Query(..., description="The cluster the operation was started on"),
    project: str = Query("default", description="The LXD project the operation belongs to"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    if cluster_id not in _clusters_for_key(api_key, cluster_id):
        raise HTTPException(status_code=403, detail=f"No permission for cluster {cluster_id}")
    allowed = _granted_projects(api_key, cluster_id)
    if allowed is not None and project not in allowed:
        raise HTTPException(status_code=403, detail=f"No permission for project {project}")
    try:
        return await run_blocking(
            fetch_operation_status,
            lxd_service,
            cluster_id,
            operation_id,
            project,
            timeout=RAW_PROXY_TIMEOUT,
            label=f"integration_operation[{cluster_id}]",
            executor=proxy_executor,
            max_in_flight=PROXY_MAX_IN_FLIGHT,
        )
    except BlockingTimeout as timeout_error:
        raise HTTPException(status_code=504, detail=str(timeout_error))
    except BlockingPoolFull as pool_error:
        raise HTTPException(status_code=503, detail=str(pool_error))
    except HTTPException:
        raise
    except Exception as e:
        # important: an id LXD does not know raised out of here into the generic 500 handler,
        # which the error sanitiser then renders as "Internal server error". A caller polling
        # an operation that has already been reaped cannot act on that; it has to be able to
        # tell "gone" from "the server is broken".
        code = classify_proxy_error(e)
        if code == "not_found":
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No operation {operation_id} on {cluster_id}. Completed operations are "
                    "reaped by LXD, so a poll that starts too late sees this rather than a "
                    "result: check the resource itself instead."
                ),
            )
        if code == "cluster_unavailable":
            raise HTTPException(status_code=503, detail=unreachable_detail(cluster_id))
        logger.warning("Operation lookup failed on %s: %s", cluster_id, e)
        raise HTTPException(status_code=502, detail=f"Could not read operation {operation_id}")


@external_router.get(
    "/regions",
    summary="Get integration region coordinates",
    description="Return non-sensitive geographic region coordinates for clusters allowed by the API key.",
)
async def get_integration_regions(
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
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
    api_key: Dict[str, Any] = Depends(require_capability("lxd.proxy")),
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

        assert_proxy_path_allowed(request.path)
        # important: reaching inside a guest is a different power from managing it, and this path
        # never asked for it. `_REFUSED_PROXY_PREFIXES` holds only `/certificates`, so
        # `POST /1.0/instances/<n>/exec` ran arbitrary commands as root for any key with
        # `lxd.proxy` + `write`, and a GET of `/files` needed only `read`. Both were demonstrated
        # against this deployment before the fix. The capability is never inherited by a legacy
        # key, so this closes the door for the 17 of 22 keys that hold everything by default.
        try:
            assert_guest_access_allowed(request.path, api_key)
        except HTTPException as refusal:
            # note: a refused attempt to reach inside a guest is the record most worth having.
            # Success is expected traffic; a key trying a path it was never granted is the
            # signal, and it is invisible if only successes are written.
            _audit_key_action(
                api_key,
                action=_proxy_audit_action(method, request.path),
                target_id=request.path[:512],
                cluster_id=request.cluster_id,
                project=project,
                result="failure",
                details={"method": method, "refused": "capability_not_granted"},
                external_user_email=request.external_user_email,
                error_message=str(refusal.detail),
            )
            raise
        # important: the project check above binds the REQUEST to a granted project, which does
        # not bound a resource whose identity is itself a project name. A key granted only
        # "tenant-a" could send DELETE /1.0/projects/tenant-b with project=tenant-a and pass
        # every check here. Same for a cluster-wide storage pool.
        assert_project_scope_allows_path(request.path, method, allowed_projects)

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
            client = await run_blocking(
                lxd_service.connection_manager.get_client,
                request.cluster_id,
                timeout=PROXY_CONNECT_TIMEOUT,
                label=f"get_client[{request.cluster_id}]",
                executor=proxy_executor,
                max_in_flight=PROXY_MAX_IN_FLIGHT,
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
                    # important: compare on the DECODED key. The raw split leaves "%70roject"
                    # distinct from "project", so the pop below missed it while LXD's own HTTP
                    # layer decoded it and honoured the smuggled project. Normalising here means
                    # one spelling reaches the check.
                    query_params[unquote(key)] = value

        # important: `project` is authorised above from request.project, so a copy of it
        # inside `path` is not the caller's to set. Dropping it closes a scope bypass: a key
        # allowed only on "default" could otherwise declare project="default" (passing the
        # check, and skipping the override below because it IS default) while sending
        # ?project=<other> in the path, which LXD honours because these certs are registered
        # unrestricted. Every other query key is legitimate passthrough and is left alone.
        query_params.pop("project", None)

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
        
        # important: this reaches into client.api.session, which bypasses pylxd's _APINode and
        # therefore the timeout _APINode would have injected. The socket timeout is passed
        # explicitly, and the call runs off the event loop: an LXD endpoint that accepts TCP
        # and then goes silent must degrade this one cluster, never the whole process.
        try:
            response = await run_blocking(
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

            logger.info(
                f"Raw proxy: {method} {request.path} on {request.cluster_id}/{project} "
                f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')} "
                f"-> {status_code}"
            )
            _audit_raw_proxy(
                api_key, request, method, project,
                "success" if status_code < 400 else "failure",
                lxd_status=status_code,
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

        except BlockingTimeout as timeout_error:
            health.record_failure(request.cluster_id, str(timeout_error))
            _audit_raw_proxy(api_key, request, method, project, "failure", error="timeout")
            logger.warning(
                f"Raw proxy timeout: {method} {request.path} on {request.cluster_id}/{project} "
                f"after {RAW_PROXY_TIMEOUT}s"
            )
            return RawProxyResponse(
                status_code=504,
                data=None,
                error=str(timeout_error),
            )
        except BlockingPoolFull as pool_error:
            _audit_raw_proxy(api_key, request, method, project, "failure", error="overloaded")
            return RawProxyResponse(
                status_code=503,
                data=None,
                error=str(pool_error),
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
            if isinstance(api_error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                health.record_failure(request.cluster_id, str(api_error))
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


def _still_running_message(operation: str, cluster_id: str) -> str:
    """What to say when we stopped waiting but the cluster did not stop working.

    important: "timed out" alone is what makes an unattended caller retry, and a retried create
    collides with the instance the first attempt is still building. Every timeout path uses this.

    note: the budget and the advice both differ by operation. Exec has its own, shorter budget
    and no asynchronous form, so telling an exec caller to resend with `"wait": false` would send
    it to a flag that operation does not read.
    """
    if operation in GUEST_ACCESS_OPERATIONS:
        return (
            f"{operation} exceeded the {EXEC_TIMEOUT:g}s exec budget and may still be running "
            f"inside the instance on {cluster_id}. Exec is synchronous, so there is no operation "
            "id to poll: check the command's effects instead, and run genuinely long work as a "
            "service rather than through exec."
        )
    return (
        f"{operation} exceeded the {PROXY_OP_TIMEOUT:g}s response budget and may still be "
        f"running on {cluster_id}. Do not retry: poll GET /api/v1/integrations/instances to see "
        f'whether it completed, or send this request again with "wait": false to receive an '
        f"operation id up front."
    )


@external_router.post(
    "/proxy",
    response_model=ProxyResponse,
    summary="Proxy LXD operation",
    description="Execute an LXD operation through Orcastra proxy. Requires valid API key with appropriate permissions.",
)
async def proxy_operation(
    request: ProxyRequest,
    api_key: Dict[str, Any] = Depends(require_capability("lxd.proxy")),
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
        
        # An unknown operation is refused here rather than after the client is built, so a
        # typo costs a 400 instead of a connection attempt. Cluster and project authorization
        # already happened above, so this leaks nothing an integrator cannot read in the docs.
        operation = request.operation
        required_permission = PROXY_OPERATION_LEVELS.get(operation)
        if required_permission is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown operation: {operation}. "
                    f"Supported: {', '.join(sorted(PROXY_OPERATION_LEVELS))}"
                ),
            )

        # important: hierarchical, not membership. This line tested `required in granted` and
        # therefore refused every operation to the 33 of 38 grants in the fleet that hold
        # exactly ["admin"], because "read" is not in ["admin"]. raw_proxy was migrated to
        # assert_cluster_permission and this was not, so the whole /proxy surface was dead
        # while the raw proxy beside it worked. tests/test_cluster_scope_ratchet.py now covers
        # both.
        assert_cluster_permission(api_key, request.cluster_id, required_permission)

        # important: an operation that reaches inside a guest costs a capability of its own, on
        # top of lxd.proxy and the level. It is never inherited by a key issued before
        # capabilities existed and never seeded by an application type, so an operator has to
        # grant it deliberately. Same rule the raw proxy path guard applies to /exec.
        guest_capability = GUEST_ACCESS_OPERATIONS.get(operation)
        if guest_capability is None and operation in ("update_instance", "create_instance"):
            # important: `raw.lxc` with an lxc hook runs as root on the HOST, and a disk device
            # sourced from a host path mounts it into the guest. Both were reachable at `write`
            # alone, which would have left the new capability guarding the front door of a room
            # with an open window.
            guest_capability = config_reaches_inside(
                (request.params or {}).get("config"), (request.params or {}).get("devices")
            )
        if guest_capability:
            try:
                assert_explicit_capability(api_key, guest_capability)
            except HTTPException as refusal:
                # The same record the raw proxy writes for the same refusal. A key trying to
                # reach inside a guest it was never granted is the signal worth alerting on, and
                # it is invisible if only the successful calls are written.
                _audit_key_action(
                    api_key,
                    action=_PROXY_AUDIT_ACTIONS[guest_capability],
                    target_id=str(request.params.get("name", "")),
                    cluster_id=request.cluster_id,
                    project=request.project,
                    result="failure",
                    details={
                        "operation": operation,
                        "command": request.params.get("command"),
                        "refused": "capability_not_granted",
                    },
                    external_user_email=request.external_user_email,
                    error_message=str(refusal.detail),
                )
                raise

        # important: authorising request.project binds the ambient project, not the target. These
        # operations name their target in params, and a project is not bounded by a project, so a
        # key granted only "tenant-a" could delete_project "tenant-b" and pass every check above.
        # A storage pool is cluster-wide and takes a caller-chosen source path on the LXD host.
        # Reaching them needs a grant that is not project-limited, which is what `*` means.
        if operation in CLUSTER_WIDE_OPERATIONS and not project_grant_is_unrestricted(
            perm.get("projects", ["default"])
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{operation} acts on a resource no project contains, so it needs a key "
                    f"granted all projects on {request.cluster_id}. This key is limited to "
                    f"{sorted(perm.get('projects', ['default']))}."
                ),
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

        # Every branch below is a blocking pylxd call, and the waiting ones poll an LXD
        # operation to completion. Inline on the loop that is the same failure mode as the
        # raw proxy, with a longer tail.
        try:
            result_data = await run_blocking(
                execute_proxy_operation,
                lxd_service,
                request.cluster_id,
                operation,
                request.params or {},
                request.project,
                request.wait,
                # note: exec holds a worker for as long as the command runs, so it gets its own
                # budget rather than the operation one. A command that needs longer is a job.
                timeout=EXEC_TIMEOUT if operation == "exec_instance" else PROXY_OP_TIMEOUT,
                label=f"proxy_operation[{operation} {request.cluster_id}]",
                # note: a runaway exec holds its worker until the command ends, because
                # run_blocking frees the caller and not the thread. Its own pool means the blast
                # radius of that is other execs rather than every read on this router.
                executor=exec_executor if operation == "exec_instance" else proxy_executor,
                max_in_flight=(
                    EXEC_MAX_IN_FLIGHT if operation == "exec_instance" else PROXY_MAX_IN_FLIGHT
                ),
            )
            health.clear(request.cluster_id)
        except BlockingTimeout as timeout_error:
            # important: exec lands HERE, not in the generic handler below, so its audit record
            # and its "still running" message have to be written on this path too. They were only
            # on the other one, which the QA run happened to reach because a socket read expired
            # first. `exec_instance` is also not in PROXY_WAIT_OPERATIONS, so without this it fell
            # through to record_failure and tripped the process-wide breaker: one integration's
            # slow command would make the cluster read as offline for every other caller.
            if operation == "exec_instance":
                _audit_key_action(
                    api_key,
                    action="integration.instance.exec",
                    target_id=str(request.params.get("name", "")),
                    cluster_id=request.cluster_id,
                    project=request.project,
                    result="failure",
                    details={
                        "command": request.params.get("command"),
                        "cwd": request.params.get("cwd"),
                        "run_as_user": request.params.get("user"),
                        "outcome": "timeout",
                    },
                    external_user_email=request.external_user_email,
                    error_message="timeout",
                )
                logger.warning(
                    "Exec timeout: %s on %s after %ss",
                    request.params.get("name"), request.cluster_id, EXEC_TIMEOUT,
                )
                return ProxyResponse(
                    success=False,
                    error_code="timeout",
                    error=_still_running_message(operation, request.cluster_id),
                )
            long_running = operation in PROXY_WAIT_OPERATIONS and request.wait
            # important: a long operation is not an unreachable cluster. Tripping the breaker
            # here made one slow create return 503 for every other caller of that cluster for
            # a full TTL, which is the mistake _gather_per_cluster already documents avoiding
            # on its own timeout path. Only a read that ran out of time still counts as a
            # symptom worth recording.
            if not long_running:
                health.record_failure(request.cluster_id, str(timeout_error))
            logger.warning(
                f"Proxy operation timeout: {operation} on {request.cluster_id} "
                f"after {PROXY_OP_TIMEOUT}s"
            )
            # This path mutates cluster state on behalf of an external system, where nobody is
            # watching a UI to notice a stale list. Drop the cached reads it just invalidated.
            get_cache_manager().invalidate_resource_cache("instances", "vms", "monitoring")
            if long_running:
                return ProxyResponse(
                    success=False,
                    error_code="timeout",
                    error=_still_running_message(operation, request.cluster_id),
                )
            return ProxyResponse(
                success=False, error_code="timeout", error=str(timeout_error)
            )
        except BlockingPoolFull as pool_error:
            return ProxyResponse(
                success=False, error_code="overloaded", error=str(pool_error)
            )
        
        # Log the operation for audit
        logger.info(
            f"Proxy operation: {operation} on {request.cluster_id}/{request.project} "
            f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')}"
        )
        
        # important: list_projects answers about the whole cluster, not about the project the
        # operation ran in, so authorising request.project does not bound it. A key granted
        # only `default` could read every tenant project name, and those names carry customer
        # slugs. The REST read was narrowed in this pass; this is the same leak through the
        # other door.
        if operation == "list_projects" and isinstance(result_data, list):
            allowed = _granted_projects(api_key, request.cluster_id)
            if allowed is not None:
                result_data = [
                    row for row in result_data
                    if isinstance(row, dict) and row.get("name") in allowed
                ]

        if operation == "exec_instance" and isinstance(result_data, dict):
            # important: the command is recorded, the OUTPUT is not. Output is whatever the
            # caller chose to read out of the guest, so an audit index that stored it would
            # become a copy of every secret anyone ever cat-ed. Byte counts and the exit code
            # carry the forensic value without the liability.
            _audit_key_action(
                api_key,
                action="integration.instance.exec",
                target_id=str(request.params.get("name", "")),
                cluster_id=request.cluster_id,
                project=request.project,
                result="success",
                details={
                    "command": result_data.get("command"),
                    "exit_code": result_data.get("exit_code"),
                    "cwd": request.params.get("cwd"),
                    "run_as_user": request.params.get("user"),
                    "stdout_bytes": len(result_data.get("stdout") or ""),
                    "stderr_bytes": len(result_data.get("stderr") or ""),
                    "truncated": result_data.get("truncated"),
                },
                external_user_email=request.external_user_email,
            )

        # An accepted-but-unfinished operation carries its id at the top level too, so a
        # caller does not have to know it is nested in data.
        operation_id = (
            result_data.get("operation_id") if isinstance(result_data, dict) else None
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
        error_code = classify_proxy_error(e)
        if request.operation == "exec_instance":
            # important: a command that outlived the budget still ran. Auditing only the calls
            # that answered in time would leave the longest-running commands, which are the ones
            # most worth reviewing, out of the record entirely.
            _audit_key_action(
                api_key,
                action="integration.instance.exec",
                target_id=str(request.params.get("name", "")),
                cluster_id=request.cluster_id,
                project=request.project,
                result="failure",
                details={
                    "command": request.params.get("command"),
                    "cwd": request.params.get("cwd"),
                    "run_as_user": request.params.get("user"),
                    "outcome": error_code,
                },
                external_user_email=request.external_user_email,
                error_message=error_code,
            )
        if request.operation == "exec_instance" and error_code == "timeout":
            # important: a socket read can still expire before the budget on the pooled path, and
            # the underlying message is a urllib3 string naming the LXD host's address, which is
            # infrastructure an integration has no business learning from a timeout.
            return ProxyResponse(
                success=False,
                error_code=error_code,
                error=_still_running_message(request.operation, request.cluster_id),
            )
        # important: a socket read timeout reaches here rather than as BlockingTimeout, and it
        # carries the same hazard: the cluster kept working after we stopped listening. Observed
        # live, a create that timed out this way left a real instance behind while the caller
        # was told it had failed. Same guidance, whichever timeout fired.
        if error_code == "timeout" and request.operation in PROXY_WAIT_OPERATIONS and request.wait:
            return ProxyResponse(
                success=False,
                error_code=error_code,
                error=_still_running_message(request.operation, request.cluster_id),
            )
        return ProxyResponse(
            success=False,
            error_code=error_code,
            error=str(e),
        )


# ============================================
# Capability Vocabulary Endpoint
# ============================================

class CapabilityOption(BaseModel):
    """One capability an API key can be granted."""
    name: str = Field(..., description="Capability identifier, ex; inventory.read")
    description: str = Field(..., description="What the capability allows, in operator language")
    elevated: bool = Field(
        default=False,
        description="Needs a deliberate decision rather than an application-type default",
    )
    # note: the reason belongs on the server because there is more than one. The create form used
    # to label every elevated capability "Writes access control", which is true of org.sync and
    # tenant.provision and false of the two that reach inside a guest.
    elevated_reason: Optional[str] = Field(
        default=None, description="Why this capability is marked, in operator language"
    )
    never_inherited: bool = Field(
        default=False,
        description=(
            "Never granted by an application type and never inherited by a key issued before "
            "capabilities existed; an operator has to grant it deliberately"
        ),
    )


class ApplicationTypeOption(BaseModel):
    """One application type and the capabilities it seeds."""
    name: str = Field(..., description="Application type identifier, ex; mcp")
    default_capabilities: List[str] = Field(
        default_factory=list,
        description="Capabilities preselected for this type. Not a ceiling.",
    )


class CapabilityVocabularyResponse(BaseModel):
    capabilities: List[CapabilityOption]
    application_types: List[ApplicationTypeOption]


@router.get(
    "/capabilities",
    response_model=CapabilityVocabularyResponse,
    summary="List API key capabilities and application types",
    description="The vocabulary the create-key form renders. Served so the picker cannot drift "
                "from what the external router actually enforces.",
    dependencies=[Depends(require_partner())],
)
async def list_capabilities():
    """Serve the capability table rather than duplicating it in the client.

    The form that grants capabilities and the router that enforces them have to agree. Hard
    coding the list in the frontend means a capability added here is silently ungrantable, and
    one removed here stays on offer. Same reason `available-clusters` exists.
    """
    return CapabilityVocabularyResponse(
        capabilities=[
            CapabilityOption(
                name=name,
                description=description,
                elevated=name in ELEVATED_CAPABILITIES,
                elevated_reason=ELEVATED_REASONS.get(name),
                never_inherited=name in GUEST_ACCESS_CAPABILITIES,
            )
            for name, description in CAPABILITIES.items()
        ],
        application_types=[
            ApplicationTypeOption(name=app_type, default_capabilities=sorted(defaults))
            for app_type, defaults in APP_TYPE_CAPABILITIES.items()
        ],
    )


# ============================================
# Available Clusters Endpoint
# ============================================

def _clusters_from_vault(lxd_service: LXDService, cluster_ids: List[str]) -> List[AvailableCluster]:
    """Name and type every cluster straight from its registration, with no state.

    The fallback for when reachability could not be established. Costs one Vault read per
    cluster, which is why it is not the ordinary path: an availability row already carries the
    display name, the endpoint and the type, resolved by the same code the Clusters page uses.

    important: the keys are `display_name` and `type`. This handler used to ask for `name` and
    `server_type`, which `save_cluster_metadata` has never written, so every cluster rendered
    as its raw id and claimed to be LXD. It also filtered on a `status` key that is not stored
    either, making that filter unreachable while the endpoint's description promised it.
    """
    rows: List[AvailableCluster] = []
    for cluster_id in cluster_ids:
        metadata = {}
        try:
            metadata = lxd_service.cert_manager.get_cluster_info(cluster_id).get("metadata", {})
        except Exception as exc:
            # A cluster is never dropped for a failed metadata read. An operator who cannot see
            # a cluster concludes it was deregistered, and a grant on a cluster that is not
            # listed cannot be revoked from the form.
            logger.warning(f"Could not get cluster info for {cluster_id}: {exc}")
        rows.append(
            AvailableCluster(
                id=cluster_id,
                name=lxd_status.resolve_display_name(cluster_id, metadata.get("display_name")),
                endpoint=metadata.get("endpoint"),
                server_type=metadata.get("type"),
            )
        )
    return rows


@router.get(
    "/available-clusters",
    response_model=AvailableClustersResponse,
    summary="List available clusters for current user",
    description="Clusters the current user can scope an API key to, each carrying its current "
                "connection status. Filtered by the user's organization scope. A cluster that "
                "is not answering is listed and labelled, never omitted.",
    dependencies=[Depends(require_partner())],
)
async def list_available_clusters(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List clusters available to the current user for API key assignment.

    important: status is an enrichment, not the payload. If reachability cannot be established
    the clusters are still returned, with a null status, because this endpoint's job is to say
    what a credential may be scoped to. Failing the whole read would empty the picker and make
    an existing grant unrevocable, which is a worse answer than "we could not tell".
    """
    try:
        cluster_ids = lxd_service.get_node_names()
        if not cluster_ids:
            return AvailableClustersResponse(clusters=[])

        try:
            availability = await lxd_status.resolve_availability(lxd_service)
            by_id = {row.name: row for row in availability.clusters}
        except Exception as exc:
            logger.warning(f"Cluster reachability unavailable for available-clusters: {exc}")
            by_id = {}

        if not by_id:
            # note: off the loop. This is one Vault round trip per cluster, and it only runs when
            # reachability is unavailable, which is exactly when the box is already unwell.
            return AvailableClustersResponse(
                clusters=await asyncio.to_thread(_clusters_from_vault, lxd_service, cluster_ids)
            )

        clusters: List[AvailableCluster] = []
        for cluster_id in cluster_ids:
            row = by_id.get(cluster_id)
            if row is None:
                # Registered but absent from the sweep. Listed with no state rather than hidden.
                clusters.extend(
                    await asyncio.to_thread(_clusters_from_vault, lxd_service, [cluster_id])
                )
                continue
            clusters.append(
                AvailableCluster(
                    id=cluster_id,
                    name=row.display_name,
                    # note: the availability layer uses the literal "unknown" when it has no
                    # endpoint. Passing that through prints a word that reads as a fact about
                    # the cluster rather than a gap in what we know.
                    endpoint=None if row.endpoint == "unknown" else row.endpoint,
                    server_type=row.node_type,
                    status=row.status,
                    reason_code=row.reason_code,
                    reason=row.reason,
                    checked_at=row.checked_at,
                )
            )
        return AvailableClustersResponse(clusters=clusters)

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

    # important: every other single-key handler here scopes through _authorize_api_key, and this
    # one took `api_key_id` straight from a query parameter. Any partner could therefore name any
    # key in any organisation and make this server issue an authenticated request to that key's
    # stored callback, then have the response upserted as organizations. Same overlap rule, same
    # 404-for-out-of-scope shape, so an id cannot be probed apart from a nonexistent one.
    metadata = _authorize_api_key(vault_client, api_key_id)

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
        # note: status only. Logging the body put the fetched content into the log,
        # which turns a blind SSRF into a returned one for anyone who can read logs.
        logger.error("External app returned %s for the configured callback", e.response.status_code)
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
    # Same reasoning as SyncDataRequest: several queries per policy, no natural ceiling.
    policies: List[SyncPolicy] = Field(default_factory=list, max_length=1000)


@external_router.post(
    "/external/sync-policies",
    summary="Sync tenant access policies from external app",
    description="Push tenant access policies from OrcaHub to Orcastra. Requires valid API key.",
    tags=["Integrations - External"],
)
async def sync_policies_from_external(
    body: SyncPoliciesRequest,
    api_key: Dict[str, Any] = Depends(require_capability("tenant.provision")),
    db: AsyncSession = Depends(get_db),
):
    """
    Sync tenant access policies from OrcaHub.

    Creates or updates policies based on external_id matching.
    Handles rules and assignments as part of the sync.

    important: this used to authenticate by hand, comparing a plain SHA-256 of the presented
    secret against the stored hash. Keys are stored as HMAC-SHA256 keyed with SECRET_KEY
    (generate_api_key_pair), so the two could never match and this endpoint answered 401 to every
    caller, including OrcaHub. The hand-rolled block also skipped the expiry check and the
    cross-worker revocation tombstone. It now shares the one dependency the rest of this router
    uses, which fixes all three at once.
    """
    from app.models.db_models import (
        TenantAccessPolicy,
        TenantPolicyRule,
        TenantPolicyAssignment,
        OrganizationMember,
    )

    api_key_id = api_key.get("id", "")

    synced_count = 0
    errors = []

    for sync_policy in body.policies:
        try:
            # find org by external_id
            # important: `source` is filtered here as every sibling lookup in this file does.
            # Without it a dashboard-created organisation that happens to carry an external_id
            # was writable through this endpoint, which grants tenants operate and console access
            # to clusters. The ownership test below is the second half.
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
            # important: `if org.api_key_id and ...` skipped the check entirely for an
            # unclaimed organisation, so a NULL owner was an open door rather than a closed one.
            # Writing tenant access policies is an authorization write; an organisation no key
            # has claimed is not one this key may write to.
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
    api_key: Dict[str, Any] = Depends(require_capability("tenant.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Read policies for external app pull sync.

    Carried the same unusable plain-SHA-256 comparison as its sibling above, so it too answered
    401 to every caller. Same fix: the shared dependency.
    """
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    api_key_id = api_key.get("id", "")

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
    api_key: Dict[str, Any] = Depends(require_capability("tenant.provision")),
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
    api_key: Dict[str, Any] = Depends(require_capability("tenant.provision")),
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
