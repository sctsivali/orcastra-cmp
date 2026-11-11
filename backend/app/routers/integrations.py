"""
API router for external integrations.
Endpoints for managing API keys, external app connections (ex; OrcaHub) and delegated access.
"""

import asyncio
import secrets
import hashlib
import hmac
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import partial
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional, List, Dict, Any, Awaitable, Callable, FrozenSet, NamedTuple, Tuple, Type, TypeVar, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Request, Response
from pydantic import AwareDatetime, BaseModel, Field, SecretStr, StringConstraints, ValidationError, field_validator
import requests

from app.services.lxd_service import LXDService, get_lxd_service
from app.services.lxd.proxy_path_params import assert_path_params
from app.services.lxd.project_scope import resolve_owner
from app.services import api_key_usage, cluster_generations, instance_grants, project_deletion
from app.services.integration_cluster_checks import (
    cluster_display_names_sync as _cluster_display_names_sync,
    cluster_infos_sync as _cluster_infos_sync,
    proxy_error_text as _proxy_error_text,
    refuse_a_cluster_not_registered as _refuse_a_cluster_not_registered,
    refuse_new_grants_to_unregistered_clusters as _refuse_new_grants_to_unregistered_clusters,
    reported_grant as _reported_grant,
    unregistered_cluster_ids as _unregistered_cluster_ids,
)
from app.services.lxd import health
from app.services.lxd import registry as cluster_registry
from app.services.lxd import status as lxd_status
from app.services.lxd_certs.storage import stored_location_fields
from app.models.schemas import AvailableCluster, AvailableClustersResponse
from app.services.lxd.proxy import (
    ALLOWED_METHODS,
    API_KEY_CACHE_TTL,
    LAST_USED_WRITE_INTERVAL,
    PROXY_CONNECT_TIMEOUT,
    EXEC_TIMEOUT,
    PROXY_MAX_IN_FLIGHT,
    PROXY_OP_TIMEOUT,
    RAW_PROXY_TIMEOUT,
    SYNC_CLUSTER_TIMEOUT,
    VALIDATE_KEY_TIMEOUT,
    auth_executor,
    proxy_executor,
    raw_request,
    socket_timeout_for,
)
from app.services.activity_logger import get_activity_logger
from app.services.integration_cluster_state import server_type_name, stamp_cluster_rows
from app.services.lxd.availability import require_client, unreachable_detail
from app.services.lxd.fanout import down_text, is_read_timeout, known_down, run_in_slot
from app.services.lxd.lazy_client import connect_or_get_client
from app.services.lxd.failover_config import project_failover_config
from app.services.lxd.limited_write_reach import (
    ChecksNotFinished,
    assert_plan_segments,
    limited_write_reach,
    proxy_write_reach,
    refuse_what_a_limited_write_applies,
)
from app.core.key_pull_sources import assert_key_create_params_allowed, assert_key_pull_source_allowed
from app.services.lxd.cloud_init_reach import cloud_init_changes, proxy_cloud_init_owed, refuse_changed_cloud_init
from app.services.lxd.operation_metadata import redact_operation_response
from app.core.tenant_visibility import used_by_in_projects
from app.services.key_default_view_only import refuse_key_default_operation
from app.services.key_party_bound import (
    forget_creator_bound_of,
    is_party_bounded,
    narrow_to_party_bound,
    project_change_refusal,
    record_key_created_project,
    refuse_an_unisolated_project,
    shared_project_config,
)
from app.services.settle_invalidation import invalidate_when_settled
from app.services.raw_project_delete import delete_through_core, raw_project_delete_target
from app.core.tenant_term import TermDates, TermDatesInvalid, TermDatesStale, merge_and_validate, policy_phase
from app.services import stranded_rules
from app.services.branding import provider_link
from app.services.stranded_rules import raw_stranding_target
from app.services.lxd.raw_proxy_operations import (
    LXD_OPERATION_NOT_FOUND,
    OwnerUnknown,
    addressed_operation_id,
    lxd_operation_id,
    operation_is_visible,
)
from app.services.lxd.proxy_operations import (
    CLUSTER_WIDE_OPERATIONS,
    GUEST_ACCESS_OPERATIONS,
    operation_pool,
    operation_timeout,
    PROXY_OPERATION_LEVELS,
    PROXY_WAIT_OPERATIONS,
    assert_required_params,
    classify_proxy_error,
    execute_proxy_operation,
    LIST_MUTATIONS,
    fetch_operation_status,
    instance_detail,
    instance_summary,
    status_name,
)
from app.core.api_key_expiry import (
    classify_key,
    days_remaining,
    expiry_state,
    is_expired,
    now_utc,
    tally_keys,
    utc_isoformat,
)
from app.core.api_key_permissions import PERMISSION_LEVELS, assert_cluster_permission, refuse_a_paused_grant
from app.core.api_key_capabilities import (
    policy_keys_in_config,
    policy_keys_in_raw_body,
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
    has_capability,
    has_explicit_capability,
)
from app.core import build_identity
from app.core.api_key_body import assert_canonical_proxy_params, canonical_raw_proxy_body
from app.core.guest_data_export import (
    assert_guest_data_path_stays_inside,
    assert_guest_data_stays_inside,
    guest_data_path_capability_for,
)
from app.core.cloud_init_config import assert_cloud_init_paid_for, cloud_init_keys_in
from app.core.api_key_capability_audit import CAPABILITIES_UPDATE_ACTION, capability_audit_fields
from app.core.limited_key_writes import (
    assert_body_within_grant,
    assert_limited_body,
    instance_write_plan,
    limited_write_plan,
    projects_named_in,
    proxy_instance_write,
)
from app.core.raw_proxy_path import assert_server_state_unchanged, canonical_proxy_path
from app.core.storage_bucket_keys import assert_bucket_keys_unreached
from app.core.lxd_segment import lxd_segment
from app.core.lxd_body_names import assert_body_names_addressable
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.url_safety import assert_url_is_fetchable
from app.core.error_sanitizer import sanitize_error
from app.core.json_body import body_spec, read_json_body
from app.middleware.security import consume_api_key_budget, consume_api_key_budget_state
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_scope_cache_key,
    org_scope_dependency,
    reconcile_personal_clusters,
    refuse_grants_on_purchased_clusters,
)
from app.core import swr
from app.core.cache import CAPACITY_GENERATION_KIND, get_cache_manager, get_resource_list_generation
from app.core.swr import Built
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete
from sqlalchemy.exc import IntegrityError
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services import cluster_parties, owner_key_enrollment
from app.services.api_key_grant_bound import (
    already_granted,
    refuse_a_grant_past_the_project_bound,
    refuse_a_project_another_party_holds,
    rule_outside_the_key_grant,
)
from app.services.organization_owners import (
    ADD_ACTION,
    MAX_OWNER_KEYS,
    PRIMARY_ACTION,
    REMOVE_ACTION,
    LastOwner,
    NoAbleOwner,
    OwnerSetFull,
    add_owner,
    check_owner_sets,
    is_owner,
    mark_entry,
    multi_owner_enabled,
    owned_by_clause,
    owner_key_ids,
    owners_of,
    record_owner_change,
    remove_owner,
    serialize_org_owners,
    set_primary,
)
from app.services.integration_roles import apply_upsert_role, may_reconcile, stored_member_role
from app.services.integration_cluster_rebind import (
    claimed_organization_count,
    creator_of,
    rebind_creator_clusters,
)
from app.services.integration_callback_url import APP_URL_SECRET_TAG, bound_callback_url, callback_url_tag
from app.services.integration_key_provisioning import PROVISIONING_CAPABILITY, confirm_provisioning_withdrawal
from app.services.integration_key_ownership import (
    MAX_LISTED_SLUGS,
    TRANSFER_ACTION as OWNER_TRANSFER_ACTION,
    KeyOwnsOrganizations,
    key_state,
    OWNERSHIP_UNKNOWN_DETAIL,
    owned_by_keys,
    owner_key_refusal,
    refuse_if_key_owns_organizations,
    able_owners,
    apply_owner_handoffs,
    OwnerHandoff,
    owned_org_ids_to_lock,
    record_owner_handoffs,
    keys_whose_home_the_user_partners,
    partners_the_home_organization,
    release_key_locks,
    serialize_key_ownership,
    SYNC_CAPABILITIES,
    sync_capabilities_missing,
    transfer_owner,
    uncovered_clusters,
    visible_organization_ids,
)
from app.services.add_on_credential_edits import (
    is_add_on_credential,
    is_current_add_on_credential,
    refuse_delete,
    refuse_grant_edit,
    refuse_secret_rotation,
)
from app.services.shared_cluster_audit import (
    ClusterConfigurationBusy,
    SharedClusterGuardError,
    assert_cluster_safe_to_share,
    serialize_cluster_share,
)
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
    assert_instance_not_migration_fenced,
    assert_project_not_migration_fenced,
)

logger = logging.getLogger(__name__)

# Internal router: JWT-authenticated endpoints for API key management (admin UI)
router = APIRouter(
    prefix="/integrations",
    tags=["Integrations"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# External router: API key-authenticated endpoints for external apps (OrcaHub, etc.)
# No JWT dependency: these endpoints authenticate via X-API-Key-ID/Secret headers or request body
external_router = APIRouter(
    prefix="/integrations",
    tags=["Integrations - External"],
)

VAULT_INTEGRATIONS_PATH = "integrations"

activity_logger = get_activity_logger()


# ============================================
# Pydantic Models
# ============================================

class ClusterPermission(BaseModel):
    """Access a key holds on one cluster, as stored and as returned."""
    # important: no validators here. Stored grants are read back into this model (`_key_info`
    # builds `APIKeyInfo(**metadata)` from raw Vault JSON; `/whoami` and `/validate` build it from
    # stored dicts), so a rule here would turn one legacy grant into a 500 for the whole key list.
    # New grants are validated by `ClusterPermissionRequest`; tighten that one instead.
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
    """Access granted to a key on one cluster, validated on the way in."""
    # important: validators only, no fields, so `model_dump()` writes exactly the keys of the
    # lenient base model to Vault.

    @field_validator("permissions")
    @classmethod
    def _reject_a_grant_that_grants_nothing(cls, value: List[str]) -> List[str]:
        """Refuse an empty or unrecognised grant at the boundary.

        The runtime answers 403 for an empty grant (app/core/api_key_permissions.py); this catches
        the mistake where it is made. An unrecognised name scores as "read" at runtime, so a typo
        would silently become a read grant. The rule lives on the request model because stored
        grants are read back through ClusterPermission.
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

        A name that matches no project gives a key that every project-bounded call refuses. The
        console picks names from the cluster's project list; this holds callers of the API to the
        same rule.
        """
        return normalise_project_grant(value)


class ExternalUserMapping(BaseModel):
    """Maps external user to Orcastra identity."""
    # important: this value is interpolated into a Vault KV path by save_user_mapping_to_vault.
    # Vault normalises the path, so `../../../../clusters/<id>` would let the mapping JSON
    # overwrite a cluster's stored certificate material. The charset guards the boundary;
    # vault_client also percent-encodes the segment.
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
    # `ExternalUserMapping(**data)` from stored JSON while the API-key list is built, so a rule
    # here would 500 that list for a legacy mapping. `UserMappingRequest` is the boundary.
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
    # Unconstrained, unlike the create request: stored keys are read back into this model, so a
    # validator would turn an unfamiliar stored value into a 500 on the key list.
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
    # Derived from expires_at on every read, never stored, so the authentication hot path never
    # has to write to Vault. `expiry_state` is what the card renders; `is_expired` is what an
    # integration branches on.
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
    # Derived from the stored metadata on every read. An add-on installation owns and rotates its
    # own credential, so the key card offers no secret rotation for one.
    is_add_on_credential: bool = Field(
        False, description="Whether an add-on installation issued this key and owns its grant"
    )
    # Filled from the database after the list cache, because what a key owns changes without the
    # key changing. None means it could not be read, which differs from owning nothing.
    owned_organization_count: Optional[int] = Field(
        None, description="Active OrcaHub organizations this key owns, or null when it could not be read"
    )
    owned_organization_slugs: List[str] = Field(
        default_factory=list,
        description=f"Slugs of the owned organizations the caller may see, at most {MAX_LISTED_SLUGS}",
    )
    owned_organization_hidden_count: int = Field(
        0, description="Owned organizations the caller may not see, counted but not named"
    )
    sole_owned_organization_count: Optional[int] = Field(
        None,
        description=(
            "Of those, the ones no other key co-owns, which ending this key would freeze; null when it "
            "could not be read"
        ),
    )


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

        The type seeds the key's capabilities, so an unrecognised value would produce a key that
        grants nothing. Boundary only, like ClusterPermissionRequest.permissions: stored keys never
        pass through here.
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

        None means "use the type's defaults". An explicit empty list gives a key that authenticates
        and can do nothing. An unrecognised name is refused so a typo is caught where it is made.
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
    """Response after creating an API key. Includes the secret, shown only once."""
    api_key: APIKeyInfo
    api_key_id: str = Field(..., description="The API Key ID (public, can be stored)")
    api_key_secret: str = Field(..., description="The API Key Secret (STORE SECURELY, shown only once)")


class APIKeyListResponse(BaseModel):
    """Response for listing API keys."""
    total: int
    active: int
    revoked: int
    # Expired keys are counted here, not in `active`.
    expired: int = 0
    keys: List[APIKeyInfo]


class APIKeyValidateRequest(BaseModel):
    """Request to validate an API key."""
    api_key_id: str = Field(..., description="API Key ID")
    api_key_secret: str = Field(..., description="API Key Secret")


class ServerIdentity(BaseModel):
    """Which build of this server answered.

    Returned only after a credential is verified. The version alone is public on `/health` and
    `/`; the commit and the build date are not.

    Self-reported: it names the build this deployment believes it runs. It is not evidence that
    the deployment is genuine.
    """
    version: Optional[str] = Field(
        default=None, description="Version label the image was built with, null if unstamped"
    )
    commit: Optional[str] = Field(
        default=None, description="Git commit the image was built from, null if unstamped"
    )
    built_at: Optional[str] = Field(
        default=None, description="When the image was built, ISO 8601 UTC, null if unstamped"
    )


class APIKeyValidateResponse(BaseModel):
    """Response for API key validation."""
    valid: bool
    app_name: Optional[str] = None
    app_type: Optional[str] = None
    # Lets an unattended integration see when its own key lapses, on the call it already makes to
    # prove it is connected.
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
    # Set only when a credential was verified. A refusal carries null, so the build stays hidden
    # from anyone who cannot authenticate.
    server: Optional[ServerIdentity] = Field(
        default=None, description="Which build of this server answered"
    )
    # Like `server`, set only when a credential was verified. Names the optional contracts this
    # deployment supports, so an integration can check before calling them.
    features: Optional[List[str]] = Field(
        default=None, description="Optional integration contracts this server supports, such as organization_owner_keys.v1"
    )
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
    # important: bounded. Each element costs a SELECT and its own commit, and each member also an
    # Authentik call, so an unbounded list holds a worker and the connection pool for as long as
    # it runs. No real push approaches these numbers.
    organizations: List[SyncOrganization] = Field(
        default_factory=list,
        max_length=1000,
        description="Organizations to sync from external app"
    )
    app_url: Optional[str] = Field(None, description="External app base URL for callbacks")

    @field_validator("app_url")
    @classmethod
    def _refuse_a_callback_this_server_must_not_call(cls, value: Optional[str]) -> Optional[str]:
        """`app_url` is stored and then fetched by this server, so it is an SSRF sink.

        Three outbound requests use it: the member push in `orcahub_sync.push_members_to_orcahub`,
        which sends the key's plaintext secret; the organization pull in `refresh_organizations`,
        whose response feeds organization creation; and the lifecycle webhook, which carries a
        valid HMAC. Pointed at 169.254.169.254 or an internal Vault, each would deliver a
        credential or inject data. https is required because every one carries a credential.

        The capability gate on this field decides who may set it, not whether the value is safe.
        """
        if value is None:
            return None
        return assert_url_is_fetchable(value, field="app_url", require_https=True)
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
    # The default is synchronous. wait=false is for work that can outlive PROXY_OP_TIMEOUT, ex; a
    # create that has to pull an image. It returns the LXD operation id immediately and the caller
    # polls /integrations/operations/{id}.
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


def _raw_proxy_fenced_write_target(request: RawProxyRequest) -> Optional[str]:
    """Return the exact instance mutated by a raw request, except a risk-reducing stop."""
    method = request.method.upper()
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    segments = list(canonical_proxy_path(request.path).segments)
    if not segments or segments[0] not in {
        "instances",
        "containers",
        "virtual-machines",
    }:
        return None

    if len(segments) == 1:
        if method != "POST" or not isinstance(request.body, dict):
            return None
        name = request.body.get("name")
    else:
        name = segments[1]
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name:
        return None

    if len(segments) == 3 and segments[2] == "state" and isinstance(request.body, dict):
        action = request.body.get("action")
        # Stop remains available for incident containment. Freeze is still a target state
        # mutation and can interrupt the authoritative workload after cutover.
        if isinstance(action, str) and action.lower() == "stop":
            return None
    return name


def _raw_proxy_fenced_project(request: RawProxyRequest, ambient_project: str) -> Optional[str]:
    """Return the project hierarchy whose migration dependencies a raw write can drift."""
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    segments = list(canonical_proxy_path(request.path).segments)
    if not segments:
        return None

    if segments[0] == "projects":
        if len(segments) >= 2:
            target = segments[1]
        elif request.method.upper() == "POST" and isinstance(request.body, dict):
            target = request.body.get("name")
        else:
            return None
        if isinstance(target, str) and target and target not in {".", ".."} and "/" not in target:
            return target
        return None

    project_dependency_families = {
        "profiles",
        "networks",
        "network-acls",
        "network-address-sets",
        "network-zones",
        "images",
    }
    if segments[0] in project_dependency_families:
        return ambient_project
    if segments[0] == "storage-pools" and any(
        segment in {"volumes", "buckets"} for segment in segments[2:]
    ):
        return ambient_project
    return None


def _raw_proxy_has_cluster_wide_write(request: RawProxyRequest) -> bool:
    """Identify infrastructure writes that can invalidate every project on a cluster."""
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    segments = list(canonical_proxy_path(request.path).segments)
    if not segments:
        return False
    if segments[0] in {"cluster", "cluster-members"}:
        return True
    if segments[0] == "networks":
        return True
    if segments[0] != "storage-pools":
        return False
    return not any(segment in {"volumes", "buckets"} for segment in segments[2:])


# Raw-proxy families LXD keeps in `default` for a project that does not isolate them.
_RAW_FEATURE_FAMILIES = {
    "images": "images",
    "profiles": "profiles",
    "network-acls": "network-acls",
    "network-zones": "network-zones",
}


def _raw_proxy_landing(request: RawProxyRequest, project: str, client: Any, endpoint: str):
    """An async callable answering where a raw write lands, or None when it lands where the key
    named. Only asked by refuse_key_default_operation once the cluster is held by another party."""
    method = request.method.upper()
    mutating = method in {"POST", "PUT", "PATCH", "DELETE"}
    segments = list(canonical_proxy_path(request.path).segments)
    if not segments:
        return None

    operation_id = addressed_operation_id(canonical_proxy_path(request.path))
    if operation_id is not None and (mutating or guest_access_capability_for(request.path)):
        async def _operation_project() -> List[str]:
            in_default = await run_blocking(
                operation_is_visible, client.api.session, endpoint, operation_id,
                frozenset({"default"}), socket_timeout_for(client),
                timeout=RAW_PROXY_TIMEOUT, label=f"raw_proxy[operation project {request.cluster_id}]",
                executor=proxy_executor, max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
            return ["default"] if in_default else []

        return _operation_project

    if not mutating or project == "default":
        return None
    collection = _RAW_FEATURE_FAMILIES.get(segments[0])
    if collection is None and segments[0] == "storage-pools" and len(segments) >= 4 \
            and segments[2] == "volumes" and segments[3] == "custom":
        collection = "storage-volumes"
    if collection is None:
        return None

    async def _store_project() -> List[str]:
        return [await run_blocking(
            resolve_owner, client, collection, project,
            timeout=RAW_PROXY_TIMEOUT, label=f"raw_proxy[store project {request.cluster_id}]",
            executor=proxy_executor, max_in_flight=PROXY_MAX_IN_FLIGHT,
        )]

    return _store_project


async def _party_bounded_project_refusal(
    api_key: Dict[str, Any], cluster_id: str, project_name: str, *, creating: bool
) -> Optional[HTTPException]:
    from app.core.database import async_session_maker

    try:
        async with async_session_maker() as db:
            return await project_change_refusal(api_key, cluster_id, project_name, creating=creating, db=db)
    except HTTPException as refusal:
        return refusal
    except Exception as exc:  # noqa: BLE001 - an unreadable party is not a pass
        logger.warning("Project change check unavailable on %s: %s", cluster_id, type(exc).__name__)
        return HTTPException(status_code=503, detail="Authentication backend unavailable")


def _raw_party_bounded_project(api_key: Dict[str, Any], request: RawProxyRequest, method: str) -> Optional[str]:
    """The project a raw project create or delete names, for a party-bounded key; else None.

    Only `POST /projects` (the body's name) and `DELETE /projects/<name>`. A change to a project's
    config, and anything below its address, keeps the project-limited rules: the browser lets a
    Partner change only a project's description and limits.
    """
    if not is_party_bounded(api_key, request.cluster_id) or method not in {"POST", "DELETE"}:
        return None
    segments = list(canonical_proxy_path(request.path).segments)
    if not segments or segments[0] != "projects":
        return None
    if len(segments) == 1 and method == "POST" and isinstance(request.body, dict):
        name = request.body.get("name")
        return str(name) if isinstance(name, str) and name else ""
    if len(segments) == 2 and method == "DELETE":
        return segments[1]
    return None


def _raw_instance_config_write(request: RawProxyRequest) -> bool:
    """An instance create, config change, restore, rename, move or copy: the writes the browser
    refuses a Partner in a project that does not isolate itself. Power, exec and files are not."""
    method = request.method.upper()
    segments = list(canonical_proxy_path(request.path).segments)
    if not segments or segments[0] not in {"instances", "containers", "virtual-machines"}:
        return False
    if len(segments) == 1:
        return method == "POST"
    if len(segments) == 3 and segments[2] == "rebuild":
        return method == "POST"
    return len(segments) == 2 and method in {"POST", "PUT", "PATCH"}


def _raw_isolation_targets(request: RawProxyRequest, project: str) -> List[str]:
    """The projects a raw write by a party-bounded key must find isolated: the request's own for an
    instance create, change, restore, move or rebuild, and every project the body names for an
    instance or image write (a move's target, a copy's or an image's source)."""
    method = request.method.upper()
    if method not in {"POST", "PUT", "PATCH"}:
        return []
    segments = list(canonical_proxy_path(request.path).segments)
    family = segments[0] if segments else ""
    targets: List[str] = [project] if _raw_instance_config_write(request) else []
    if family in {"instances", "containers", "virtual-machines", "images"} and isinstance(request.body, dict):
        targets.extend(projects_named_in(request.body))
    return sorted({t for t in targets if t})


def _project_isolation_config(client: Any, project: str) -> Dict[str, Any]:
    """Blocking: the project's config, for the isolation check. A test seam."""
    from app.services.lxd.project_scope import list_project_configs

    return list_project_configs(client).get(project) or {}


async def _refuse_an_unisolated_target(client: Any, cluster_id: str, projects: Any) -> None:
    """409 when a party-bounded key writes into, or names in a body, a project that shares
    `default`'s volumes, images, profiles or networks, as the browser refuses a Partner.

    `default` is judged like any other project. The view-only rule counts a teammate's member row
    as the caller's side and this bound does not, and the browser refuses a Partner's write into
    an unrestricted `default` on a shared cluster."""
    names = [projects] if isinstance(projects, str) else list(projects)
    for name in sorted({n for n in names if n}):
        config = await run_blocking(
            _project_isolation_config, client, name,
            timeout=RAW_PROXY_TIMEOUT, label=f"key_party_bound[isolation {cluster_id}]",
            executor=proxy_executor, max_in_flight=PROXY_MAX_IN_FLIGHT,
        )
        refuse_an_unisolated_project(cluster_id, config)


def _rule_reach(project_name: Optional[str]) -> str:
    """The project a tenant rule reaches for the view-only `default` rule: `*` includes `default`."""
    return "default" if (project_name or "").strip() in ("", "*") else str(project_name)


def _raw_proxy_default_reach(request: RawProxyRequest, ambient_project: str) -> List[str]:
    """The projects a raw request changes, or opens a guest session or file in, for the view-only
    `default` rule (services/key_default_view_only.py). Empty for a plain read.

    A project's own address (`/projects/<name>`) names it; a cluster-wide write names none
    (for a key those need a `*` grant, _PROJECT_UNBOUNDABLE_PREFIXES); anything else lands in the
    request's project, or where _raw_proxy_landing says LXD keeps it. A body that names a project
    (a move into `default`, a copy out of it) adds that project too.
    """
    method = request.method.upper()
    mutating = method in {"POST", "PUT", "PATCH", "DELETE"}
    if not mutating and not (
        guest_access_capability_for(request.path) or guest_data_path_capability_for(request.path)
    ):
        return []
    segments = list(canonical_proxy_path(request.path).segments)
    touched: List[str] = []
    if segments and segments[0] == "projects":
        target = segments[1] if len(segments) >= 2 else None
        if target is None and method == "POST" and isinstance(request.body, dict):
            target = request.body.get("name")
        if isinstance(target, str) and target:
            touched.append(target)
    elif not _raw_proxy_has_cluster_wide_write(request):
        touched.append(ambient_project)
    if mutating and isinstance(request.body, dict):
        touched.extend(projects_named_in(request.body))
    return touched


async def _assert_proxy_write_is_not_fenced(
    *, cluster_id: str, project: str, instance_name: str
) -> None:
    try:
        await assert_instance_not_migration_fenced(
            cluster_id=cluster_id,
            project=project,
            instance_name=instance_name,
        )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


async def _assert_proxy_project_write_is_not_fenced(
    *, cluster_id: str, project: str
) -> None:
    try:
        await assert_project_not_migration_fenced(
            cluster_id=cluster_id,
            project=project,
        )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


async def _assert_proxy_cluster_write_is_not_fenced(*, cluster_id: str) -> None:
    try:
        await assert_cluster_not_migration_fenced(cluster_id=cluster_id)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


class ProxyResponse(BaseModel):
    """Response from proxied operation."""
    success: bool
    # Set only on failure. A failure inside the cluster answers 200 with success=false, and the
    # code lets an unattended caller tell "it is not there" from "retry, the cluster is down"
    # without parsing the message.
    error_code: Optional[str] = None
    # important: the list_* operations return a list. Typed as Dict alone, every one of them fails
    # response validation and reports success:false. Widened rather than wrapped so the payload
    # keeps the shape the operation produces.
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


API_KEY_STORE_UNAVAILABLE = "api_key_store_unavailable"


def _vault_did_not_answer(exc: BaseException) -> bool:
    """True when Vault failed as a store (sealed, down, unreachable, or refusing the dashboard's
    own token), never for "not there". A 404 is a missing key and a 403 the policy explains is a
    refusal reported as it is (core/vault_token.py decides which 403 is which)."""
    from app.core import vault_token

    return vault_token.store_failed(exc)


def _key_store_unavailable() -> HTTPException:
    """The 503 for a key read, list or write that Vault could not serve.

    A sealed Vault must not read as a wrong key (401) or an empty key list (200). /clusters answers
    503 for the same outage (core/registry_guard.py).
    """
    from app.core.registry_guard import RETRY_AFTER_SECONDS

    return HTTPException(
        status_code=503,
        detail={
            "error": API_KEY_STORE_UNAVAILABLE,
            "message": (
                "API keys cannot be read or written because the secret store (Vault) is sealed, "
                "unavailable or refusing the dashboard's token. Try again once Vault answers."
            ),
        },
        headers={"Retry-After": str(RETRY_AFTER_SECONDS), "Cache-Control": "no-store"},
    )


def save_api_key_to_vault(
    vault_client, key_id: str, metadata: Dict[str, Any], secret_hash: str,
    raw_secret: Optional[str] = None,
):
    """Save API key metadata, hash, and optionally raw secret to Vault."""
    metadata_saved = False
    try:
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata
        )
        metadata_saved = True
        # The raw secret is kept for callback auth.
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
        unavailable = _vault_did_not_answer(e)
        if metadata_saved:
            # important: metadata without a secret lists as an Active key that can never
            # authenticate, which an operator reads as a live credential. Best effort: the write
            # that just failed may mean Vault is gone, and then the key is left as it was.
            try:
                vault_client.vault_delete_metadata(
                    f"secret/metadata/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
                )
            except Exception as cleanup:  # noqa: BLE001 - the mint already failed; report that
                logger.warning(f"API key {key_id} left its metadata without a secret: {cleanup}")
        if unavailable:
            raise _key_store_unavailable() from e
        raise HTTPException(status_code=500, detail="Could not save API key")


def save_api_key_secret_to_vault(vault_client, key_id: str, secret_hash: str, raw_secret: str) -> None:
    """Replace a key's secret document and nothing else, which is all a rotation writes.

    The grant is a separate document and stays untouched, so a rotation cannot drop the
    `capabilities` field or any other part of it. The raw copy is what the OrcaHub callbacks sign
    with.
    """
    vault_client.vault_kv_put(
        f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret",
        {"hash": secret_hash, "raw": raw_secret},
    )


def get_api_key_from_vault(vault_client, key_id: str, *, use_cache: bool = True) -> tuple[Dict[str, Any], str]:
    """Get API key metadata and secret hash from Vault.

    important: use_cache=False for every read a write is built from. The metadata path is mirrored
    in the shared Redis cache, and a reader can refill the mirror with a copy from before a write,
    so a read-modify-write from the mirror can put a revoked key back. The secret path is never
    mirrored.
    """
    try:
        metadata = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata", use_cache=use_cache
        )
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
        )
        return metadata, secret_data.get("hash", "")
    except Exception as e:
        logger.error(f"Failed to get API key from Vault: {e}")
        if _vault_did_not_answer(e):
            raise _key_store_unavailable() from e
        raise HTTPException(status_code=404, detail="API key not found")


def _read_key_metadata_blocking(vault_client, key_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """A key's metadata document, or None and why: "missing" when Vault has no such key, else "unknown".

    The metadata path only: nothing that reports or checks an owner key needs the secret document,
    and get_api_key_from_vault reads both.

    Read with use_cache=False. The metadata path is mirrored in the shared Vault cache, and a copy
    from before a revoke would let a transfer hand an organization to a revoked key.
    """
    from app.core.vault_client import VaultClientError

    try:
        metadata = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata", use_cache=False
        )
    except (VaultClientError, HTTPException) as exc:
        return None, "missing" if getattr(exc, "status_code", None) == 404 else "unknown"
    except Exception:  # noqa: BLE001 - an unreadable store is reported, never raised to the list
        return None, "unknown"
    if not isinstance(metadata, dict) or not metadata:
        return None, "missing"
    return metadata, None


def list_api_keys_from_vault(vault_client) -> List[Dict[str, Any]]:
    """List all API key metadata from Vault."""
    try:
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

        all_metadata = []
        for key_id in keys:
            key_id = key_id.rstrip("/")
            try:
                metadata = vault_client.vault_read(
                    f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
                )
                all_metadata.append(metadata)
            except Exception as e:
                # A key whose document went away is skipped, and so is one document Vault cannot
                # serve while it answered the LIST. Vault going away part way (sealed, or no
                # answer at all) is not a shorter list.
                status = getattr(e, "status_code", None)
                # 403 counts only when the dashboard's own token is refused (_vault_did_not_answer
                # says so): then every remaining document would fail the same way.
                if _vault_did_not_answer(e) and status in (None, 403, 503):
                    raise
                logger.error(f"API key {key_id} could not be read and is left out of the list: {e}")
                continue
        
        return all_metadata
    except Exception as e:
        logger.warning(f"Failed to list API keys from Vault: {e}")
        # important: an unanswered LIST is not an empty key store. Answered as one, a sealed
        # Vault shows every operator zero keys.
        if _vault_did_not_answer(e):
            raise _key_store_unavailable() from e
        return []


def api_key_vault_records(vault_client, key_id: str) -> List[str]:
    """Every KV v2 metadata path under one API key, descriptor last.

    The descriptor goes last so a delete that stops part way still leaves the key findable, and
    retryable from the UI, rather than an orphaned secret nothing describes.
    """
    base = f"secret/metadata/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}"
    try:
        users = vault_client.vault_list(f"{base}/users", use_cache=False) or []
    except Exception:  # noqa: BLE001 - no mappings listed is no mappings to remove
        users = []
    return [f"{base}/users/{u.rstrip('/')}" for u in users] + [f"{base}/secret", f"{base}/metadata"]


def delete_api_key_from_vault(vault_client, key_id: str) -> bool:
    """Delete an API key from Vault for good. True only when nothing of it can still be read.

    important: metadata deletes, not data deletes. A KV v2 data delete only marks the latest
    version deleted, so the key would stay in the LIST, its secret could be undeleted from version
    history, and every reconcile pass would read it back as a 404. A policy without delete on
    secret/metadata falls back to the data delete, which at least stops the key working.
    """
    failed = []
    for path in api_key_vault_records(vault_client, key_id):
        try:
            vault_client.vault_delete_metadata(path)
            continue
        except Exception as e:  # noqa: BLE001 - tried again below as a data delete
            logger.warning(f"Permanent delete of {path} failed, falling back to a data delete: {e}")
        try:
            vault_client.vault_delete(path.replace("secret/metadata/", "secret/data/", 1))
        except Exception as e:  # noqa: BLE001 - one record must not keep the others alive
            failed.append(path)
            logger.warning(f"Failed to delete {path} from Vault: {e}")
    if failed:
        logger.warning(f"API key {key_id} left {len(failed)} Vault record(s) behind")
        return False
    logger.info(f"Deleted API key {key_id} from Vault")
    return True


def save_user_mapping_to_vault(vault_client, api_key_id: str, user_mapping: ExternalUserMapping):
    """Save user mapping to Vault."""
    try:
        path = f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/users/{user_mapping.external_user_id}"
        vault_client.vault_kv_put(path, user_mapping.model_dump())
        logger.info(f"Saved user mapping {user_mapping.external_user_id} for API key {api_key_id}")
    except Exception as e:
        logger.error(f"Failed to save user mapping to Vault: {e}")
        raise HTTPException(status_code=500, detail="Could not save user mapping")


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


# Process-local API key cache and last-used throttle.
#
# important: this stays in process memory and never reaches Redis. CacheManager writes
# non-sensitive namespaces to Redis, and API key material must not leave the process. Same
# module-level lock plus dict pattern as app/services/lxd/health.py.
_key_cache_lock = threading.Lock()
# digest -> (cached_at_monotonic, key_id, metadata)
_key_cache: Dict[str, tuple] = {}
# key_id -> monotonic timestamp of the last last_used_at write
_last_used_written: Dict[str, float] = {}


def _api_key_digest(key_id: str, key_secret: str) -> str:
    """Cache key covering BOTH halves, so a wrong secret can never hit a cached entry."""
    return hashlib.sha256(f"{key_id}:{key_secret}".encode()).hexdigest()


# important: the metadata cache is per-process, so a revocation served by one uvicorn worker
# would leave the key valid on the others until their own TTL expired. Revocations are also
# published as a short-lived shared tombstone, which every worker checks before trusting a
# local hit. Without Redis this falls back to the per-process TTL.
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

    The tombstone outlives the cache TTL by the validation timeout. A validation already in flight
    can still store stale metadata, or a replaced secret's digest, after the local entries were
    dropped; the tombstone sends the next hit on that entry back to Vault.
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


def _schedule_last_used_update(key_id: str) -> None:
    """Record a key's use at most once per interval, without awaiting it.

    Fire and forget: a request must never pay for, or fail on, a bookkeeping field.

    important: writes to api_key_usage, never the key document. Rewriting the document from a
    cached read and without the key's lock could restore a grant a cleanup had just removed, an
    old `is_revoked`, or old capabilities.
    """
    now = time.monotonic()
    with _key_cache_lock:
        if now - _last_used_written.get(key_id, 0.0) < LAST_USED_WRITE_INTERVAL:
            return
        _last_used_written[key_id] = now
    api_key_usage.schedule_use(key_id)


def _fetch_cluster_info(cert_manager, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the sync cluster-info fan-out. Reads Vault, so it must not run on the
    event loop. Returns a single-item list so it composes with the shared gather helper.
    """
    cluster_info = cert_manager.get_cluster_info(cluster_id)
    metadata = cluster_info.get("metadata", {})
    location_data = stored_location_fields(cluster_id, metadata)
    public_location = _public_location_data(location_data)
    zone_name = location_data.get("region_name") or location_data.get("city") or None
    return [{
        "id": cluster_id,
        # important: Vault stores `display_name` and `type`; nothing writes `name` or
        # `server_type`, so reading those would answer every cluster as its raw id and as LXD.
        "name": lxd_status.resolve_display_name(cluster_id, metadata.get("display_name")),
        "endpoint": metadata.get("endpoint"),
        "server_type": server_type_name(metadata.get("type")),
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

    Raises for an unreachable cluster rather than returning empty, so _gather_per_cluster records
    the reason and a dead cluster never reads as an empty one.
    """
    # connect_or_get_client: this runs on proxy_executor, so its Vault fallback is already off the
    # loop, and it is breaker-gated, so a dead cluster costs nothing after the first failure.
    # get_client alone would answer 503 for a cluster registered after this worker started.
    client = require_client(connect_or_get_client(lxd_service, cluster_id), cluster_id)
    return [
        {"name": p.name, "cluster_id": cluster_id, "description": p.description}
        for p in client.projects.all()
    ]


def _fetch_cluster_failover(lxd_service, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the failover fan-out. Only ever runs on proxy_executor.

    important: a fresh GET /1.0, not the pooled client's `host_info`. That is read once when the
    client connects and the pool keeps clients for hours, so turning healing off would still read
    as on. The raw body never leaves this function:
    project_failover_config keeps numbers and one boolean, nothing that addresses the server.
    """
    client = require_client(connect_or_get_client(lxd_service, cluster_id), cluster_id)
    payload = client.api.get().json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    return [{"cluster_id": cluster_id, **project_failover_config(metadata)}]


def _fetch_cluster_instances(lxd_service, cluster_id: str) -> List[Dict[str, Any]]:
    """Blocking half of the instances fan-out. Only ever runs on proxy_executor.

    important: reuses the instance manager's per-node fetch, which walks every project with
    ``recursion=1`` and resolves addresses for running instances. ``client.instances.all()`` on
    the pooled client is bound by pylxd to the default project and would silently leave out
    every other project.

    The fetch reconnects through Vault for the cluster's certificate and records a breaker
    failure when the cluster does not answer; the dashboard shares that breaker through
    list_all_vms.
    """
    vms, connected = lxd_service.instance_manager.fetch_node_instances(cluster_id)
    if not connected:
        # The manager reports reachability separately from the result; ignoring it would make an
        # unreachable cluster look like one holding no instances.
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

    # connect_or_get_client: this runs on proxy_executor, so its Vault fallback is already off the
    # loop, and it is breaker-gated, so a dead cluster costs nothing after the first failure.
    # get_client alone would answer 503 for a cluster registered after this worker started.
    client = require_client(connect_or_get_client(lxd_service, cluster_id), cluster_id)
    # important: search only what the key may see. find_instance_in_projects with project=None
    # lists every project on the cluster and then does one GET per project, so a miss for a
    # caller-chosen name would cost 1 + N round trips on the shared proxy pool.
    search = [project] if project else sorted((granted or {}).get(cluster_id) or []) or [None]
    if search == [None]:
        search = _projects_holding(client, name)
    # important: collect every match. A name is unique per project only; stopping at the first
    # hit would silently return one of two same-named instances, the documented 409 for an
    # ambiguous name would never fire, and a caller could act on the wrong instance.
    details = []
    for candidate in search:
        instance, found_in = find_instance_in_projects(client, name, candidate)
        if instance is None:
            continue
        details.append(instance_detail(
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
        ))
    return details


def _projects_holding(client: Any, name: str) -> List[Optional[str]]:
    """Every project on the cluster holding an instance called `name`, for a key granted all.

    One all-projects listing instead of a GET per project. [None] when the listing fails, which
    falls back to the per-project search rather than answering "not found" for a cluster that did
    not say.
    """
    from app.services.lxd.project_scope import list_across_projects

    try:
        rows, _complete = list_across_projects(client, "instances", recursion=1)
    except Exception as exc:  # noqa: BLE001 - the per-project search below still answers
        logger.warning("All-projects instance listing failed, searching by project: %s", exc)
        return [None]
    return sorted({row.get("project") or "default" for row in rows if row.get("name") == name})


class ClusterFanout(NamedTuple):
    """What a fan-out returned, and the clusters it could not reach.

    important: every failure path contributes an empty list, so without `unreachable` a read over
    a tripped cluster would come back short with an authoritative-looking `total`, and a caller
    polling for its own instances could not tell that from a delete.

    The dashboard routers report the same idea under another field name, as a `List[str]` of node
    names for a banner. This carries a reason per cluster, so an integration can choose between
    retrying and giving up.
    """

    items: List[Dict[str, Any]]
    unreachable: List[Dict[str, str]]

    @property
    def partial(self) -> bool:
        return bool(self.unreachable)


async def _gather_per_cluster(
    fn, ctx, cluster_ids, label: str, *extra, skip_tripped: bool = True
) -> ClusterFanout:
    """Run a blocking per-cluster fetch across clusters concurrently and off the event loop.

    The cost is the slowest cluster, paid on a worker thread; a serial fan-out on the loop would
    block the whole process for the sum of every cluster's timeout. A cluster the breaker has
    tripped is skipped. `skip_tripped=False` is for a fetch that never contacts the cluster.

    important: every `unreachable` reason is sanitized. Raw exception text carries the Vault
    address, KV paths and certificate paths, and this body goes to an external key holder.

    A grant can name an id no cluster is registered under any more. It is reported as not
    registered without a fetch, which would only log a Vault 404 per call.
    """
    misses: List[Dict[str, str]] = []
    cluster_ids = list(cluster_ids)
    unregistered = await _unregistered_cluster_ids(cluster_ids)

    def missed(cluster_id: str, reason: Any) -> List[Dict[str, Any]]:
        misses.append({"cluster_id": cluster_id, "reason": sanitize_error(reason, limit=200)})
        return []

    async def one(cluster_id: str) -> List[Dict[str, Any]]:
        if cluster_id in unregistered:
            return missed(cluster_id, "not registered")
        if skip_tripped and health.is_tripped(cluster_id):
            logger.debug(f"sync: skipping {label} for tripped cluster {cluster_id}")
            return missed(cluster_id, health.get_reason(cluster_id) or "recently unreachable")
        # A lapsed trip with no client in this worker: the healer redials it (fanout.known_down).
        down = known_down(cluster_id, get_lxd_service().connection_manager) if skip_tripped else None
        if down is not None:
            logger.debug(f"sync: skipping {label} for known-down cluster {cluster_id}")
            return missed(cluster_id, down_text(down))
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
            # important: never trip the shared breaker here. A bulk list timing out means the
            # cluster is slow or holds a lot of data, not that it is unreachable; tripping would
            # make every raw_proxy call for that cluster answer 503 for a full TTL.
            logger.warning(
                f"sync: {label} for {cluster_id} timed out after {SYNC_CLUSTER_TIMEOUT}s; "
                "returning partial results for this cluster"
            )
            return missed(cluster_id, f"timed out after {SYNC_CLUSTER_TIMEOUT:g}s")
        except BlockingPoolFull:
            logger.warning(f"sync: {label} for {cluster_id} shed, proxy pool saturated")
            return missed(cluster_id, "proxy pool saturated")
        except Exception as e:
            logger.warning(f"Could not get {label} for {cluster_id}: {e}")
            # str(HTTPException) renders as "503: <detail>". The row being in `unreachable`
            # already implies the status, and the prefix would make two clusters with the same
            # cause read as two different failures.
            return missed(cluster_id, getattr(e, "detail", None) or str(e))

    batches = await asyncio.gather(*[one(c) for c in cluster_ids])
    return ClusterFanout([item for batch in batches for item in batch], misses)


async def _assert_granted_projects_exist(
    lxd_service: LXDService, permissions: List[ClusterPermissionRequest]
) -> None:
    """Refuse a grant that names a project no selected cluster has, when that can be proven.

    important: a name is refused only when every selected cluster answered and none reports it.
    If one stayed silent the check is skipped: the name might live only there, and narrowing a
    key during an outage must still work. Names are checked against the union of all selected
    clusters; the console warns before submit about a project that lives on only some of them.

    The error names only what the caller sent. Echoing the cluster's project list would let a
    partner enumerate another tenant's project names through a rejected write.
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
        # A name this request wants might live only on the cluster that did not answer, and the
        # console offers manual entry for that case, so an outage must not refuse the grant.
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

    That is 6 to 7 blocking Vault calls, each (2,3) with Retry(total=2); on the event loop a
    degraded Vault could park a worker for a minute or more.
    """
    vault_client = get_vault_integrations_client()
    metadata, secret_hash = get_api_key_from_vault(vault_client, key_id)

    if not verify_api_key_secret(key_secret, secret_hash):
        raise HTTPException(status_code=401, detail="Invalid API key secret")

    # important: the recoverable copy of the secret exists only because the outbound OrcaHub
    # callbacks in services/orcahub_sync.py re-present it to the partner, and its only reader
    # returns None unless the key carries `app_url`. Keys without a callback base stay hash-only,
    # so no plaintext credential is stored that nothing reads.
    if metadata.get("app_url"):
        try:
            secret_data = vault_client.vault_read(
                f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
            )
            # Only for the hash just verified. A rotation between the two reads has already
            # stored its own raw copy, and writing this one back would revive the replaced secret.
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


def _store_app_url_in_vault(
    api_key_id: str, app_url: Optional[str], *, secret_hash: Optional[str] = None, use_cache: bool = True
) -> bool:
    """Store external app callback URL in Vault alongside API key metadata. None clears it.

    The URL is stored with a tag of `secret_hash`, the hash of the secret that set it, and a callback
    uses it only while that is still the key's secret (services/integration_callback_url.py).
    Without one the stored URL is never used.

    Returns whether the write landed. The sync ignores it (bookkeeping must not fail a sync); a
    secret rotation does not, because it clears the URL before issuing a secret.
    """
    try:
        vault_client = get_vault_integrations_client()
        metadata, _ = get_api_key_from_vault(vault_client, api_key_id, use_cache=use_cache)
        metadata["app_url"] = app_url
        metadata[APP_URL_SECRET_TAG] = callback_url_tag(secret_hash) if app_url and secret_hash else None
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/metadata",
            metadata
        )
        logger.info(f"Stored app_url for API key {api_key_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to store app_url in Vault: {e}")
        return False


def _bind_callback_url_blocking(key_id: str, app_url: str, presented_secret: Optional[str]) -> bool:
    """Store the URL a sync sent, bound to the secret it authenticated with, if that is still current.

    important: under the key's lock, and read from Vault rather than its mirror. A sync authenticated
    with a secret that a rotation has replaced since must not set the URL the new secret is sent to,
    and a key revoked, deactivated or expired since gets none. Returns whether the URL was stored.
    """
    metadata, secret_hash = get_api_key_from_vault(get_vault_integrations_client(), key_id, use_cache=False)
    if not presented_secret or not secret_hash or not verify_api_key_secret(presented_secret, secret_hash):
        logger.warning("Callback URL from api key %s not stored: its secret changed while the sync ran", key_id)
        return False
    if classify_key(metadata) != "active":
        logger.warning("Callback URL from api key %s not stored: the key is no longer active", key_id)
        return False
    return _store_app_url_in_vault(key_id, app_url, secret_hash=secret_hash, use_cache=False)


def _forget_callback_url_blocking(key_id: str) -> Optional[bool]:
    """Clear a key's stored callback URL. None when it had none, else whether the write landed.

    important: a secret rotation runs this. `app_url` is set by any /sync caller holding the key's
    secret, and the OrcaHub callbacks send the raw secret to it, so a URL planted with a leaked
    secret would receive the secret the rotation issues to replace it. The connection sets the URL
    again on its next sync, with the new secret.
    """
    metadata, _ = get_api_key_from_vault(get_vault_integrations_client(), key_id, use_cache=False)
    if not metadata.get("app_url"):
        return None
    return _store_app_url_in_vault(key_id, None, use_cache=False)


async def _upsert_synced_organizations(
    db: AsyncSession,
    organizations: List["SyncOrganization"],
    api_key_id: str,
    *,
    add_on: bool = False,
) -> int:
    """
    Persist organizations pushed from OrcaHub. Idempotent: every sync calls it again.

    Identity is (source='orcahub', external_id). OrcaHub slugs are globally unique, so an orcahub
    row sharing a slug is the same org whose external_id changed through delete and recreate.
    Pass 1 updates known identities in place, freeing any renamed slug before Pass 2 handles the
    rest: free slug -> insert; slug held by an orcahub row whose external_id is retired (absent from
    this push) -> re-link it; slug held by a live row -> skip and warn, never taking another org's
    identity or re-parenting its members.
    Each org commits alone, so one conflict skips that row only. Returns the synced count.
    """
    synced = 0
    handled: set[str] = set()
    # Identities present in this push. A slug-owning orcahub row whose external_id is still in
    # this set is a live org whose slug must not be taken; only retired identities may be
    # re-linked.
    incoming_ids = {o.id for o in organizations}

    # important: an add-on credential may first-claim an organization only while it is its
    # installation's current key (is_current_add_on_credential). Each organization is decided under
    # the key's lock, taken before its row as everywhere else, so a rotation's carry either sees this
    # claim or this claim sees the rotation. Answered lazily, once per claim, and a refusal sticks.
    replaced = False

    async def _replaced() -> bool:
        nonlocal replaced
        if add_on and not replaced:
            replaced = not await is_current_add_on_credential(db, api_key_id)
        return replaced

    async def _refuse_replaced(org_data: "SyncOrganization") -> None:
        logger.warning(
            "Refusing a first claim of organization %s (external_id=%s): api key %s is no longer its "
            "add-on installation's current credential", org_data.slug, org_data.id, api_key_id,
        )
        await db.commit()

    # Pass 1: reconcile known identities. Updating here frees any slug the org renamed away
    # from, so Pass 2's inserts/re-links see the post-rename slug map.
    for org_data in organizations:
        async with (serialize_key_ownership(db, api_key_id) if add_on else nullcontext()):
            try:
                # FOR UPDATE (PostgreSQL; SQLite omits it), so two keys claiming one unclaimed
                # organization at once take turns: the second reads the first's claim and is refused,
                # rather than both claiming it and the loser staying in its owner set unseen. A transfer
                # reads the row the same way, so it waits for this sync or this sync waits for it.
                existing = (await db.execute(
                    select(Organization).where(
                        and_(
                            Organization.source == "orcahub",
                            Organization.external_id == org_data.id,
                        )
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()
                if not existing:
                    continue
                # important: `api_key_id` records which key owns an org. A key may only update what
                # it already owns, or an org not yet claimed; otherwise any valid key could rewrite
                # an org another integration owns, including its members' roles.
                # Any key in the owner set may sync it. Only an unclaimed org gets this key as its
                # primary; an owned org keeps its primary, so a co-owner's push never moves the callbacks.
                if existing.api_key_id and not await is_owner(db, existing, api_key_id):
                    logger.warning(
                        "Refusing sync of organization %s (external_id=%s): owned by api key %s, "
                        "caller is %s",
                        existing.slug, org_data.id, existing.api_key_id, api_key_id,
                    )
                    handled.add(org_data.id)
                    # important: nothing changed, so end the transaction now. Kept open, the row read
                    # FOR UPDATE above stays locked into the rest of the sync, which takes this key's
                    # lock; a transfer or owner-set change for this key holds that lock while it waits
                    # on this row, and PostgreSQL answers one of the two with a deadlock.
                    await db.commit()
                    continue
                if not existing.api_key_id and await _replaced():
                    handled.add(org_data.id)
                    await _refuse_replaced(org_data)
                    continue
                existing.name = org_data.name
                existing.slug = org_data.slug
                existing.description = org_data.description
                if not existing.api_key_id:
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
        async with (serialize_key_ownership(db, api_key_id) if add_on else nullcontext()):
            try:
                slug_owner = (await db.execute(
                    select(Organization).where(Organization.slug == org_data.slug)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()

                if slug_owner is None:
                    if await _replaced():
                        await _refuse_replaced(org_data)
                        continue
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
                    and (not slug_owner.api_key_id or await is_owner(db, slug_owner, api_key_id))
                ):
                    # The row's OrcaHub identity is retired (its external_id is absent from this
                    # push) and OrcaHub slugs are globally unique, so this row is the deleted
                    # predecessor of the incoming org that reused the slug. Re-link it in place.
                    #
                    # important: the ownership term matches Pass 1. This branch reassigns
                    # `api_key_id`, so without it any key could take over another integration's
                    # organisation by pushing an org that reused its slug with a fresh external_id,
                    # and then pass every later check, including the member sync into Authentik.
                    if not slug_owner.api_key_id and await _replaced():
                        await _refuse_replaced(org_data)
                        continue
                    slug_owner.name = org_data.name
                    slug_owner.description = org_data.description
                    slug_owner.external_id = org_data.id
                    if not slug_owner.api_key_id:
                        slug_owner.api_key_id = api_key_id
                    slug_owner.is_active = True
                else:
                    # important: the slug is held by a live row: a non-OrcaHub org (ex; a partner's
                    # local org) or another OrcaHub org still in this push (a Pass 1 rename that
                    # could not land yet). Overwriting it would hijack that org and re-parent its
                    # members. Log the clash and skip; the next sync retries once it is resolved.
                    logger.warning(
                        f"Skipped organization {org_data.id}: slug '{org_data.slug}' is held by a live "
                        f"{slug_owner.source} org (id={slug_owner.id}, external_id={slug_owner.external_id}); "
                        f"resolve the slug clash to sync it"
                    )
                    # As in Pass 1, the row read FOR UPDATE is released before going on.
                    await db.commit()
                    continue

                await db.commit()
                synced += 1
            except Exception as e:
                await db.rollback()
                logger.warning(f"Skipped organization {org_data.id} (slug={org_data.slug}) during sync insert: {e}")

    if synced > 0:
        logger.info(f"Synced {synced} organizations from OrcaHub (api_key={api_key_id})")

    return synced


async def _upsert_synced_members(
    db: AsyncSession,
    members: List["SyncMember"],
    api_key_id: str,
    key_creator: Optional[str] = None,
    pushed_organizations: Tuple[str, ...] = (),
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
            # Read afresh. A transfer or an owner leaving can land while a long member list is
            # written, and the session keeps the primary it read first (is_owner trusts it).
            org = (await db.execute(
                select(Organization).where(
                    and_(
                        Organization.source == "orcahub",
                        Organization.external_id == m.organization_id,
                    )
                ).execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if not org:
                logger.warning(f"Org not found for external_id={m.organization_id}, skipping member {m.email}")
                continue
            # important: the org is resolved purely from the caller-supplied external id, so a
            # key that owns nothing could name another integration's org and rewrite its
            # members, which pushes a global Authentik role change for those emails. Same
            # ownership rule as the organisation upsert above, and strict for an unclaimed org
            # too: a normal sync claims the org earlier in the same request.
            if not await is_owner(db, org, api_key_id):
                logger.warning(
                    "Refusing member sync into organization %s: owned by api key %s, caller is %s",
                    org.slug, org.api_key_id or "<none>", api_key_id,
                )
                continue

            if m.action == "remove":
                # Idempotent: an already-absent member is a no-op success.
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
                    # Membership removal cuts organization access. The baseline Authentik
                    # role belongs to the account, even after its last organization is gone.
                continue

            # Identity is (organization_id, user_email), so update-if-present / insert-else
            # is unambiguous. Per-member commit isolates a failing row from the rest.
            orcastra_role = stored_member_role(m.role)

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

            # important: an integration may grant a role, never demote one or touch role_admin.
            # sync_user_role removes every other role group, so through it any key could demote
            # an Admin.
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
    # important: only the key's creator, and only into the one org this push lists and this key
    # claims. Otherwise a key could push another partner's email into its own org and take that
    # partner's unbound clusters.
    for email in upserted_emails:
        try:
            await rebind_creator_clusters(
                db, email, members, api_key_id=api_key_id, creator=key_creator, roles=sync_svc,
                pushed_organizations=pushed_organizations,
            )
        except Exception as e:
            logger.warning(f"reconcile_personal_clusters failed for {email}: {e}")

    return synced, removed


# Circuit breaker: no legitimate single sync removes anywhere near this many members. If a push
# would remove more than this (ex; an OrcaHub-side query bug), abort the whole reconcile
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
    Offboarding reconcile: for each org in this push, delete the OrganizationMember rows whose email
    is not in its pushed roster. OrcaHub sends full rosters, never explicit removals, so a departed
    member would otherwise keep org-scope access.

    important: destructive (cuts org-scope access, cascades to TenantPolicyAssignment). An empty
    members[] skips the run; an org this key does not own, or no key owns, is never pruned; an org
    present with an empty roster is skipped, never purged (OrcaHub drops an emptied org instead),
    which is the main guard against a mass delete; more than _RECONCILE_MAX_REMOVALS_PER_SYNC
    removals aborts with nothing deleted. The baseline role stays. Email match is exact, like
    UNIQUE(org, user_email). Returns members removed, 0 in a dry run.
    """
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
            if not may_reconcile(org.slug, frozenset(await owner_key_ids(db, org.id)), api_key_id):
                continue

            desired_emails = {
                m.email for m in members
                if str(m.organization_id) == str(org_data.id) and m.action != "remove"
            }
            if not desired_emails:
                # important: never purge every member of an org from an empty roster. A
                # legitimately emptied org drops out of the push instead.
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
    # important: trailing, and defaulted, so the header positions are unchanged. FastAPI injects a
    # Response by annotation wherever it sits, and the tests that call this dependency directly
    # pass the two headers positionally.
    response: Response = None,  # type: ignore[assignment]
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

    # State checks run on every request, cache hit or not, straight from in-memory metadata.
    # They cost nothing and keep expiry exact even while the entry is cached.

    if metadata.get("is_revoked", False):
        raise HTTPException(status_code=401, detail="API key has been revoked")

    if not metadata.get("is_active", True):
        raise HTTPException(status_code=401, detail="API key is not active")

    # important: is_expired reads every stored notation (naive, offset-bearing) and counts an
    # unreadable value as expired, so a malformed expiry is a 401, never a 500 on every request.
    if is_expired(metadata.get("expires_at")):
        raise HTTPException(status_code=401, detail="API key has expired")

    # important: after the secret verifies, never before. The key id is public, so counting it in
    # the middleware would let anyone holding a victim's id spend that integration's budget with a
    # wrong secret. See consume_api_key_budget.
    allowed, budget = consume_api_key_budget_state(x_api_key_id)
    if budget is not None and response is not None:
        # Tells a caller its own limit, which is tuned per deployment. Omitted when the limiter is
        # off or its store could not answer, so a budget that cannot be computed is never reported.
        response.headers["X-RateLimit-Limit"] = str(budget["limit"])
        response.headers["X-RateLimit-Remaining"] = str(budget["remaining"])
        response.headers["X-RateLimit-Reset"] = str(budget["reset"])
    if not allowed:
        retry_after = str(budget["reset"]) if budget is not None else "30"
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this API key. Please try again later.",
            headers={
                "Retry-After": retry_after,
                **(
                    {
                        "X-RateLimit-Limit": str(budget["limit"]),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(budget["reset"]),
                    }
                    if budget is not None
                    else {}
                ),
            },
        )

    # important: an add-on's credential is an ordinary key pair, so every check above passes for
    # one whose installation is suspended: a suspend moves the installation row and leaves the
    # Vault key alone. Without this check a suspended add-on stops receiving webhooks but keeps
    # full REST and MCP access.
    #
    # Only install-issued credentials pay for this. The snapshot is cached per worker on the key's
    # own TTL with a Redis tombstone, so the usual request costs no query, and a suspend reaches
    # every worker at once rather than on a timer. The session acquires a connection only on a
    # cache miss.
    if metadata.get("extension_install_id") is not None:
        from app.core.database import async_session_maker
        from app.services.extension_scope import assert_install_is_live

        try:
            async with async_session_maker() as session:
                await assert_install_is_live(session, x_api_key_id)
        except HTTPException:
            # A decision about the credential. ScopeRefused is one of these.
            raise
        except Exception as exc:  # noqa: BLE001
            # important: a database outage on the authentication path answers 503, like the Vault
            # timeout above and the MCP door, not a bare 500 on every REST endpoint an add-on
            # credential touches.
            logger.warning(
                "Installation check unavailable for key %s: %s", x_api_key_id, exc
            )
            raise HTTPException(
                status_code=503, detail="Authentication backend unavailable"
            ) from None

    metadata = await _bound_to_registrations(metadata)
    # important: after the cache, into a copy, like the registration binding above. A key a Partner
    # created is held to its creator's projects on a cluster another party holds, so a stored `*`
    # grant does not reach the other party (services/key_party_bound.py).
    metadata = await narrow_to_party_bound(metadata)
    _schedule_last_used_update(x_api_key_id)

    return metadata


async def _bound_to_registrations(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """The key as it may act now: grants for a registration that has ended are left out.

    important: after the validation cache, on every request, into a copy (cluster_generations.bind).
    A registration that ends must stop its grants at once on every worker, and the cached document
    must still hold them for when the cluster is granted again.
    """
    if not get_settings().key_grant_generations:
        return metadata
    try:
        generations = await cluster_generations.current()
    except cluster_generations.GenerationsUnavailable:
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    return cluster_generations.bind(metadata, generations)


OWNER_KEYS_FEATURE = "organization_owner_keys.v1"
# GET /integrations/clusters/failover. Advertised so a client can tell "this deployment predates
# the read" from "this cluster did not answer" without spending a call on a 404.
FAILOVER_CONFIG_FEATURE = "cluster_failover_config.v1"


def _integration_features() -> Optional[List[str]]:
    """The optional contracts this deployment speaks (APIKeyValidateResponse.features)."""
    features = [OWNER_KEYS_FEATURE] if multi_owner_enabled() else []
    features.append(FAILOVER_CONFIG_FEATURE)
    return features


def require_capability(capability: str):
    """Authenticate the key, then refuse it unless it carries ``capability``.

    Every handler on `external_router` must depend on one of these rather than on
    `validate_api_key` directly, so a key issued for one job cannot do every other job on this
    router. `tests/test_every_external_endpoint_declares_a_capability.py` keeps a new endpoint
    from skipping it.

    FastAPI caches a dependency per request, so the nested `validate_api_key` still costs one
    Vault round trip even where a handler also takes the key for its own use.
    """
    async def _dep(api_key: Dict[str, Any] = Depends(validate_api_key)) -> Dict[str, Any]:
        assert_capability(api_key, capability)
        return api_key

    # Name it after the capability so a traceback, a dependency-graph dump and the OpenAPI
    # schema all say which one was required, rather than reporting every gate as `_dep`.
    _dep.__name__ = f"require_{capability.replace('.', '_')}"
    _dep.__qualname__ = _dep.__name__
    return _dep


def require_explicit_capability(capability: str):
    """Like ``require_capability``, but a key issued before capabilities existed does not satisfy it.

    important: ``assert_capability`` reads a missing ``capabilities`` field as unrestricted so
    older integrations keep working. That suits a power that existed when the key was issued; for
    a new guest-reaching power it would hand the power to every legacy key without anyone deciding
    it. A capability in ``GUEST_ACCESS_CAPABILITIES`` gets the explicit check from this dependency
    alone, without a second ``assert_explicit_capability`` call inside the handler.
    """
    async def _dep(api_key: Dict[str, Any] = Depends(validate_api_key)) -> Dict[str, Any]:
        assert_explicit_capability(api_key, capability)
        return api_key

    _dep.__name__ = f"require_explicit_{capability.replace('.', '_')}"
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
    # Uses the request-scoped set org_scope_dependency already resolved (no extra query),
    # the same source the cluster picker uses, so it never rejects an option the UI offered.
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        requested = {p.cluster_id for p in request.cluster_permissions}
        invalid = requested - allowed
        if invalid:
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted to grant access to clusters: {sorted(invalid)}",
            )

    refuse_grants_on_purchased_clusters(p.cluster_id for p in request.cluster_permissions)
    await _refuse_new_grants_to_unregistered_clusters(p.cluster_id for p in request.cluster_permissions)
    # important: a project the caller cannot see, or '*', only where no other party holds the
    # cluster. After the scope guard and before any cluster is read.
    await _refuse_a_grant_past_the_project_bound(db, _rbac.email, request.cluster_permissions)

    # After the scope guard: this reaches the clusters, so it must never run for a cluster the
    # caller was just told they may not touch.
    await _assert_granted_projects_exist(lxd_service, request.cluster_permissions)

    try:
        key_id, key_secret, secret_hash = generate_api_key_pair()

        # Aware, so the stored string carries an offset. Naive values already in Vault keep
        # working, because parse_expiry reads a naive value as UTC.
        created_at = now_utc()
        expires_at = None
        # `is not None` rather than truthiness: `ge=1` makes 0 unreachable, but the two spellings
        # would disagree if that constraint changed, and this decides whether a key ever expires.
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

        # created_by records the creator for org-level ownership.
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
            # Stamped by the server with the registration each cluster has now; anything a caller
            # sent as a stamp, or as `registered`, is dropped (services/cluster_generations.stamp).
            "cluster_permissions": await cluster_generations.stamped(
                [p.model_dump(exclude={"registered"}) for p in request.cluster_permissions]
            ),
        }
        
        # The raw secret is kept for callback authentication.
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

        _audit_credential_lifecycle(
            action="integration.api_key.create", key_id=key_id, metadata=metadata
        )

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


def _assert_key_in_scope(
    metadata: Dict[str, Any],
    allowed: Optional[set[str]],
    generations: Optional["cluster_generations.Generations"] = None,
    caller_email: Optional[str] = None,
) -> None:
    """The overlap rule list_api_keys uses. 404 so an out-of-scope id reads as a missing one."""
    if not metadata:
        raise HTTPException(status_code=404, detail="API key not found")
    if not _key_in_scope(metadata, allowed, generations, caller_email):
        raise HTTPException(status_code=404, detail="API key not found")


def _key_in_scope(
    metadata: Dict[str, Any],
    allowed: Optional[set[str]],
    generations: Optional["cluster_generations.Generations"],
    caller_email: Optional[str],
) -> bool:
    """Whether a key reaches one of the caller's clusters, or is the caller's own with none left.

    The second half is for a key every grant of which is paused (its registrations ended). It
    reaches no cluster, so no cluster scope admits it; without this not even its creator could
    revoke it while its non-cluster capabilities (ex; org.sync) still work.
    """
    if allowed is None:
        return True
    reached = _key_clusters(metadata, generations)
    if reached & allowed:
        return True
    creator = str(metadata.get("created_by") or "").lower()
    return not reached and bool(caller_email) and creator == str(caller_email).lower()


def _read_api_key_metadata_blocking(key_id: str) -> Dict[str, Any]:
    """A key's metadata document, or {} when it cannot be read. The cached /metadata path only."""
    try:
        return get_vault_integrations_client().vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
        ) or {}
    except Exception:
        return {}


async def _run_org_keys(db: AsyncSession, email: str) -> set[str]:
    """The integration keys on this user's side (cluster_parties.side_keys).

    Not every key an organization they run carries: a buyer's founder holds a partner row in a
    buyer organization that carries the seller's key, and must not get it. A key counts through an
    organization the user created, or through the key's home organization when the user is a
    partner there.
    """
    return await cluster_parties.run_org_keys(db, email)


def _key_clusters(
    metadata: Dict[str, Any], generations: Optional["cluster_generations.Generations"] = None
) -> set[str]:
    """The clusters a key reaches, for deciding who manages it.

    important: only grants for a registration still active. A grant left from an ended
    registration names an id another organization may hold now, and counting it would make that
    organization's partners managers of this key: able to list, revoke, delete and edit it.
    """
    return {str(c or "") for c in cluster_generations.reachable(metadata.get("cluster_permissions"), generations)}


def _with_registration_state(
    grants: List[Any], generations: Optional["cluster_generations.Generations"]
) -> List[Dict[str, Any]]:
    """Grants for a reader: no stamp, and a paused one marked `registered: false`."""
    dicts = [g for g in grants if isinstance(g, dict)]
    if generations is None:
        return [{k: v for k, v in g.items() if k != cluster_generations.STAMP} for g in dicts]
    return cluster_generations.annotate(dicts, generations)


async def _generations_for_scope() -> Optional["cluster_generations.Generations"]:
    """Registration state for the management checks, or None (every grant counts) if unreadable."""
    return await cluster_generations.current_or_none()


async def _keys_the_caller_manages(
    db: AsyncSession, user: AuthenticatedUser, keys: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The keys, out of ones already in the caller's cluster scope, that this user may manage.

    important: cluster overlap alone would let a Partner on a cluster bound to another organization
    read, revoke and delete that organization's keys, breaking its OrcaHub sync and provisioning. A
    key granting a cluster another party holds is managed only by its creator, or by a caller the
    key is on the side of (_run_org_keys). An Admin, and a key whose clusters are all the caller's
    own, keep the overlap rule.

    "Another party" is services/cluster_parties.py, decided by party. The partner project bound in
    core/org_scope.py counts bindings instead; that module's docstring says why the two differ.
    """
    from app.core.rbac import Role, get_highest_role, resolve_roles

    if not keys or get_highest_role(resolve_roles(user)) == Role.ADMIN:
        return keys
    email = (user.email or "").lower()
    mine = [k for k in keys if str(k.get("created_by") or "").lower() == email]
    rest = [k for k in keys if str(k.get("created_by") or "").lower() != email]
    if not rest:
        return keys
    generations = await _generations_for_scope()
    held = await _clusters_held_by_another_party(
        db, user.email, set().union(*(_key_clusters(k, generations) for k in rest))
    )
    linked = await _run_org_keys(db, user.email) if held else set()
    kept = [k for k in rest if not (_key_clusters(k, generations) & held) or str(k.get("id") or "") in linked]
    return [k for k in keys if k in mine or k in kept]


async def _authorize_api_key(key_id: str, user: AuthenticatedUser, db: AsyncSession) -> Dict[str, Any]:
    """Return an API key's metadata only if the caller may manage it, else 404.

    important: `list_api_keys` filters by cluster overlap and by who manages the key, and every
    single-key handler takes `key_id` straight from the path, so both apply here too and the two
    surfaces agree. The read runs in a thread (asyncio.to_thread keeps the request's org scope)
    because Vault answers synchronously.
    """
    metadata = await asyncio.to_thread(_read_api_key_metadata_blocking, key_id)
    _assert_key_in_scope(
        metadata, get_current_allowed_clusters(), await _generations_for_scope(), caller_email=user.email
    )
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
    """A Partner may not hold a key reaching more of a cluster than they can see there.

    Asked when a key is minted, when its grant is edited, and when a Partner who did not mint it
    rotates its secret (services.api_key_grant_bound.refuse_a_grant_past_the_project_bound,
    which an add-on installation asks too).
    """
    await refuse_a_grant_past_the_project_bound(
        db, caller_email, grants, already_granted, held=_clusters_held_by_another_party
    )


def _owner_metadata_reader(vault_client) -> Callable[[str], Awaitable[Tuple[Optional[Dict[str, Any]], Optional[str]]]]:
    """How the ownership checks read another owner key: uncached, off the event loop."""
    async def _read(other_key_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        return await asyncio.to_thread(_read_key_metadata_blocking, vault_client, other_key_id)

    return _read


async def _refuse_ending_an_owner(
    db: AsyncSession,
    key_id: str,
    *,
    action: str,
    audit_action: str,
    user: AuthenticatedUser,
    metadata: Dict[str, Any],
    withdrawn: Tuple[str, ...] = (),
    vault_client: Any = None,
) -> List[OwnerHandoff]:
    """refuse_if_key_owns_organizations, with the refusal recorded on the action it refused. Returns the
    organizations another owner carries on, for apply_owner_handoffs."""
    try:
        return await refuse_if_key_owns_organizations(
            db, key_id, action=action, user=user, metadata=metadata, withdrawn=withdrawn,
            read_metadata=_owner_metadata_reader(vault_client) if vault_client is not None else None,
        )
    except KeyOwnsOrganizations as refusal:
        _audit_credential_lifecycle(
            action=audit_action, key_id=key_id, metadata=metadata, result="denied",
            error_code="key_owns_organizations", extra_details=refusal.audit_details,
        )
        raise


# important: a fixed sentence. The database's own text names the database, and this answer reaches the browser.
OWNER_SETS_NOT_SAVED = (
    "The key was changed, but the organizations it owns could not be updated. An Orcastra administrator "
    "can finish this from each organization's owner keys."
)


KEY_DELETE_INCOMPLETE = (
    "The API key was not fully removed from the credential store. It may still be listed; "
    "try the delete again once the credential store is reachable."
)


def _raise_if_incomplete(removed: bool) -> None:
    """An incomplete key delete is a 503, never a reported success."""
    if not removed:
        raise HTTPException(status_code=503, detail=KEY_DELETE_INCOMPLETE)


async def _write_key_then_commit(
    db: AsyncSession, key_id: str, handed_off: List[Any], write: Callable[[], None]
) -> None:
    """The Vault write that ends or narrows a key, and the owner-set changes it carries.

    important: the owner sets are flushed and the primary-in-set rule checked before the Vault write, so a
    change the database would refuse stops here with nothing written anywhere. Once the write lands the
    key's cached copies are dropped at once, whatever follows: a revoke must not keep working from the
    validation cache because a later step failed. A commit that still fails after the write (the
    connection lost) answers a fixed sentence; the owner sets are then finished from the Administrator's
    owner-key routes.
    """
    if handed_off:
        try:
            await check_owner_sets(db)
        except Exception as exc:  # noqa: BLE001 - nothing is written yet; the text names the database
            logger.warning("Could not save the owner sets api key %s leaves: %s", key_id, type(exc).__name__)
            await db.rollback()
            raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
    try:
        # Off the event loop. A delete is one Vault call per record under the key, each with its
        # own timeout, and a degraded Vault would otherwise stall every request on the worker.
        await asyncio.to_thread(write)
    finally:
        # Also when the write raised: it may have landed before its reply was lost.
        evict_api_key_cache(key_id)
    if not handed_off:
        return
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - Vault is written; the text names the database
        logger.error("Api key %s was written in Vault but its owner sets were not saved: %s", key_id, type(exc).__name__)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - the answer below is the same either way
            pass
        raise HTTPException(status_code=503, detail=OWNER_SETS_NOT_SAVED) from None


async def _with_ownership(db: AsyncSession, user: AuthenticatedUser, keys: List[APIKeyInfo]) -> List[APIKeyInfo]:
    """Fill in which organizations each key owns, as this caller may see them.

    After the list cache, never inside it. The cached list is shared by everyone with the same
    cluster scope, while the slugs a caller may see depend on the caller, and ownership moves without
    the key changing. A database that cannot answer leaves the counts null rather than failing the
    list, because the refusal on revoke and delete is what enforces the rule.
    """
    if not keys:
        return keys
    try:
        owned = await owned_by_keys(db, [info.id for info in keys])
        visible = await visible_organization_ids(db, user) if owned else None
        sets = await owners_of(db, {org_id for rows in owned.values() for org_id, _slug in rows})
    except Exception as exc:  # noqa: BLE001 - the list still renders, without ownership
        logger.warning("Could not read which organizations the listed keys own: %s", type(exc).__name__)
        return keys
    for info in keys:
        rows = owned.get(info.id, [])
        shown = [slug for org_id, slug in rows if visible is None or org_id in visible]
        info.owned_organization_count = len(rows)
        info.owned_organization_slugs = shown[:MAX_LISTED_SLUGS]
        info.owned_organization_hidden_count = len(rows) - len(shown)
        info.sole_owned_organization_count = sum(1 for org_id, _slug in rows if len(sets.get(org_id, [])) <= 1)
    return keys


async def _with_usage(keys: List[APIKeyInfo]) -> List[APIKeyInfo]:
    """The recorded last use (services/api_key_usage.py), when it is newer than the document's."""
    recorded = await api_key_usage.last_used(info.id for info in keys)
    for info in keys:
        info.last_used_at = api_key_usage.newer(info.last_used_at, recorded.get(info.id))
    return keys


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
        # important: the `:v2` suffix marks the cached shape, which carries the user mapping
        # counts beside the metadata; a v1 entry is a bare list. Change the suffix with the
        # shape, so a rolling deploy never reads the other shape.
        cache_key = get_scope_cache_key("integrations:api_keys:list:v2")
        cached_data = None if fresh else cache.get("api_responses", cache_key)

        if cached_data is None:
            all_metadata = await asyncio.to_thread(list_api_keys_from_vault, vault_client)
            
            # Filter by org scope: non-admin users only see keys
            # whose cluster_permissions overlap with their allowed clusters
            allowed = get_current_allowed_clusters()
            if allowed is not None:
                generations = await _generations_for_scope()
                # Show a key if ANY cluster it still reaches is in the user's set. A key that reaches
                # none (every grant paused) is kept here and narrowed to its creator after the
                # cache, which every caller with this scope shares.
                all_metadata = [
                    meta for meta in all_metadata
                    if not _key_clusters(meta, generations) or _key_clusters(meta, generations) & allowed
                ]

            # The user mapping counts are resolved here, inside the cache miss, and cached with
            # the metadata they belong to. Read below the cache, every request would pay a Vault
            # LIST plus one read per mapping for every visible key, even on a hit.
            user_counts = await asyncio.to_thread(
                lambda: {
                    meta["id"]: len(get_user_mappings_from_vault(vault_client, meta["id"]))
                    for meta in all_metadata
                    if isinstance(meta, dict) and meta.get("id")
                }
            )

            cache.set(
                "api_responses",
                cache_key,
                {"metadata": all_metadata, "user_counts": user_counts},
                ttl=60,
            )
        else:
            all_metadata = cached_data.get("metadata", [])
            user_counts = cached_data.get("user_counts", {})
        # After the cache, because the cached list is shared by everyone with the same cluster
        # scope while who manages a key depends on the caller.
        scope = get_current_allowed_clusters()
        generations = await _generations_for_scope()
        all_metadata = await _keys_the_caller_manages(
            db,
            user,
            [
                m for m in all_metadata
                if isinstance(m, dict) and _key_in_scope(m, scope, generations, user.email)
            ],
        )
        
        # An expired key is not active. The counting lives in `tally_keys` rather than in this
        # loop so it can be asserted on fixtures.
        records = [m for m in all_metadata if isinstance(m, dict)]
        counts = tally_keys(records)

        # `_key_info` builds the model with `APIKeyInfo(**metadata)`, so the count is passed
        # alongside rather than folded into the dict, where it would be an unexpected field.
        keys = [
            _key_info(metadata, user_counts.get(metadata.get("id"), 0))
            for metadata in records
        ]
        keys = await _with_usage(await _with_ownership(db, user, keys))

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
        
        (info,) = await _with_usage(await _with_ownership(db, user, [_key_info(metadata, len(user_mappings))]))
        return info
        
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
    db: AsyncSession = Depends(get_db),
    # Last, so callers passing the others by position are unaffected.
    rebind_cluster_ids: List[str] = Query(
        default=[],
        description=(
            "Clusters whose paused grant (made for a registration of the id that has since ended) "
            "should now reach the cluster registered under it. Without it a paused grant stays paused."
        ),
    ),
):
    """Update API key cluster permissions."""
    # important: before the cluster reads below. An add-on installation owns its grant.
    current = await _authorize_api_key(key_id, _rbac, db)
    refuse_grant_edit(current)

    # important: the same cluster-set check as create_api_key. Without it a partner could create
    # a key scoped to their cluster and then widen it to the whole fleet in a second call.
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        requested = {p.cluster_id for p in permissions}
        invalid = requested - allowed
        if invalid:
            raise HTTPException(
                status_code=403,
                detail=f"Not permitted to grant access to clusters: {sorted(invalid)}",
            )
    refuse_grants_on_purchased_clusters(p.cluster_id for p in permissions)
    # What the key already holds on a cluster is carried over at its level or lower, so an edit
    # cannot widen a key past the bound but never breaks a running one.
    await _refuse_a_grant_past_the_project_bound(db, _rbac.email, permissions, already_granted(current))

    # The cluster reads stay outside the key's lock: nothing in them depends on what the key holds.
    await _assert_granted_projects_exist(lxd_service, permissions)

    vault_client = get_vault_integrations_client()
    try:
        # important: the held ids, the registry refusal and the write all under the key's lock, like
        # revoke and the capability edit. Read before the lock, an id a deregistration cleanup removed
        # in between would still count as held, and the write would put it back. Committed right
        # after the write, so the lock is not held through the mapping read below.
        async with serialize_key_ownership(db, key_id):
            metadata, secret_hash = await asyncio.to_thread(
                get_api_key_from_vault, vault_client, key_id, use_cache=False
            )
            await _refuse_new_grants_to_unregistered_clusters(
                (p.cluster_id for p in permissions),
                frozenset(
                    grant.get("cluster_id") for grant in metadata.get("cluster_permissions", [])
                    if isinstance(grant, dict)
                ),
            )
            metadata["cluster_permissions"] = await cluster_generations.stamped(
                [p.model_dump(exclude={"registered"}) for p in permissions],
                held=metadata.get("cluster_permissions") or [],
                rebind=rebind_cluster_ids,
            )
            await asyncio.to_thread(
                vault_client.vault_kv_put,
                f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
                metadata,
            )
            await release_key_locks(db)
        
        # Clear all scoped cache variants
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        # important: also drop the process-local validation cache, otherwise the previous
        # cluster_permissions keep authorising proxy calls until its TTL expires.
        evict_api_key_cache(key_id)

        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        return (await _with_usage([_key_info(metadata, len(user_mappings))]))[0]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key permissions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class APIKeyCapabilitiesRequest(BaseModel):
    """Request to replace an API key's capability set."""
    capabilities: List[str] = Field(..., min_length=1)
    force: bool = Field(
        False,
        description=(
            "Withdraw tenant.provision from a key that owns OrcaHub organizations anyway. Without it "
            "such an edit answers 409 with what would stop."
        ),
    )

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
    db: AsyncSession = Depends(get_db),
):
    """Give a key a capability set it can be narrowed with.

    A key issued before capabilities existed carries no field, which means unrestricted. This is
    how such a key is narrowed without revoking it and reconfiguring the integration.

    Read-modify-write like every other metadata writer here, so the capability field is the only
    one that changes. The validation cache is evicted because the previous capability set would
    otherwise keep authorising requests until its TTL expired.
    """
    await _authorize_api_key(key_id, _rbac, db)
    vault_client = get_vault_integrations_client()

    try:
        # important: read inside the key's lock, which revoke, delete, the permission edit and a
        # rotation take too. Read before it, this write could undo a revoke that landed in between.
        handed_off: List[Any] = []
        async with serialize_key_ownership(db, key_id):
            metadata, _secret_hash = await asyncio.to_thread(
                get_api_key_from_vault, vault_client, key_id, use_cache=False
            )
            refuse_grant_edit(metadata)
            # A key holding grants on purchased clusters keeps them, but gains no capability
            # through them.
            refuse_grants_on_purchased_clusters(
                p.get("cluster_id") for p in (metadata.get("cluster_permissions") or []) if isinstance(p, dict)
            )
            previous = dict(metadata)
            was_legacy = "capabilities" not in metadata
            metadata["capabilities"] = sorted(set(request.capabilities))
            # important: an owner syncs its organizations with these, so withdrawing either one
            # freezes them like a revoke would. A legacy key holds them without naming them.
            withdrawn = tuple(
                name for name in SYNC_CAPABILITIES
                if has_capability(previous, name) and not has_capability(metadata, name)
            )
            async with serialize_org_owners(db, await owned_org_ids_to_lock(db, key_id) if withdrawn else []):
                if withdrawn:
                    handoffs = await _refuse_ending_an_owner(
                        db, key_id, action="capabilities", audit_action=CAPABILITIES_UPDATE_ACTION,
                        user=_rbac, metadata=previous, withdrawn=withdrawn, vault_client=vault_client,
                    )
                    # The key stays an owner of what another owner carries on, but not its primary.
                    handed_off = await apply_owner_handoffs(db, key_id, handoffs, remove=False)
                # Asked about rather than refused (services/integration_key_provisioning.py): an
                # owner without tenant.provision stops provisioning for its organizations.
                forced = None
                if has_capability(previous, PROVISIONING_CAPABILITY) and not has_capability(metadata, PROVISIONING_CAPABILITY):
                    forced = await confirm_provisioning_withdrawal(
                        db, key_id, user=_rbac, force=request.force, read_metadata=_owner_metadata_reader(vault_client)
                    )
                await _write_key_then_commit(db, key_id, handed_off, lambda: vault_client.vault_kv_put(
                    f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
                    metadata,
                ))
        record_owner_handoffs(handed_off, key_id=key_id, action="capabilities", actor_email=_rbac.email, remove=False)
        logger.info(
            "Set capabilities on API key %s to %s (was %s)",
            key_id, metadata["capabilities"], "legacy/unrestricted" if was_legacy else "a set",
        )

        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        _audit_credential_lifecycle(
            action=CAPABILITIES_UPDATE_ACTION, key_id=key_id, metadata=metadata, previous=previous,
            extra_details=forced,
        )

        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        return (await _with_usage([_key_info(metadata, len(user_mappings))]))[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key capabilities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not update capabilities")


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
        # important: after the authorization above, so an id outside the caller's reach is still
        # 404, and inside the key's ownership lock, so a transfer to this key cannot land between
        # the check and the write. A revoked owner freezes every organization it owns. The key is
        # read under the lock too, so this write does not undo an edit that held it before.
        async with serialize_key_ownership(db, key_id):
            metadata, _ = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id, use_cache=False)
            async with serialize_org_owners(db, await owned_org_ids_to_lock(db, key_id)):
                handoffs = await _refuse_ending_an_owner(
                    db, key_id, action="revoke", audit_action="integration.api_key.revoke",
                    user=_rbac, metadata=metadata, vault_client=vault_client,
                )
                # A revoked key leaves every owner set another owner carries on.
                handed_off = await apply_owner_handoffs(db, key_id, handoffs, remove=True)

                metadata["is_revoked"] = True
                metadata["is_active"] = False
                metadata["revoked_at"] = datetime.utcnow().isoformat()

                await _write_key_then_commit(db, key_id, handed_off, lambda: vault_client.vault_kv_put(
                    f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
                    metadata
                ))
        record_owner_handoffs(handed_off, key_id=key_id, action="revoke", actor_email=_rbac.email, remove=True)
        
        # Clear all scoped cache variants for real-time revocation
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        cache.delete("api_responses", f"integrations:api_key:{key_id}")
        # important: revocation must be immediate. _write_key_then_commit has evicted the key from
        # the process-local validation cache; without that it keeps working until its TTL expires.

        logger.info(f"API key {key_id} revoked")
        _audit_credential_lifecycle(
            action="integration.api_key.revoke", key_id=key_id, metadata=metadata
        )

        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        return (await _with_usage([_key_info(metadata, len(user_mappings))]))[0]
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not revoke the API key") from None


API_KEY_ROTATE = "integration.api_key.secret.rotate"
_ROTATE_A_DEAD_KEY = "A revoked, inactive or expired key cannot be rotated. Create a new key instead."


@router.post(
    "/api-keys/{key_id}/rotate-secret",
    response_model=APIKeyCreateResponse,
    summary="Rotate an API key's secret",
    description=(
        "Issue a new secret for the same key id. The old secret stops working at once, on every "
        "worker, and the new one is shown only once. The organizations the key owns stay with it."
    ),
)
async def rotate_api_key_secret(
    key_id: str,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Rotate a key's secret in place.

    important: the key id is what every synced organization records as its owner, so a new key
    would be refused by all of them and stop their sync. Keeping the id keeps the organizations,
    the member push and provisioning as they were.
    """
    await _authorize_api_key(key_id, _rbac, db)
    vault_client = get_vault_integrations_client()
    # important: the key is read and judged under its lock, which revoke, delete and the grant
    # edits take too. A read from before the lock can predate a revoke or a grant edit that landed
    # since, and the callback URL writes below would then put a revoked key back.
    async with serialize_key_ownership(db, key_id):
        metadata, _ = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id, use_cache=False)
        refuse_secret_rotation(metadata)
        if classify_key(metadata) != "active":
            raise HTTPException(status_code=409, detail=_ROTATE_A_DEAD_KEY)
        # important: everything that could fail runs before the write. Once the new secret is stored
        # the previous one stops working, and a failure after it would leave a secret no one was shown.
        user_mappings = await asyncio.to_thread(get_user_mappings_from_vault, vault_client, key_id)
        try:
            key_info = _key_info(metadata, len(user_mappings))
        except ValidationError:
            raise HTTPException(
                status_code=409,
                detail="This key's stored record no longer validates. Update its permissions, then rotate it.",
            ) from None
        # important: revoking, deleting or narrowing a key someone else minted hands the caller nothing,
        # but a rotation hands them a working secret. So every caller but an Admin, the key's creator
        # included, gets one only for a key whose every cluster is inside their current scope: a
        # creator whose scope has shrunk must not take a secret reaching clusters outside it. A
        # non-creator is also held to the project bound; the creator is not, as at creation.
        # Managing the key at all already needed its side (_authorize_api_key).
        allowed = get_current_allowed_clusters()
        if allowed is not None:
            outside = sorted({grant.cluster_id for grant in key_info.cluster_permissions} - set(allowed))
            if outside:
                _audit_credential_lifecycle(
                    action=API_KEY_ROTATE, key_id=key_id, metadata=metadata,
                    result="denied", error_code="cluster_out_of_scope",
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"This key also grants {outside}, which you cannot reach. An administrator can rotate it.",
                )
            if str(metadata.get("created_by") or "").lower() != (_rbac.email or "").lower():
                try:
                    await _refuse_a_grant_past_the_project_bound(db, _rbac.email, key_info.cluster_permissions)
                except HTTPException as exc:
                    if exc.status_code == 403:
                        _audit_credential_lifecycle(
                            action=API_KEY_ROTATE, key_id=key_id, metadata=metadata,
                            result="denied", error_code="project_out_of_bound",
                        )
                    raise
            # important: and for a key that owns organizations, only as a current partner of the key's
            # home organization, the creator too. A fresh secret for such a key is the sync, members
            # and provisioning of every organization it owns. A partner row in a buyer organization
            # does not count, nor does created_by, and a home no sale proves leaves the rotation to an
            # Administrator: see keys_whose_home_the_user_partners.
            try:
                partners_home = await partners_the_home_organization(db, key_id, _rbac.email)
            except Exception as exc:  # noqa: BLE001 - unknown refuses; the text names no database
                logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
                raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
            if partners_home is False:
                _audit_credential_lifecycle(
                    action=API_KEY_ROTATE, key_id=key_id, metadata=metadata,
                    result="denied", error_code="outside_the_home_organization",
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "This key owns OrcaHub organizations, so only an Orcastra administrator can rotate "
                        "its secret, or a partner of the key's own organization once the key has "
                        "provisioned a buyer on a cluster that organization registered. Ask an Orcastra "
                        "administrator to rotate it."
                    ),
                )
        try:
            forgot = await asyncio.to_thread(_forget_callback_url_blocking, key_id)
        except Exception:  # noqa: BLE001 - reported below as a refusal, with nothing changed
            forgot = False
        if forgot is False:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The secret was not rotated because the key's callback URL could not be cleared. "
                    "Nothing changed. Try again."
                ),
            )
        key_secret, secret_hash = generate_api_key_secret()
        try:
            await asyncio.to_thread(save_api_key_secret_to_vault, vault_client, key_id, secret_hash, key_secret)
        except Exception as e:
            # The write may have landed before its reply was lost, so every worker still drops
            # its cached copy and checks Vault again.
            evict_api_key_cache(key_id)
            logger.error("Failed to rotate the secret of API key %s: %s", key_id, type(e).__name__)
            # 503, so the sentence reaches the operator. A 500 is answered with a reference.
            raise HTTPException(
                status_code=503,
                detail="The secret rotation did not confirm. Rotate again before using this key.",
            ) from None
        # The validation cache is keyed by id and secret, and the tombstone makes every worker
        # drop its copy, so the replaced secret fails on the next request wherever it lands.
        evict_api_key_cache(key_id)
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        cache.delete("api_responses", f"integrations:api_key:{key_id}")
    # No second clear. A sync authenticated with the replaced secret stores its URL under this
    # lock and only while that secret is current (_bind_callback_url_blocking), and a URL stored
    # with the replaced secret is never used with the new one (bound_callback_url).
    _audit_credential_lifecycle(
        action=API_KEY_ROTATE, key_id=key_id, metadata=metadata,
        extra_details={"callback_url_cleared": bool(forgot)},
    )
    logger.info("API key %s secret rotated", key_id)
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
        # Ownership first, so an add-on credential that owns organizations is answered with the
        # remedy for that (transfer them, or suspend the add-on) rather than the installation's.
        async with serialize_key_ownership(db, key_id):
            # Read before the delete: afterwards there is nothing left to describe what was removed.
            metadata, _ = await asyncio.to_thread(get_api_key_from_vault, vault_client, key_id, use_cache=False)
            async with serialize_org_owners(db, await owned_org_ids_to_lock(db, key_id)):
                handoffs = await _refuse_ending_an_owner(
                    db, key_id, action="delete", audit_action="integration.api_key.delete",
                    user=_rbac, metadata=metadata, vault_client=vault_client,
                )
                await refuse_delete(db, key_id, metadata)
                handed_off = await apply_owner_handoffs(db, key_id, handoffs, remove=True)

                await _write_key_then_commit(db, key_id, handed_off, lambda: _raise_if_incomplete(delete_api_key_from_vault(vault_client, key_id)))
        record_owner_handoffs(handed_off, key_id=key_id, action="delete", actor_email=_rbac.email, remove=True)

        # Clear all scoped cache variants (the validation cache went with the delete)
        cache = get_cache_manager()
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        await api_key_usage.forget(key_id)

        _audit_credential_lifecycle(
            action="integration.api_key.delete", key_id=key_id, metadata=metadata
        )

        return {"success": True, "message": f"API key {key_id} deleted"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not delete the API key") from None


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
        
        now = datetime.utcnow().isoformat()
        user_mapping = ExternalUserMapping(
            external_user_id=request.external_user_id,
            external_email=request.external_email,
            external_name=request.external_name,
            permissions=request.cluster_permissions,
            created_at=now,
            updated_at=now,
        )
        
        save_user_mapping_to_vault(vault_client, key_id, user_mapping)

        # Mapping counts are cached with the key list, so clear it like the other key writers.
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
        # The mapping count is cached with the key list, as for add_user_mapping.
        get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
        
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
        metadata, secret_hash = await asyncio.to_thread(get_api_key_from_vault, vault_client, request.api_key_id)
        
        if not verify_api_key_secret(request.api_key_secret, secret_hash):
            return APIKeyValidateResponse(valid=False, error="Invalid API key or secret")

        if metadata.get("is_revoked", False):
            return APIKeyValidateResponse(valid=False, error="API key revoked")

        if not metadata.get("is_active", True):
            return APIKeyValidateResponse(valid=False, error="API key not active")

        # Same expiry decision as the header dependency. A naive/aware comparison here would raise
        # into the broad `except Exception` below and answer a generic "Validation failed".
        expires_at = metadata.get("expires_at")
        if is_expired(expires_at):
            return APIKeyValidateResponse(valid=False, error="API key expired")
        
        raw_permissions = metadata.get("cluster_permissions", [])
        grant_ids = [perm.get("cluster_id") for perm in raw_permissions if perm.get("cluster_id")]
        unregistered = await _unregistered_cluster_ids(grant_ids)
        generations = await _generations_for_scope()
        # A paused grant's id may be another organization's cluster now: never look up its name.
        live_ids = cluster_generations.reachable(raw_permissions, generations)

        # Enrich cluster permissions with cluster names from Vault
        enriched_permissions = []
        settings = get_settings()
        cluster_names: Dict[str, str] = {}
        if settings.vault_enabled and settings.vault_token:
            try:
                from app.services.lxd_cert_manager import LXDCertificateManager
                vault = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault,
                    clusters_path=settings.vault_clusters_path
                )
                lookups = [c for c in dict.fromkeys(grant_ids) if c not in unregistered and c in live_ids]
                cluster_names = await asyncio.to_thread(_cluster_display_names_sync, cert_manager, lookups)
            except Exception as e:
                logger.warning(f"Could not enrich cluster names: {e}")

        for perm in _with_registration_state(raw_permissions, generations):
            cluster_id = perm.get("cluster_id")
            enriched_perm = _reported_grant(dict(perm), unregistered)
            enriched_perm["cluster_name"] = cluster_names.get(cluster_id, cluster_id)
            enriched_permissions.append(ClusterPermission(**enriched_perm))
        
        # This endpoint takes credentials in the body, so it never passes through
        # validate_api_key and would otherwise be the one metered path a valid key could spend
        # without limit. Counted here, after the secret verified, for the same reason as there.
        if not consume_api_key_budget(request.api_key_id):
            return APIKeyValidateResponse(
                valid=False, error="Rate limit exceeded for this API key"
            )

        # granted_capabilities returns None for a legacy key, which is what the response means by
        # null. An integrator reads this to discover what it may call before it calls it, so it
        # must not be flattened into an empty list.
        granted = granted_capabilities(metadata)
        return APIKeyValidateResponse(
            valid=True,
            app_name=metadata.get("app_name"),
            app_type=metadata.get("app_type"),
            capabilities=sorted(granted) if granted is not None else None,
            expires_at=metadata.get("expires_at"),
            expires_in_days=days_remaining(metadata.get("expires_at")),
            cluster_permissions=enriched_permissions,
            server=ServerIdentity(**build_identity.full()),
            features=_integration_features(),
        )
        
    except HTTPException as exc:
        # A key store Vault cannot serve is not an invalid key: the same 503 as every other key
        # door, so an integrator retries instead of discarding a good credential. Every id gets
        # it during an outage, so it says nothing about which ids exist.
        if exc.status_code == 503:
            raise
        # The same wording as a wrong secret. This endpoint takes no authentication, so telling
        # the two apart would reveal which key ids exist.
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
    # The secret the dependency above already verified, read again only to bind app_url to it.
    x_api_key_secret: Optional[str] = Header(None, alias="X-API-Key-Secret"),
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
                db, request.organizations, api_key_id, add_on=is_add_on_credential(api_key)
            )
            result.organizations_synced = orgs_synced

        # ── Sync members from external app ──
        if request.members:
            members_synced, members_removed = await _upsert_synced_members(
                db, request.members, api_key_id, creator_of(api_key),
                tuple(o.id for o in request.organizations),
            )
            result.members_synced = members_synced
            result.members_removed = members_removed

        # ── Offboarding reconcile (flag-gated, default off): drop members missing from the
        #    pushed roster of each synced org. See _reconcile_synced_members for the guards. ──
        _settings = get_settings()
        if _settings.member_reconcile_enabled and request.organizations:
            result.members_removed += await _reconcile_synced_members(
                db, request.organizations, request.members, api_key_id,
                dry_run=_settings.member_reconcile_dry_run,
            )

        # ── Store callback URL in Vault API key metadata ──
        if request.app_url:
            # important: a write gated like the organization push. `app_url` is the base that
            # `refresh_organizations` fetches `/api/external/organizations` from, and it feeds
            # the response straight into _upsert_synced_organizations. Setting it without
            # org.sync would give organization creation through the back door and aim an
            # authenticated server-side request at a host of the caller's choosing.
            assert_capability(api_key, "org.sync")
            # important: under the key's lock, bound to the secret this sync presented, and only if
            # that is still the key's secret. A sync already in flight when a rotation ran would
            # otherwise set the URL the new secret is then sent to. Vault write, so off the loop
            # like the rest of this handler. Best effort: a bookkeeping write must not fail the sync.
            presented = x_api_key_secret if isinstance(x_api_key_secret, str) else None
            try:
                async with serialize_key_ownership(db, api_key_id):
                    # important: waited out, never cut off, as the rotation's writes are. A timeout
                    # would stop the wait but not the thread, whose read-modify-write of the whole
                    # key document would then land outside the lock and could undo a revoke or a
                    # grant edit made meanwhile. The Vault client bounds each attempt itself.
                    await asyncio.to_thread(_bind_callback_url_blocking, api_key_id, request.app_url, presented)
            except Exception as e:
                logger.warning(f"Could not store app_url for {api_key_id}: {e}")
            finally:
                # Not held through the inventory reads below, whether or not the write worked.
                try:
                    await release_key_locks(db)
                except Exception as e:  # noqa: BLE001 - the lock ends with the session either way
                    logger.warning(f"Could not release the key lock of {api_key_id} after the sync: {e}")
        
        allowed_clusters = {
            p["cluster_id"] for p in api_key.get("cluster_permissions", [])
        }

        requested_clusters = set(request.cluster_ids) if request.cluster_ids else allowed_clusters
        cluster_ids = allowed_clusters.intersection(requested_clusters)

        if request.include_clusters:
            settings = get_settings()
            if settings.vault_enabled and settings.vault_token:
                from app.services.lxd_cert_manager import LXDCertificateManager
                vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault_client,
                    clusters_path=settings.vault_clusters_path
                )
                
                result.clusters = (await _gather_cluster_rows(api_key, cert_manager, cluster_ids)).items
        
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


# OrcaHub polls /integrations/regions about every 2.4 minutes. Each cluster's status read is a
# few seconds on a busy host, so they run side by side on their own pool, apart from the proxy
# pool the rest of this API shares, under a wall each.
_TOTALS_WORKERS = 4
_TOTALS_EXECUTOR = ThreadPoolExecutor(max_workers=_TOTALS_WORKERS, thread_name_prefix="integration_totals")
_TOTALS_WALL_SECONDS = 15
_TOTALS_FRESH_SECONDS = 120
# Some cluster did not answer: remembered for less, so the next poll reads it again soon.
_TOTALS_DEGRADED_SECONDS = 30
_TOTALS_KEYS = ("cpu", "memory", "storage", "gpu", "gpu_cards")


async def _integration_hardware_totals(
    lxd_service: LXDService, allowed_clusters: set
) -> Dict[str, int]:
    """Real hardware totals for the clusters an API key may see.

    important: this reaches the clusters, so it is cached and threaded. The result is keyed by
    the cluster set rather than by the key id, so two keys with the same access share one
    result. Unreachable clusters contribute nothing instead of blocking the response: the
    shared circuit breaker returns them immediately.

    The GPU total counts compute GPUs, matching every internal surface.

    Stale-while-revalidate: a poll answers from the cache, up to SWR_MAX_STALE_SECONDS past its
    soft TTL, while one background read replaces it. A read where no cluster answered is not
    remembered.
    """
    empty = {key: 0 for key in _TOTALS_KEYS}
    if not allowed_clusters:
        return empty

    clusters = sorted(allowed_clusters)
    cache = get_cache_manager()
    digest = hashlib.sha256(",".join(clusters).encode()).hexdigest()[:16]

    async def build() -> Built:
        statuses = await asyncio.gather(
            *(
                # Queued for one of the pool's slots, never refused one: a key granting more
                # clusters than the pool has threads must still count every cluster.
                run_in_slot(
                    "integration_totals",
                    _TOTALS_WORKERS,
                    lxd_service.check_node_status,
                    cluster_id,
                    timeout=_TOTALS_WALL_SECONDS,
                    label=f"integration.totals[{cluster_id}]",
                    executor=_TOTALS_EXECUTOR,
                )
                for cluster_id in clusters
            ),
            return_exceptions=True,
        )
        totals = dict(empty)
        answered = 0
        for cluster_id, status in zip(clusters, statuses):
            if isinstance(status, BaseException):
                logger.debug(f"Could not read hardware for {cluster_id}: {type(status).__name__}")
                continue
            resources = status.resources if status.connected else None
            if not resources:
                continue
            answered += 1
            totals["cpu"] += resources.cpu_total
            totals["memory"] += resources.memory_total
            totals["storage"] += resources.storage_total
            totals["gpu"] += resources.gpu_total
            totals["gpu_cards"] += resources.gpu_cards_total
        if answered == 0:
            fresh_for = None
        elif answered < len(clusters):
            fresh_for = _TOTALS_DEGRADED_SECONDS
        else:
            fresh_for = _TOTALS_FRESH_SECONDS
        return Built(value=totals, body=totals, fresh_for=fresh_for)

    served = await swr.serve(
        cache=cache,
        # The regions: prefix puts it in the reachability and access sweeps (cache.py).
        base_key=f"regions:integration_hardware:v1:{digest}",
        fresh=False,
        build=build,
        generation=lambda: (
            get_resource_list_generation(cache, CAPACITY_GENERATION_KIND),
            get_resource_list_generation(cache, "regions"),
        ),
        label="integration hardware totals",
    )
    body = served.value if served.built else served.body
    return {key: int((body or {}).get(key, 0)) for key in _TOTALS_KEYS}


# ============================================
# Resource-shaped reads for integrations
# ============================================
#
# Plain GET reads for a key holder, beside the proxy-shaped POST /integrations/raw-proxy. GET
# /instances, /nodes and /projects are the JWT routers and answer 401 to a key, so without these
# an integrator's first call fails and reads as "this needs a login".
#
# They add no reach. Each one is the data POST /integrations/sync already returns to the same
# caller, through the same fetch helpers, narrowed by the same intersection with the key's
# cluster_permissions. Only the shape differs.


# Which audit action a raw-proxy path belongs under. A call that reaches inside a guest is the
# same kind of event as opening a console, so it is named and rated like one; everything else is
# ordinary proxy traffic.
_PROXY_AUDIT_ACTIONS: Dict[Optional[str], str] = {
    "instance.exec": "integration.instance.exec",
    "instance.files": "integration.instance.files",
    # Named per capability rather than one "desktop" action, so an alert can tell an agent
    # looking at a screen from an agent typing into it. They are different events to a reviewer.
    "desktop.view": "integration.instance.desktop_view",
    "desktop.input": "integration.instance.desktop_input",
    "desktop.clipboard": "integration.instance.desktop_clipboard",
}


async def _assert_desktop_allowed(
    api_key: Dict[str, Any],
    request: "ProxyRequest",
    operation: str,
    capability: str,
) -> None:
    """Everything a desktop action needs beyond the capability and the cluster grant.

    The capability says what the integration may attempt. This adds what it cannot express:
    whether the organization owning the screen agreed, whether a human has authorised this key
    for a bounded time, and whether the key has already done enough of this in the last minute.

    It lives here rather than in `services/desktop/entry.py` because it needs the database and
    the owning organization, and `entry.desktop_action` runs on a worker thread through
    `run_blocking`. `proxy_operation` takes no session either, so this opens its own, as
    `routers/console.py` does for the break-glass check on the human path.

    Every refusal is audited: a key attempting a screen it was never authorised for is the
    signal worth alerting on.
    """
    from app.core.database import async_session_maker
    from app.core.org_scope import resolve_cluster_org
    from app.services import break_glass_service
    from app.services.desktop import budget as desktop_budget
    from app.services.desktop_policy import consent_for_cluster

    key_id = str(api_key.get("id") or "")
    instance = str((request.params or {}).get("name") or "")
    # Filled in as soon as the session opens, so a refusal is attributable to the tenant whose
    # screen was reached even when it is refused before anything else happens.
    org: Tuple[Optional[int], Optional[str]] = (None, None)

    def _refuse(
        status: int, detail: str, refused: str, headers=None, dissenting: Optional[str] = None
    ) -> None:
        _audit_key_action(
            api_key,
            action=_guest_audit_action(operation),
            target_id=instance,
            cluster_id=request.cluster_id,
            project=request.project,
            result="failure",
            details={
                "operation": operation,
                **_guest_audit_details(operation, request.params or {}),
                "refused": refused,
                # Only ever here, never in the response. See the comment on the consent check.
                **({"dissenting_organization": dissenting} if dissenting else {}),
            },
            external_user_email=request.external_user_email,
            error_message=refused,
            target_organization_id=org[0],
            target_organization_slug=org[1],
        )
        raise HTTPException(status_code=status, detail=detail, headers=headers)

    async with async_session_maker() as db:
        org = await resolve_cluster_org(request.cluster_id, db)

        # important: every organization sharing the cluster, not only the one it is attributed to.
        # A cluster can carry more than one binding, so consulting a single row would let one
        # tenant's yes authorise capturing another tenant's screens.
        consent, dissenting = await consent_for_cluster(db, request.cluster_id)
        # important: `dissenting` is recorded, never returned. It names a co-tenant of a shared
        # cluster, and this refusal is reachable by any key holding the capability before the
        # grant is checked, so the message would tell an unauthorised caller which organizations
        # share the cluster and what each has consented to.
        if not consent.may_view:
            _refuse(
                403,
                (
                    "An organization sharing this cluster has not enabled desktop control. "
                    "Screen contents leaving for a model is a decision that belongs to the "
                    "organization rather than to the integration, so an administrator of every "
                    "organization on this cluster has to turn it on first."
                ),
                "organization_has_not_consented",
                dissenting=dissenting,
            )
        if capability == "desktop.clipboard" and not consent.may_clipboard:
            _refuse(
                403,
                (
                    "An organization sharing this cluster has not consented to an agent reading "
                    "its clipboards. Watching a screen and lifting whatever was last copied on "
                    "it are separate decisions."
                ),
                "organization_has_not_allowed_clipboard",
                dissenting=dissenting,
            )
        if capability == "desktop.input" and not consent.may_input:
            _refuse(
                403,
                (
                    "An organization sharing this cluster allows an agent to see its screens and "
                    "not to type into them. Watching and touching are separate consents."
                ),
                "organization_has_not_allowed_input",
                dissenting=dissenting,
            )

        # The pin is passed so a grant issued against a different registration of the same
        # cluster id is refused rather than inherited, while a public-CA renewal is not.
        pin_state = await break_glass_service.cluster_pin_state(request.cluster_id)
        grant = await break_glass_service.get_active_key_grant(
            db, key_id, request.cluster_id, pin_state=pin_state
        )
        if grant is None:
            _refuse(
                403,
                (
                    "This key holds no live break-glass grant for this cluster. An unattended "
                    "caller reaches a customer's desktop through a grant that names a reason, "
                    "expires, and can be revoked, which is the same bar a human is held to. "
                    "Request one with POST /api/v1/break-glass/grants."
                ),
                "no_active_grant",
            )

    allowed, retry_after = desktop_budget.consume(
        key_id=key_id,
        cluster_id=request.cluster_id,
        project=request.project,
        instance=instance,
        capture=operation in ("desktop_view", "desktop_probe"),
        # Input fails closed when the budget store cannot answer. Every other limiter here fails
        # open, which is not acceptable for typing into somebody's desktop.
        fail_closed=capability == "desktop.input",
    )
    if not allowed:
        _refuse(
            429,
            (
                f"This key has spent its desktop budget for {instance} on {request.cluster_id}. "
                "The budget is per instance, so other guests are unaffected."
            ),
            "desktop_budget_exhausted",
            headers={"Retry-After": str(retry_after or 60)},
        )


def _guest_audit_action(operation: str) -> str:
    """The audit action for a guest-reaching operation.

    important: `.get`, never `[]`. A missing entry must degrade to a generic name: a KeyError here
    would lose the record of a refused attempt, and a timeout's audit row and "may still be
    running" message.
    """
    return _PROXY_AUDIT_ACTIONS.get(
        GUEST_ACCESS_OPERATIONS.get(operation), "integration.proxy.request"
    )


def _guest_audit_details(operation: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """What is worth recording about a guest-reaching call, and nothing that is not.

    An exec is described by its argv, a file call by its path, and a desktop action by what it
    did and where. Recording every shape unconditionally would put nulls in every record and tell
    a reader nothing about which call this was.

    important: typed text is recorded by length only, as exec output is: the first thing an agent
    types at a login prompt is a password.
    """
    if operation == "exec_instance":
        return {
            "command": params.get("command"),
            "cwd": params.get("cwd"),
            "run_as_user": params.get("user"),
        }
    if operation in ("get_file", "put_file"):
        return {"path": params.get("path")}
    text = params.get("text")
    return {
        "desktop_action": params.get("action"),
        "coordinate": params.get("coordinate"),
        "text_length": len(text) if isinstance(text, str) else None,
    }


def _proxy_audit_action(method: str, path: str) -> str:
    """What to call this raw-proxy call in the audit trail.

    Guest-reaching calls are named first, because reaching inside a guest is the more serious
    fact about a call that also happens to delete something. A destructive call comes next: a
    DELETE through the proxy removes the same resource the dashboard's own endpoint would, and
    naming it `integration.proxy.request` would put it outside HIGH_SEVERITY_ACTIONS, hidden from
    anything alerting on `image.delete`. Everything else is ordinary proxy traffic.
    """
    guest = _PROXY_AUDIT_ACTIONS.get(guest_access_capability_for(path))
    if guest:
        return guest
    return proxy_destructive_action_for(method, path) or "integration.proxy.request"


def _audit_credential_lifecycle(
    *,
    action: str,
    key_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    result: str = "success",
    error_message: Optional[str] = None,
    previous: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an operator creating, revoking or deleting an integration credential.

    Distinct from `_audit_key_action`: there the actor is the key; here it is a human holding a
    JWT, and `log_activity` fills that in from the request context. The app log is not routed to
    the audit index, so this is the only place an operator can see who minted a credential.

    `api_key_id` is public and allowlisted by the audit sanitiser. The secret never passes
    through here, and the metadata is reduced to the fields that describe the grant. An edit
    passes `previous`, the metadata before the write, so the record also says which capabilities
    it granted and withdrew.
    """
    metadata = metadata or {}
    try:
        activity_logger.log_activity(
            action=action,
            target_type="api_key",
            target_id=key_id,
            result=result,
            error_code=error_code,
            error_message=error_message,
            details={
                "api_key_id": key_id,
                "app_name": metadata.get("app_name"),
                "app_type": metadata.get("app_type"),
                "capabilities": metadata.get("capabilities"),
                **capability_audit_fields(metadata, previous),
                "cluster_ids": [
                    grant.get("cluster_id")
                    for grant in (metadata.get("cluster_permissions") or [])
                    if isinstance(grant, dict)
                ],
                "expires_at": metadata.get("expires_at"),
                **(extra_details or {}),
            },
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the request
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


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
    target_organization_id: Optional[int] = None,
    target_organization_slug: Optional[str] = None,
) -> None:
    """Record what a key did, in the index a human reviews rather than only the app log.

    The owning organization is passed through, so an action against a tenant's guest is
    attributable to that tenant and appears in a tenant-scoped view of the audit, as on the human
    console and terminal paths.

    The app log is no substitute: it never records the body, is not routed to the audit index,
    and `LogDeduplicationFilter` collapses a hundred different commands against one instance
    inside a minute into a single line.

    important: the actor is the key, not `external_user_email`. That field is supplied by the
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
            target_organization_id=target_organization_id,
            target_organization_slug=target_organization_slug,
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

    `is_expired` is derived from the timestamp, not from `classify_key`. The classifier ranks
    revocation above expiry so the badge reads "Revoked", but the field is documented as "past
    its expiration" and an integration branches on it. A key that is both would otherwise report
    `is_expired: false` next to `expiry_state: expired`.
    """
    grants = metadata.get("cluster_permissions") or []
    generations = cluster_generations.known()
    # Never the stamp; a paused grant (its registration ended) reads `registered: false`, and, for
    # the people who manage the key, `rebindable` when its id is registered again.
    shown = (
        cluster_generations.annotate(grants, generations, for_managers=True)
        if generations is not None
        else [{k: v for k, v in g.items() if k != cluster_generations.STAMP} if isinstance(g, dict) else g for g in grants]
    )
    return APIKeyInfo(
        **{**metadata, "cluster_permissions": shown},
        is_expired=is_expired(metadata.get("expires_at")),
        expiry_state=expiry_state(metadata.get("expires_at")),
        user_mappings_count=user_mappings_count,
        is_add_on_credential=is_add_on_credential(metadata),
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

    A call that timed out or was shed may still have reached the cluster. Recording only the ones
    that answered would leave the slowest, the ones worth reviewing, out of the index.
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


def _audit_raw_refusal(
    api_key: Dict[str, Any],
    request: Any,
    method: str,
    project: Optional[str],
    refusal: Exception,
    refused: str,
) -> None:
    """The failure record a raw-proxy gate leaves when it refuses a request before it is sent.

    Reads the path, the cluster and the claimed email only, never the body, so a gate that
    runs before the body is held to one spelling may hand it the request.
    """
    _audit_key_action(
        api_key,
        action=_proxy_audit_action(method, request.path),
        target_id=request.path[:512],
        cluster_id=request.cluster_id,
        project=project,
        result="failure",
        details={"method": method, "refused": refused},
        external_user_email=request.external_user_email,
        error_message=str(getattr(refusal, "detail", refusal)),
    )


def _observe_policy_key_write(
    api_key: Dict[str, Any],
    keys: List[str],
    *,
    cluster_id: str,
    project: Optional[str],
    target: str,
    external_user_email: Optional[str] = None,
) -> None:
    """Record a key setting the upstream product's snapshot-schedule keys. Refuse nothing yet.

    important: this observes and does not gate. `snapshots.schedule` decides whether the console
    treats an instance as owned by the metered product sold upstream, which decides whether an
    operator's own snapshot actions on it are refused. Any key with `lxd.proxy` and `write` can
    set it, because the proxy prices an instance-config change by what it reaches inside the
    guest and these keys reach sideways into another product.

    Which keys legitimately write them is not knowable from this repository, so the record comes
    first. A refusal would be an explicit, never-inherited capability seeded only for the
    application type that owns the schedules. The console's own create paths already refuse these
    keys at the request boundary, because no user-authenticated caller has a reason to set them.
    """
    if not keys:
        return
    logger.warning(
        "Policy key write: key %s (%s) set %s on %s/%s/%s",
        api_key.get("id", "unknown"),
        api_key.get("app_name", "unknown"),
        ", ".join(keys),
        cluster_id,
        project or "default",
        target,
    )
    _audit_key_action(
        api_key,
        action="integration.instance.config.policy_key_write",
        target_id=target[:512],
        cluster_id=cluster_id,
        project=project,
        result="success",
        details={"policy_keys": keys},
        external_user_email=external_user_email,
    )


def _clusters_for_key(api_key: Dict[str, Any], cluster_id: Optional[str]) -> set:
    """The clusters this request may touch: the key's grant, optionally narrowed by one id.

    An intersection, the same one sync_data performs. A caller-supplied cluster that is not in
    the grant is not in the result, so an unknown id and an unauthorised one are
    indistinguishable and neither can be probed.
    """
    allowed = {
        grant["cluster_id"]
        for grant in api_key.get("cluster_permissions", [])
        if isinstance(grant, dict) and "cluster_id" in grant
    }
    if cluster_id:
        return allowed & {cluster_id}
    return allowed


def _frozen_grant(api_key: Dict[str, Any], cluster_id: str) -> Optional[FrozenSet[str]]:
    """`_granted_projects` in the shape an operation answer is filtered by (None means `*`)."""
    granted = _granted_projects(api_key, cluster_id)
    return None if granted is None else frozenset(granted)


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
    see instances outside `default`. Narrowing back to the grant therefore has to happen here,
    as on the proxy paths, or a key scoped to one project enumerates all of them.

    `project_field` exists because a project row names itself in `name` while an instance row
    names its project in `project`. The wrong field silently keeps every row, so callers state it.
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

    `/validate` answers the same but takes the credentials in the body; this takes the headers
    every other endpoint wants, so a caller sending them does not get a 422.

    No capability is required, and it is exempt in the capability ratchet for the reason
    `/validate` carries: it grants nothing and reports only what the presented key already holds.
    """
    granted = granted_capabilities(api_key)
    # What the key holds, paused grants included: the published contract reports every stored entry.
    grants = [
        grant for grant in api_key.get("_held_cluster_permissions") or api_key.get("cluster_permissions", [])
        if isinstance(grant, dict) and "cluster_id" in grant
    ]
    unregistered = await _unregistered_cluster_ids(grant["cluster_id"] for grant in grants)
    grants = _with_registration_state(grants, await _generations_for_scope())
    return APIKeyValidateResponse(
        valid=True,
        app_name=api_key.get("app_name"),
        app_type=api_key.get("app_type"),
        capabilities=sorted(granted) if granted is not None else None,
        expires_at=api_key.get("expires_at"),
        expires_in_days=days_remaining(api_key.get("expires_at")),
        cluster_permissions=[
            ClusterPermission(**_reported_grant(grant, unregistered))
            for grant in grants
        ],
        server=ServerIdentity(**build_identity.full()),
        features=_integration_features(),
    )


async def _gather_cluster_rows(api_key: Dict[str, Any], cert_manager, cluster_ids) -> ClusterFanout:
    """Every cluster row a key's listing returns: registration, last known state, its own access.

    `skip_tripped=False` because the fetch reads only Vault. A tripped cluster stays a row whose
    status says offline or not_authorized, rather than an `unreachable` entry with the breaker's
    raw text, which reads as "this cluster may not exist" to an integration.
    """
    fanout = await _gather_per_cluster(
        _fetch_cluster_info, cert_manager, cluster_ids, "cluster_info", skip_tripped=False
    )
    stamp_cluster_rows(api_key, fanout.items)
    return fanout


@external_router.get(
    "/clusters",
    summary="List the clusters this key may reach",
    description=(
        "The clusters in the key's grant. Each row carries `status`, `reason_code`, `reason` and "
        "`checked_at` from the last check the server already holds (all null when nothing "
        "checked the cluster recently; this read never probes a cluster), and `access`, the "
        "level and projects this key holds on it. `unreachable` lists clusters whose "
        "registration could not be read."
    ),
)
async def list_integration_clusters(
    cluster_id: Optional[str] = Query(None, description="Narrow to one cluster"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
):
    """The answer to "which cluster_id do I pass" for a key holder."""
    settings = get_settings()
    if not (settings.vault_enabled and settings.vault_token):
        raise HTTPException(status_code=503, detail="Vault is not configured")

    from app.services.lxd_cert_manager import LXDCertificateManager

    cert_manager = LXDCertificateManager(
        vault_client=get_vault_client(settings.vault_addr, settings.vault_token),
        clusters_path=settings.vault_clusters_path,
    )
    fanout = await _gather_cluster_rows(api_key, cert_manager, _clusters_for_key(api_key, cluster_id))
    return {
        "clusters": fanout.items,
        "total": len(fanout.items),
        "partial": fanout.partial,
        "unreachable": fanout.unreachable,
    }


@external_router.get(
    "/clusters/failover",
    summary="Read how the clusters this key may reach recover from a lost member",
    description=(
        "Whether each cluster is clustered, and its automatic healing and offline thresholds and "
        "database voter limits, read live. Numbers only: the rest of the server configuration "
        "is never returned. A threshold of 0 means automatic healing is off."
    ),
)
async def list_integration_cluster_failover(
    cluster_id: Optional[str] = Query(None, description="Narrow to one cluster"),
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """The one server setting a marketplace needs before it may call a cluster highly available.

    Gated by inventory.read, like every other read here. It adds no reach: the key can already
    list these clusters, and the answer names no host, address or credential.
    """
    fanout = await _gather_per_cluster(
        _fetch_cluster_failover, lxd_service, _clusters_for_key(api_key, cluster_id), "failover_config"
    )
    clusters = sorted(fanout.items, key=lambda row: row["cluster_id"])
    return {
        "clusters": clusters,
        "total": len(clusters),
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
    # important: requests folds a dot segment out of the path it sends, so ".." would read LXD's server
    # root and answer its config as an instance. Refused here, which MCP's orcastra_get_instance calls too.
    lxd_segment(name, what="Instance name")
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
        # 404 whether the name does not exist or sits in a project the key was not granted.
        # Splitting the two would let a caller enumerate other tenants' instance names one
        # guess at a time.
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
        # A name is unique only per project, so report every match rather than pick one.
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
        refuse_a_paused_grant(api_key, cluster_id)
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
            visible_projects=_frozen_grant(api_key, cluster_id),
            timeout=RAW_PROXY_TIMEOUT,
            label=f"integration_operation[{cluster_id}]",
            executor=proxy_executor,
            max_in_flight=PROXY_MAX_IN_FLIGHT,
        )
    except BlockingTimeout as timeout_error:
        raise HTTPException(status_code=504, detail=str(timeout_error)) from timeout_error
    except BlockingPoolFull as pool_error:
        # Chained so MCP can tell a busy server from a down cluster; both are 503 here.
        raise HTTPException(status_code=503, detail=str(pool_error)) from pool_error
    except HTTPException:
        raise
    except Exception as e:
        # important: an id LXD does not know must not reach the generic 500 handler, which
        # renders "Internal server error". A caller polling an operation that has already been
        # reaped has to be able to tell "gone" from "the server is broken".
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
    description=(
        "Return non-sensitive geographic region coordinates for clusters allowed by the API key. "
        "A region whose id is 'c-' and a lowercase ISO code stands for the whole country: its city is "
        "the country name and its coordinates are a representative point inside the country, not the site."
    ),
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
        # A grant can name an id no cluster is registered under; it has no location to report.
        allowed_clusters = allowed_clusters - await _unregistered_cluster_ids(allowed_clusters)

        if settings.vault_enabled and settings.vault_token:
            from app.services.lxd_cert_manager import LXDCertificateManager
            vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
            cert_manager = LXDCertificateManager(
                vault_client=vault_client,
                clusters_path=settings.vault_clusters_path
            )

            infos = await asyncio.to_thread(_cluster_infos_sync, cert_manager, sorted(allowed_clusters))
            for cluster_id, cluster_info in infos.items():
                try:
                    location_data = _public_location_data(
                        stored_location_fields(cluster_id, cluster_info.get("metadata"))
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
            # This endpoint reports hardware, not workload. Instance counts come from a different
            # source and are not enumerated here, so the key stays 0.
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
        cluster_permissions = {
            p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])
        }
        
        if request.cluster_id not in cluster_permissions:
            refuse_a_paused_grant(api_key, request.cluster_id)
            raise HTTPException(
                status_code=403,
                detail=f"No permission for cluster {request.cluster_id}"
            )
        
        perm = cluster_permissions[request.cluster_id]

        project = request.project or "default"
        allowed_projects = perm.get("projects", ["default"])
        if project not in allowed_projects and "*" not in allowed_projects:
            raise HTTPException(
                status_code=403,
                detail=f"No permission for project {project}"
            )
        
        method = request.method.upper()

        # Validated out here in the outer try so it surfaces as a real HTTP 400. Raised
        # inside the inner block it would be swallowed by the generic handler and downgraded
        # to HTTP 200 carrying status_code 500.
        if method not in ALLOWED_METHODS:
            raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

        # important: the trust surface is refused for every grant, and each refusal is recorded like the guest
        # gates below, so a key probing it shows in the trail. A malformed path is the parser's 400, not recorded.
        try:
            assert_proxy_path_allowed(request.path)
            # important: server configuration and the cluster hand out trust (the OIDC issuer, a join
            # token for a new member), so writes to them are refused for every grant, `*` included.
            assert_server_state_unchanged(request.path, method)
            # important: a storage bucket key answers with the bucket's S3 secret, a credential that
            # outlives the API key, so the keys are refused for every grant and method like the trust
            # store (core/storage_bucket_keys.py).
            assert_bucket_keys_unreached(request.path)
        except HTTPException as refusal:
            if refusal.status_code == 403:
                _audit_raw_refusal(api_key, request, method, project, refusal, "refused_for_every_key")
            raise
        # important: reaching inside a guest is a different power from managing it.
        # `_REFUSED_PROXY_PREFIXES` holds only `/certificates`, so without this
        # `POST /1.0/instances/<n>/exec` would run arbitrary commands as root for any key with
        # `lxd.proxy` + `write`, and a GET of `/files` would need only `read`. The capability is
        # never inherited by a legacy key.
        try:
            assert_guest_access_allowed(request.path, api_key)
        except HTTPException as refusal:
            # A refused attempt to reach inside a guest is the record most worth having: a key
            # trying a path it was never granted is the signal, and it is invisible if only
            # successes are written.
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
        # important: an image export, an image secret and a custom volume backup hand a guest's
        # data out whole, which /files prices one file at a time (core/guest_data_export.py).
        # Priced on the path alone, like the guest-access gate above, so both run before the body
        # is read and a refused path is recorded whatever the body says.
        try:
            assert_guest_data_path_stays_inside(request.path, api_key)
        except HTTPException as refusal:
            _audit_raw_refusal(api_key, request, method, project, refusal, "capability_not_granted")
            raise
        # important: LXD reads body field names without regard to case, so every guard below (the
        # guest-data price, the migration fence, the policy observer, the limited-key rules) and the
        # request sent read this one dict, which refuses a field spelled any other way
        # (core/api_key_body.py). A refusal is recorded: no correct caller sends one.
        try:
            request = request.model_copy(update={"body": canonical_raw_proxy_body(method, request.body)})
        except HTTPException as refusal:
            _audit_raw_refusal(api_key, request, method, project, refusal, "field_name_not_canonical")
            raise
        # important: a migration source hands the guest's filesystem out whole too, which only its
        # body says (core/guest_data_export.py). Recorded when refused, like the gates above.
        try:
            assert_guest_data_stays_inside(request.path, method, request.body, api_key)
        except HTTPException as refusal:
            _audit_raw_refusal(api_key, request, method, project, refusal, "capability_not_granted")
            raise
        # important: cloud-init user-data runs as root in the guest on its next boot, so an instance
        # or profile write carrying it costs instance.exec for every key (core/cloud_init_config.py).
        # A create is refused here. A write to an existing object is judged by what it changes, after
        # the object is read below.
        try:
            cloud_init_read = assert_cloud_init_paid_for(request.path, method, request.body, api_key)
        except HTTPException as refusal:
            _audit_raw_refusal(api_key, request, method, project, refusal, "capability_not_granted")
            raise
        # important: a create whose source the daemon fetches itself (an image server, a URL, a
        # migration) would make the hypervisor reach any address and accept data the dashboard never
        # saw (core/key_pull_sources.py). Recorded when refused: no correct caller sends one.
        try:
            assert_key_pull_source_allowed(method, request.body)
        except HTTPException as refusal:
            _audit_raw_refusal(api_key, request, method, project, refusal, "pull_source_not_allowed")
            raise
        # important: LXD addresses an object by the name its create or rename body gave it, and several of its
        # name checks allow `.` and `..`, which no key door can address afterwards (core/lxd_body_names.py).
        assert_body_names_addressable(request.path, method, request.body)
        # important: the project check above binds the REQUEST to a granted project, which does
        # not bound a resource whose identity is itself a project name. A key granted only
        # "tenant-a" could send DELETE /1.0/projects/tenant-b with project=tenant-a and pass
        # every check here. Same for a cluster-wide storage pool.
        party_project = _raw_party_bounded_project(api_key, request, method)
        if party_project is not None:
            # A Partner's key on a cluster another party holds still creates, changes and deletes
            # projects inside its creator's bound, as the browser does (services/key_party_bound.py).
            refusal = await _party_bounded_project_refusal(
                api_key, request.cluster_id, party_project, creating=method == "POST"
            )
            if refusal is None and method == "POST":
                try:
                    # The /proxy create_project asks admin; the raw door asks the same for this.
                    assert_cluster_permission(api_key, request.cluster_id, "admin")
                    request.body = {
                        **request.body,
                        "config": shared_project_config(request.cluster_id, request.body.get("config") or {}),
                    }
                except HTTPException as policy_refusal:
                    refusal = policy_refusal
            if refusal is not None:
                _audit_raw_refusal(api_key, request, method, project, refusal, "outside_the_creators_bound")
                raise refusal
        else:
            assert_project_scope_allows_path(request.path, method, allowed_projects)
        assert_body_within_grant(request.path, method, request.body, allowed_projects)

        required_permission = "read"

        if method in ["POST", "PUT", "PATCH"]:
            required_permission = "write"
        elif method == "DELETE":
            required_permission = "admin"
        
        # Shared with the image and ISO upload routers. An empty grant is a 403, not a 500; see
        # app/core/api_key_permissions.py.
        assert_cluster_permission(api_key, request.cluster_id, required_permission)
        await _refuse_a_cluster_not_registered(request.cluster_id)
        # important: `default` on a cluster another party holds is view only for a key a partner
        # created, as it is in the browser and on /proxy. A `?project=` in the path cannot move
        # the call elsewhere: request_target replaces it with the authorised project below.
        try:
            await refuse_key_default_operation(
                api_key, request.cluster_id, _raw_proxy_default_reach(request, project)
            )
        except HTTPException as refusal:
            if refusal.status_code == 403:
                _audit_raw_refusal(api_key, request, method, project, refusal, "default_view_only")
            raise

        write_target = _raw_proxy_fenced_write_target(request)
        if write_target is not None:
            await _assert_proxy_write_is_not_fenced(
                cluster_id=request.cluster_id,
                project=project,
                instance_name=write_target,
            )
        fenced_project = _raw_proxy_fenced_project(request, project)
        if fenced_project is not None:
            await _assert_proxy_project_write_is_not_fenced(
                cluster_id=request.cluster_id,
                project=fenced_project,
            )
        if _raw_proxy_has_cluster_wide_write(request):
            await _assert_proxy_cluster_write_is_not_fenced(
                cluster_id=request.cluster_id,
            )

        # important: LXD applies profiles a write does not name (a create gets default, a copy its
        # source's, a move the target project's), so a project-limited key's write is judged by
        # what it applies before it is sent (core/limited_key_writes.py). The names it reads are held
        # to one path segment here, before the cluster is asked anything.
        plan = (
            [] if project_grant_is_unrestricted(allowed_projects)
            else limited_write_plan(request.path, method, request.body, project)
        )
        assert_plan_segments(plan)

        # important: the breaker check sits after the permission checks, so response timing
        # cannot reveal which clusters exist to a caller without permission.
        if health.is_tripped(request.cluster_id):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cluster {request.cluster_id} recently unreachable: "
                    f"{health.get_reason(request.cluster_id) or 'unknown'}"
                ),
            )

        # get_client blocks too. It revalidates the pooled connection with a live
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

        # The shared 503 names the breaker's recorded reason (an untrusted certificate, a failed
        # reconnect), which "not connected" alone would not tell an integrator.
        client = require_client(client, request.cluster_id)

        # important: `project` is authorised above from request.project, so a copy of it
        # inside `path` is not the caller's to set. Dropping it closes a scope bypass: a key
        # allowed only on "default" could otherwise declare project="default" (passing the
        # check, and skipping the override because it IS default) while sending
        # ?project=<other> in the path, which LXD honours because these certs are registered
        # unrestricted. Every other query key is legitimate passthrough and is left alone.
        # important: the URL is rebuilt from the same canonical_proxy_path every guard above
        # judged, every spelling of `project` (`%2570roject` included) replaced by the authorised
        # one and the query re-encoded with urlencode, so a decoded `&`, `#` or `=` stays data and
        # no fragment can drop the authorised project (core/raw_proxy_path.py).
        endpoint = client.api._api_endpoint
        full_url = f"{endpoint}{canonical_proxy_path(request.path).request_target(project)}"

        # important: what the key names is not always where LXD writes. A project that does not
        # isolate images, profiles, network ACLs or custom volumes keeps them in `default`, and an
        # operation belongs to the project of its resources whatever `?project=` says.
        isolation_targets = _raw_isolation_targets(request, project) if is_party_bounded(api_key, request.cluster_id) else []
        if isolation_targets:
            try:
                await _refuse_an_unisolated_target(client, request.cluster_id, isolation_targets)
            except HTTPException as refusal:
                _audit_raw_refusal(api_key, request, method, project, refusal, "project_not_isolated")
                raise

        landing = _raw_proxy_landing(request, project, client, endpoint)
        if landing is not None:
            try:
                await refuse_key_default_operation(api_key, request.cluster_id, [], lands_in=landing)
            except HTTPException as refusal:
                if refusal.status_code == 403:
                    _audit_raw_refusal(api_key, request, method, project, refusal, "default_view_only")
                raise

        # The raw path inspects no body at all, so this is the only place a policy-key write
        # through it is visible to anyone.
        _observe_policy_key_write(
            api_key,
            policy_keys_in_raw_body(request.path, request.body),
            cluster_id=request.cluster_id,
            project=project,
            target=request.path,
            external_user_email=request.external_user_email,
        )

        # important: this reaches into client.api.session, which bypasses pylxd's _APINode and
        # therefore the timeout _APINode would have injected. The socket timeout is passed
        # explicitly, and the call runs off the event loop: an LXD endpoint that accepts TCP
        # and then goes silent must degrade this one cluster, never the whole process.
        try:
            # important: only the object's own config says whether a cloud-init value changes, so it is
            # read as a job of its own and a read that fails sends nothing (services/lxd/cloud_init_reach.py).
            if cloud_init_read is not None:
                try:
                    await refuse_changed_cloud_init(
                        cloud_init_changes,
                        client.api.session,
                        endpoint,
                        cloud_init_read,
                        project,
                        request.body.get("config"),
                        socket_timeout_for(client),
                        api_key=api_key,
                        timeout=RAW_PROXY_TIMEOUT,
                        label=f"raw_proxy[cloud-init {request.cluster_id}]",
                        executor=proxy_executor,
                        max_in_flight=PROXY_MAX_IN_FLIGHT,
                    )
                except ChecksNotFinished as not_sent:
                    _audit_raw_refusal(api_key, request, method, project, not_sent, "cloud_init_not_compared")
                    return RawProxyResponse(status_code=not_sent.status_code, data=None, error=str(not_sent))
                except HTTPException as refusal:
                    _audit_raw_refusal(api_key, request, method, project, refusal, "capability_not_granted")
                    raise
            if plan:
                await refuse_what_a_limited_write_applies(
                    limited_write_reach,
                    client.api.session,
                    endpoint,
                    plan,
                    socket_timeout_for(client),
                    allowed_projects=allowed_projects,
                    timeout=RAW_PROXY_TIMEOUT,
                    label=f"raw_proxy[checks {request.cluster_id}]",
                    executor=proxy_executor,
                    max_in_flight=PROXY_MAX_IN_FLIGHT,
                )
            # important: LXD ignores ?project= on a single operation, so the project forced into
            # the URL above bounds nothing there. A limited key reaches an operation only when its
            # resources name a granted project; anything else reads as an id LXD does not hold
            # (services/lxd/raw_proxy_operations.py).
            operation_id = addressed_operation_id(canonical_proxy_path(request.path))
            if operation_id is not None and not project_grant_is_unrestricted(allowed_projects):
                try:
                    visible = await run_blocking(
                        operation_is_visible,
                        client.api.session,
                        endpoint,
                        operation_id,
                        frozenset(allowed_projects),
                        socket_timeout_for(client),
                        timeout=RAW_PROXY_TIMEOUT,
                        label=f"raw_proxy[operation owner {request.cluster_id}]",
                        executor=proxy_executor,
                        max_in_flight=PROXY_MAX_IN_FLIGHT,
                    )
                except OwnerUnknown as unknown:
                    _audit_raw_refusal(api_key, request, method, project, unknown, "operation_owner_unknown")
                    return RawProxyResponse(
                        status_code=502, data=None,
                        error="The cluster did not say whose operation this is, so nothing was sent",
                    )
                if not visible:
                    _audit_raw_refusal(
                        api_key, request, method, project, LookupError("Operation not found"),
                        "operation_not_visible",
                    )
                    return RawProxyResponse(status_code=404, data=dict(LXD_OPERATION_NOT_FOUND), headers={})
            # important: a project DELETE goes through the shared deletion core, which revokes the
            # access rules naming the project once it is gone. Sent straight to LXD it would leave
            # them behind, and a buyer whose project was removed would keep the cluster in scope.
            deleted_project = raw_project_delete_target(method, request.path)
            if deleted_project is not None:
                deleted = await _raw_project_delete(
                    lxd_service, client, request, api_key, project, deleted_project, full_url
                )
                if party_project and 200 <= deleted.status_code < 300:
                    # The name may be another party's next; no worker's cached bound may still list it.
                    forget_creator_bound_of(api_key, request.cluster_id)
                return deleted
            # An instance delete, rename or move, or a project rename, leaves the rules naming the
            # old name for whatever is created under it next (services/stranded_rules.py).
            forget_stranded = await stranded_rules.capture(
                request.cluster_id, raw_stranding_target(method, request.path, request.body, project)
            )
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

            status_code = response.status_code
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text if hasattr(response, 'text') else str(response)}
            # Read before redaction below, which may drop the operation's metadata.
            accepted_operation_id = lxd_operation_id(data) if status_code == 202 else None
            # important: an exec or console operation carries the secrets that open its websocket,
            # and a read key may list every operation in its projects. A write's answer is the
            # operation it started, which for a migration source holds its secrets too. An exec's
            # environment never leaves, and its command only to a key holding instance.exec.
            data = redact_operation_response(
                canonical_proxy_path(request.path),
                data,
                method=method,
                sees_command=has_explicit_capability(api_key, "instance.exec"),
            )
            # important: a pool's, network's, profile's or volume's used_by is cluster-wide, so a key
            # granted some projects would read every other project's instances through a plain GET
            # on a shared cluster. Held to the grant here, as on the structured proxy and MCP.
            if not project_grant_is_unrestricted(allowed_projects):
                data = used_by_in_projects(data, frozenset(allowed_projects))

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
                get_cache_manager().invalidate_after_external_write()
                if method == "POST" and party_project:
                    # The project joins the creator's bound, as a browser create does.
                    note = await record_key_created_project(api_key, request.cluster_id, party_project)
                    if note:
                        logger.warning("Raw project create on %s: %s", request.cluster_id, note.lstrip(". "))

                # important: a 202 has not happened yet. A dashboard read in the meantime still
                # sees the instance an OrcaHub delete is removing and caches it, so clear again
                # when LXD reports the operation settled.
                # Not for a call inside a guest (exec, files, console): its operation lives as
                # long as the session and changes no list, and it would hold a waiter.
                if status_code == 202 and not guest_access_capability_for(request.path):
                    invalidate_when_settled(
                        lambda: client,
                        accepted_operation_id,
                        project,
                        get_cache_manager().invalidate_after_external_write,
                        label=f"raw_proxy[{method} {request.cluster_id}]",
                        bucket=f"key:{api_key.get('id')}",
                        on_outcome=forget_stranded,
                    )
                elif status_code != 202 and forget_stranded is not None:
                    await forget_stranded("Success")

            return RawProxyResponse(
                status_code=status_code,
                data=data,
                headers={},
            )

        except BlockingTimeout as timeout_error:
            # important: a read that outlived the cap is a slow cluster, not an unreachable one.
            # The pooled session never retries a connect (Retry(connect=0)), so a dead host fails
            # fast as a connect error below and is still recorded. Tripping the breaker here
            # would turn one heavy list (a recursive network read on a host with a hundred
            # interfaces) into 60 s of 503 for every caller of the cluster, provisioning included.
            # A write still trips it: its outcome is unknown, so callers should back off.
            if method not in ("GET", "HEAD", "OPTIONS"):
                health.record_failure(request.cluster_id, str(timeout_error))
                # The write may still land on its worker thread; clear the lists when it ends.
                timeout_error.when_finished(get_cache_manager().invalidate_after_external_write)
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
        except ChecksNotFinished as not_sent:
            # Only reads ran, so the cluster is not marked unreachable for them.
            _audit_raw_proxy(api_key, request, method, project, "failure", error=not_sent.error_code)
            return RawProxyResponse(status_code=not_sent.status_code, data=None, error=str(not_sent))
        except HTTPException:
            # Re-raised so a real transport status is never downgraded to a 200 envelope by the
            # generic handler below.
            raise
        except Exception as api_error:
            # important: the breaker tracks reachability, so only a transport failure may trip
            # it. An LXD 4xx, a decode error or a bug in this handler must not mark the cluster
            # dead: connection.get_client consults the same breaker, so a wrongly tripped cluster
            # disappears from the dashboard's own read paths too.
            # A read timeout means TCP and TLS completed and LXD is still working: slow, not
            # unreachable, the distinction _validate_connection already makes. It answers 504 and
            # leaves the breaker alone. A connect failure or connect timeout still trips it.
            # important: not isinstance(ReadTimeout). The pooled session mounts Retry(read=0), so a
            # GET that outlives the read budget arrives as ConnectionError(MaxRetryError(
            # ReadTimeoutError)) and a stalled body as ConnectionError(ReadTimeoutError).
            if is_read_timeout(api_error):
                _audit_raw_proxy(api_key, request, method, project, "failure", error="timeout")
                logger.warning(f"Raw proxy read timeout: {method} {request.path} on {request.cluster_id}/{project}")
                return RawProxyResponse(status_code=504, data=None, error=str(api_error))
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


def _instance_rule_forgetter(
    cluster_id: str, project: Optional[str], instance_name: Any, rule_ids: FrozenSet[int]
) -> Optional[Callable[[str], Awaitable[None]]]:
    """Forget the captured rules naming a deleted instance, once its delete is known to have landed."""
    if not rule_ids or not isinstance(instance_name, str) or not instance_name:
        return None

    async def forget(outcome: str) -> None:
        if outcome != "Success":
            return
        await instance_grants.forget_deleted_instance(
            cluster_id=cluster_id,
            project_name=project or "default",
            instance_name=instance_name,
            only_rule_ids=rule_ids,
        )

    return forget


async def _raw_project_delete(
    lxd_service: LXDService,
    client: Any,
    request: RawProxyRequest,
    api_key: Dict[str, Any],
    project: str,
    name: str,
    full_url: str,
) -> RawProxyResponse:
    """A raw `DELETE /1.0/projects/<name>`, answered with LXD's own reply (services/raw_project_delete.py)."""
    try:
        answer = await delete_through_core(
            lxd_service,
            client,
            cluster_id=request.cluster_id,
            name=name,
            scope_project=project,
            requester=api_key.get("id"),
            full_url=full_url,
            body=request.body,
        )
    except project_deletion.ProjectDeleteUnknown:
        _audit_raw_proxy(api_key, request, "DELETE", project, "failure", error="timeout")
        get_cache_manager().invalidate_after_external_write()
        get_cache_manager().invalidate_project_caches()
        return RawProxyResponse(
            status_code=504, data=None, error=_still_running_message("delete_project", request.cluster_id)
        )
    except ClusterConfigurationBusy as busy:
        _audit_raw_proxy(api_key, request, "DELETE", project, "failure", error="cluster_busy")
        backlogged = isinstance(busy, project_deletion.ProjectDeletesBacklogged)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_deletes_backlogged" if backlogged else "cluster_busy",
                "message": (
                    f"Earlier project deletes on {request.cluster_id} are still settling. Retry shortly."
                    if backlogged
                    else f"Another configuration change on {request.cluster_id} is still running. Retry shortly."
                ),
            },
        ) from busy
    except MigrationFenceConflict as fenced:
        _audit_raw_proxy(api_key, request, "DELETE", project, "failure", error="migration_fence")
        raise HTTPException(
            status_code=409,
            detail=f"A migration holds project {name} on {request.cluster_id}; delete it once the migration ends.",
        ) from fenced
    except BlockingPoolFull as pool_error:
        _audit_raw_proxy(api_key, request, "DELETE", project, "failure", error="overloaded")
        return RawProxyResponse(status_code=503, data=None, error=str(pool_error))
    status_code = answer.status_code or 500
    health.clear(request.cluster_id)
    _audit_raw_proxy(
        api_key, request, "DELETE", project, "success" if status_code < 400 else "failure", lxd_status=status_code
    )
    logger.info(
        f"Raw proxy: DELETE project {name} on {request.cluster_id} "
        f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')} -> {status_code}"
    )
    return RawProxyResponse(status_code=status_code, data=answer.data, headers={})


def _still_running_message(operation: str, cluster_id: str) -> str:
    """What to say when the wait ended but the cluster may still be working.

    important: "timed out" alone is what makes an unattended caller retry, and a retried create
    collides with the instance the first attempt is still building. Every timeout path uses this.

    The budget and the advice both differ by operation. Exec has its own, shorter budget
    and no asynchronous form, so telling an exec caller to resend with `"wait": false` would send
    it to a flag that operation does not read.
    """
    if operation == "delete_project":
        return (
            f"delete_project did not finish within its budget and may still complete on {cluster_id}. "
            "Do not retry yet: call list_projects to see whether the project is gone."
        )
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
    # important: assigned before the try, because the outer exception handler reads them. Set
    # inside it, any non-HTTPException raised before the permission checks finish (a malformed
    # stored grant is enough) would become an UnboundLocalError: a 500 with no audit record.
    #
    # The owning organization is resolved once for every audit record this call may write, so the
    # record can be attributed to the tenant whose guest was reached.
    _guest_org_id: Optional[int] = None
    _guest_org_slug: Optional[str] = None

    try:
        cluster_permissions = {
            p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])
        }
        
        if request.cluster_id not in cluster_permissions:
            refuse_a_paused_grant(api_key, request.cluster_id)
            raise HTTPException(
                status_code=403,
                detail=f"No permission for cluster {request.cluster_id}"
            )
        
        perm = cluster_permissions[request.cluster_id]
        
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

        # important: hierarchical, not membership. `required in granted` would refuse every
        # operation to a grant of exactly ["admin"], because "read" is not in ["admin"].
        # tests/test_cluster_scope_ratchet.py holds this path and raw_proxy to the same check.
        assert_cluster_permission(api_key, request.cluster_id, required_permission)
        await _refuse_a_cluster_not_registered(request.cluster_id)
        # important: create_instance sends params to LXD as the body and create_storage_pool its
        # definition, and LXD reads their field names without regard to case, so every guard below
        # reads params held to one spelling (core/api_key_body.py). MCP tools arrive here too.
        # A refusal is recorded, like the guest-capability refusal below: no correct caller sends
        # one, and an exec whose params were refused here would otherwise leave no trace.
        try:
            assert_canonical_proxy_params(operation, request.params or {})
        except HTTPException as refusal:
            _audit_key_action(
                api_key,
                action=_guest_audit_action(operation),
                target_id=str((request.params or {}).get("name") or operation)[:512],
                cluster_id=request.cluster_id,
                project=request.project,
                result="failure",
                details={"operation": operation, "refused": "field_name_not_canonical"},
                external_user_email=request.external_user_email,
                error_message=str(refusal.detail),
            )
            raise
        if operation == "create_instance":
            # The first-class create sends params as the body, so it reads the raw door's rule.
            try:
                assert_key_create_params_allowed(request.params or {})
            except HTTPException as refusal:
                _audit_key_action(
                    api_key,
                    action=_guest_audit_action(operation),
                    target_id=str((request.params or {}).get("name") or operation)[:512],
                    cluster_id=request.cluster_id,
                    project=request.project,
                    result="failure",
                    details={"operation": operation, "refused": "pull_source_not_allowed"},
                    external_user_email=request.external_user_email,
                    error_message=str(refusal.detail),
                )
                raise

        # important: `default` on a cluster another party holds is view only for a key a partner
        # created, as it is in the browser: no change, and no session or file inside a guest.
        # MCP tools reach this door too (mcp/dispatch.py).
        if required_permission != "read" or operation in GUEST_ACCESS_OPERATIONS:
            # create_instance sends params to LXD as the body, so a copy whose source names
            # `default` reaches into it whatever request.project says.
            touched = (
                [] if operation in CLUSTER_WIDE_OPERATIONS
                else [request.project, *projects_named_in(request.params or {})]
            )
            try:
                await refuse_key_default_operation(api_key, request.cluster_id, touched)
            except HTTPException as refusal:
                if refusal.status_code == 403:
                    _audit_key_action(
                        api_key,
                        action=_guest_audit_action(operation),
                        target_id=str((request.params or {}).get("name") or operation)[:512],
                        cluster_id=request.cluster_id,
                        project=request.project,
                        result="failure",
                        details={"operation": operation, "refused": "default_view_only"},
                        external_user_email=request.external_user_email,
                        error_message=str(refusal.detail),
                    )
                raise
        # important: an operation that reaches inside a guest costs a capability of its own, on
        # top of lxd.proxy and the level. It is never inherited by a key issued before
        # capabilities existed and never seeded by an application type, so an operator has to
        # grant it explicitly. Same rule the raw proxy path guard applies to /exec.
        guest_capability = GUEST_ACCESS_OPERATIONS.get(operation)
        if guest_capability is None and operation in ("update_instance", "create_instance"):
            # important: `raw.lxc` with an lxc hook runs as root on the HOST, and a disk device
            # sourced from a host path mounts it into the guest, so both cost a guest-access
            # capability, not `write` alone.
            guest_capability = config_reaches_inside(
                (request.params or {}).get("config"), (request.params or {}).get("devices")
            )
        # important: cloud-init user-data runs as root in the guest on its next boot, so config
        # carrying it costs instance.exec for every key (core/cloud_init_config.py). When a host-path
        # disk already priced the write at instance.files, exec is asked for on top of it below. An
        # update from a key without exec owes it only for a value the instance does not already hold,
        # so the instance is read first and a read that fails sends nothing.
        try:
            cloud_init_capability = await proxy_cloud_init_owed(
                lxd_service, request.cluster_id, operation, request.params or {}, request.project, api_key,
                timeout=PROXY_OP_TIMEOUT, executor=proxy_executor, max_in_flight=PROXY_MAX_IN_FLIGHT,
            )
        except ChecksNotFinished as not_sent:
            _audit_key_action(
                api_key,
                action=_guest_audit_action(operation),
                target_id=str((request.params or {}).get("name") or operation)[:512],
                cluster_id=request.cluster_id,
                project=request.project,
                result="failure",
                details={
                    "operation": operation,
                    "cloud_init_keys": cloud_init_keys_in((request.params or {}).get("config")),
                    "refused": "cloud_init_not_compared",
                },
                external_user_email=request.external_user_email,
                error_message=str(not_sent),
            )
            return ProxyResponse(success=False, error_code=not_sent.error_code, error=str(not_sent))
        if guest_capability is None:
            guest_capability = cloud_init_capability

        # Observation only, outside the capability decision above: these keys reach sideways into
        # another product's ledger rather than inside the guest.
        if operation in ("update_instance", "create_instance"):
            _observe_policy_key_write(
                api_key,
                policy_keys_in_config((request.params or {}).get("config")),
                cluster_id=request.cluster_id,
                project=request.project,
                target=str((request.params or {}).get("name") or operation),
                external_user_email=request.external_user_email,
            )
        if guest_capability:
            from app.core.database import async_session_maker as _org_session
            from app.core.org_scope import resolve_cluster_org as _resolve_org

            try:
                async with _org_session() as _org_db:
                    _guest_org_id, _guest_org_slug = await _resolve_org(
                        request.cluster_id, _org_db
                    )
            except Exception:  # noqa: BLE001 - attribution must not fail the request
                logger.warning(
                    "Could not resolve the owning organization for %s", request.cluster_id
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
                    action=_guest_audit_action(operation),
                    target_id=str(request.params.get("name", "")),
                    cluster_id=request.cluster_id,
                    project=request.project,
                    result="failure",
                    details={
                        "operation": operation,
                        **_guest_audit_details(operation, request.params or {}),
                        "refused": "capability_not_granted",
                    },
                    external_user_email=request.external_user_email,
                    error_message=str(refusal.detail),
                    target_organization_id=_guest_org_id,
                    target_organization_slug=_guest_org_slug,
                )
                raise

            if guest_capability.startswith("desktop."):
                # important: AFTER the capability check, so a key that was never granted the
                # capability is refused for that reason rather than for a missing grant, and
                # BEFORE any work reaches the cluster.
                await _assert_desktop_allowed(api_key, request, operation, guest_capability)

        if cloud_init_capability not in (None, guest_capability):
            try:
                assert_explicit_capability(api_key, cloud_init_capability)
            except HTTPException as refusal:
                _audit_key_action(
                    api_key,
                    action=_guest_audit_action(operation),
                    target_id=str(request.params.get("name", "")),
                    cluster_id=request.cluster_id,
                    project=request.project,
                    result="failure",
                    details={
                        "operation": operation,
                        "cloud_init_keys": cloud_init_keys_in((request.params or {}).get("config")),
                        "refused": "capability_not_granted",
                    },
                    external_user_email=request.external_user_email,
                    error_message=str(refusal.detail),
                    target_organization_id=_guest_org_id,
                    target_organization_slug=_guest_org_slug,
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
            # A Partner's key on a cluster another party holds still creates and deletes
            # projects inside its creator's bound, as the browser does (services/key_party_bound.py).
            if operation in ("create_project", "delete_project") and is_party_bounded(api_key, request.cluster_id):
                refusal = await _party_bounded_project_refusal(
                    api_key, request.cluster_id, str((request.params or {}).get("name") or ""),
                    creating=operation == "create_project",
                )
                if refusal is not None:
                    raise refusal
                if operation == "create_project":
                    # The browser's project policy: restricted, isolated features, user.* only.
                    request.params = {
                        **(request.params or {}),
                        "config": shared_project_config(request.cluster_id, (request.params or {}).get("config") or {}),
                    }
            else:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"{operation} acts on a resource no project contains, so it needs a key "
                        f"granted all projects on {request.cluster_id}. This key is limited to "
                        f"{sorted(perm.get('projects', ['default']))}."
                    ),
                )
        # important: a Partner's key on a cluster another party holds writes an instance only into
        # a project that isolates itself, as in the browser.
        if operation in ("create_instance", "update_instance", "restore_snapshot") and is_party_bounded(
            api_key, request.cluster_id
        ):
            isolation_client = require_client(
                await run_blocking(
                    lxd_service.connection_manager.get_client,
                    request.cluster_id,
                    timeout=PROXY_CONNECT_TIMEOUT,
                    label=f"get_client[{request.cluster_id}]",
                    executor=proxy_executor,
                    max_in_flight=PROXY_MAX_IN_FLIGHT,
                ),
                request.cluster_id,
            )
            await _refuse_an_unisolated_target(
                isolation_client, request.cluster_id,
                [request.project, *projects_named_in(request.params or {})],
            )
        # important: a project-limited key is held to the raw proxy's body rule on the operations
        # that write an instance's config, devices or profiles (core/limited_key_writes.py).
        limited_write = (
            None if project_grant_is_unrestricted(allowed_projects)
            else proxy_instance_write(operation, request.params or {})
        )
        if limited_write is not None:
            assert_limited_body(limited_write[2], allowed_projects, instance=True)

        if operation in {
            "create_instance",
            "update_instance",
            "start_instance",
            "restart_instance",
            "delete_instance",
            "exec_instance",
            "put_file",
            "desktop_input",
            "desktop_clipboard",
            "create_snapshot",
            "restore_snapshot",
            "delete_snapshot",
        }:
            instance_name = (request.params or {}).get("name")
            if isinstance(instance_name, str) and instance_name:
                await _assert_proxy_write_is_not_fenced(
                    cluster_id=request.cluster_id,
                    project=request.project,
                    instance_name=instance_name,
                )

        fenced_project: Optional[str] = None
        if operation in {"create_network", "delete_network"}:
            fenced_project = request.project
        elif operation in {"create_project", "delete_project"}:
            target_project = (request.params or {}).get("name")
            if isinstance(target_project, str) and target_project:
                fenced_project = target_project
        if fenced_project is not None:
            await _assert_proxy_project_write_is_not_fenced(
                cluster_id=request.cluster_id,
                project=fenced_project,
            )
        if operation in {
            "create_network",
            "delete_network",
            "create_storage_pool",
            "delete_storage_pool",
        }:
            await _assert_proxy_cluster_write_is_not_fenced(
                cluster_id=request.cluster_id,
            )

        # important: the breaker check sits after the permission checks, so response timing
        # cannot reveal which clusters exist to a caller without permission.
        if health.is_tripped(request.cluster_id):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cluster {request.cluster_id} recently unreachable: "
                    f"{health.get_reason(request.cluster_id) or 'unknown'}"
                ),
            )

        # important: the profiles and sources the write applies are read as a job of their own,
        # so a write whose checks time out is never started (see raw_proxy).
        limited_plan = instance_write_plan(*limited_write, request.project) if limited_write else []
        assert_plan_segments(limited_plan)
        if limited_plan:
            try:
                await refuse_what_a_limited_write_applies(
                    proxy_write_reach,
                    lxd_service,
                    request.cluster_id,
                    limited_plan,
                    allowed_projects=allowed_projects,
                    timeout=PROXY_OP_TIMEOUT,
                    label=f"proxy_operation[checks {request.cluster_id}]",
                    executor=proxy_executor,
                    max_in_flight=PROXY_MAX_IN_FLIGHT,
                )
            except ChecksNotFinished as not_sent:
                return ProxyResponse(success=False, error_code=not_sent.error_code, error=str(not_sent))

        # Every branch below is a blocking pylxd call, and the waiting ones poll an LXD
        # operation to completion. Inline on the loop that is the same failure mode as the
        # raw proxy, with a longer tail.
        _operation_executor, _operation_in_flight = operation_pool(operation)
        from app.services.desktop.entry import serve_wait, wait_seconds

        _desktop_wait = wait_seconds(operation, request.params or {})
        # A rule naming the instance must not pass to the next instance created under its name
        # (services/instance_grants.py): captured before the delete, forgotten once it landed.
        deleted_instance = (request.params or {}).get("name") if operation == "delete_instance" else None
        instance_rules = (
            await instance_grants.rule_ids_naming_instance(request.cluster_id, request.project, deleted_instance)
            if isinstance(deleted_instance, str) and deleted_instance
            else frozenset()
        )
        try:
            if _desktop_wait is not None:
                # important: on the event loop, after every permission, consent and budget check
                # above, and still through the audit below. Handed to the pool it would hold one of
                # the desktop pool's workers for up to ten seconds doing nothing. Bounded per
                # worker inside serve_wait; a full share is the BlockingPoolFull answer below.
                result_data = await serve_wait(_desktop_wait)
            elif operation == "delete_project":
                # important: the dashboard route's delete, not a bare pylxd call: off the loop with
                # its own budget, under the cluster lock and the migration fence, and the access
                # rules naming the project go with it. Without that a same-name project another
                # organization creates later inherits the deleted project's bound.
                result_data = await _delete_project_for_proxy(lxd_service, request, api_key.get("id"))
                if is_party_bounded(api_key, request.cluster_id):
                    forget_creator_bound_of(api_key, request.cluster_id)
            else:
                result_data = await run_blocking(
                    execute_proxy_operation,
                    lxd_service,
                    request.cluster_id,
                    operation,
                    request.params or {},
                    request.project,
                    request.wait,
                    visible_projects=_frozen_grant(api_key, request.cluster_id),
                    # A guest-reaching call holds a worker for as long as it runs, so it gets its
                    # own budget rather than the operation one. A command that needs longer is a
                    # job.
                    #
                    # important: three tiers, chosen by `operation_pool` beside the vocabulary rather
                    # than by a two-way condition on GUEST_ACCESS_OPERATIONS here, which would put
                    # every desktop action on exec's workers. `_client_for` derives its socket
                    # window from the same selector, so the budget and the window cannot disagree.
                    timeout=operation_timeout(operation),
                    label=f"proxy_operation[{operation} {request.cluster_id}]",
                    executor=_operation_executor,
                    max_in_flight=_operation_in_flight,
                )
                health.clear(request.cluster_id)
                if operation == "create_project" and is_party_bounded(api_key, request.cluster_id):
                    note = await record_key_created_project(
                        api_key, request.cluster_id, str((request.params or {}).get("name") or "")
                    )
                    if note and isinstance(result_data, dict):
                        result_data = {**result_data, "note": note.lstrip(". ")}
            if operation in LIST_MUTATIONS:
                # The upstream product writes through this proxy rather than through the
                # dashboard's own routes. Without this an instance it just deleted would stay
                # listed, or one it created stay missing, for a whole TTL.
                get_cache_manager().invalidate_after_external_write()
                pending_operation = (
                    result_data.get("operation_id") if isinstance(result_data, dict) else None
                )
                forget_rules = _instance_rule_forgetter(
                    request.cluster_id, request.project, deleted_instance, instance_rules
                )
                if pending_operation:
                    # Accepted but not finished (wait=false or a long operation): clear again
                    # once LXD settles it.
                    invalidate_when_settled(
                        # Runs on the waiter's threads, never on the loop.
                        partial(lxd_service.connection_manager.get_client, request.cluster_id),
                        str(pending_operation),
                        request.project,
                        get_cache_manager().invalidate_after_external_write,
                        label=f"proxy_operation[{operation} {request.cluster_id}]",
                        bucket=f"key:{api_key.get('id')}",
                        on_outcome=forget_rules,
                    )
                elif forget_rules is not None:
                    await forget_rules("Success")
        except BlockingTimeout as timeout_error:
            # important: exec lands here, not in the generic handler below, so its audit record
            # and its "still running" message have to be written on this path too.
            # `exec_instance` is also not in PROXY_WAIT_OPERATIONS, so without this it would fall
            # through to record_failure and trip the process-wide breaker: one integration's
            # slow command would make the cluster read as offline for every other caller.
            if operation in GUEST_ACCESS_OPERATIONS:
                _audit_key_action(
                    api_key,
                    action=_guest_audit_action(operation),
                    target_id=str(request.params.get("name", "")),
                    cluster_id=request.cluster_id,
                    project=request.project,
                    result="failure",
                    details={
                        "operation": operation,
                        **_guest_audit_details(operation, request.params or {}),
                        "outcome": "timeout",
                    },
                    external_user_email=request.external_user_email,
                    error_message="timeout",
                    target_organization_id=_guest_org_id,
                    target_organization_slug=_guest_org_slug,
                )
                logger.warning(
                    "Guest access timeout: %s %s on %s after %ss",
                    operation, request.params.get("name"), request.cluster_id, EXEC_TIMEOUT,
                )
                return ProxyResponse(
                    success=False,
                    error_code="timeout",
                    error=_still_running_message(operation, request.cluster_id),
                )
            long_running = operation in PROXY_WAIT_OPERATIONS and request.wait
            # important: a long operation is not an unreachable cluster. Tripping the breaker
            # here would make one slow create return 503 for every other caller of that cluster
            # for a full TTL, as _gather_per_cluster also avoids on its timeout path. Only a read
            # that ran out of time counts as a symptom worth recording.
            if not long_running:
                health.record_failure(request.cluster_id, str(timeout_error))
            logger.warning(
                f"Proxy operation timeout: {operation} on {request.cluster_id} "
                f"after {PROXY_OP_TIMEOUT}s"
            )
            # A write that ran out of time may still have changed the cluster for an unattended
            # external system. Drop the cached reads it may have invalidated.
            if operation in LIST_MUTATIONS:
                get_cache_manager().invalidate_after_external_write()
                timeout_error.when_finished(get_cache_manager().invalidate_after_external_write)
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
        except project_deletion.ProjectDeleteUnknown:
            get_cache_manager().invalidate_after_external_write()
            get_cache_manager().invalidate_project_caches()
            return ProxyResponse(
                success=False,
                error_code="timeout",
                error=_still_running_message(operation, request.cluster_id),
            )
        except MigrationFenceConflict:
            return ProxyResponse(
                success=False,
                error_code="conflict",
                error=f"A migration holds this project on {request.cluster_id}; delete it once the migration ends.",
            )
        except ClusterConfigurationBusy:
            return ProxyResponse(
                success=False,
                error_code="conflict",
                error=f"Another configuration change on {request.cluster_id} is still running. Retry shortly.",
            )

        logger.info(
            f"Proxy operation: {operation} on {request.cluster_id}/{request.project} "
            f"by {request.external_user_email or 'unknown'} via {api_key.get('app_name', 'unknown')}"
        )

        # important: list_projects answers about the whole cluster, not about the project the
        # operation ran in, so authorising request.project does not bound it. A key granted
        # only `default` could otherwise read every tenant project name, and those names carry
        # customer slugs. The REST read applies the same narrowing.
        if operation == "list_projects" and isinstance(result_data, list):
            allowed = _granted_projects(api_key, request.cluster_id)
            if allowed is not None:
                result_data = [
                    row for row in result_data
                    if isinstance(row, dict) and row.get("name") in allowed
                ]

        if operation in GUEST_ACCESS_OPERATIONS and isinstance(result_data, dict):
            # important: keyed on the guest-access set, not on the name `exec_instance`, so a
            # successful desktop action leaves the record a tenant would look for too.
            #
            # The request is described, the result is not. Output is whatever the caller chose to
            # read out of the guest, so an audit index that stored it would become a copy of every
            # secret anyone printed. Sizes and the exit code carry the forensic value, and the same
            # rule records typed text as a length.
            _audit_key_action(
                api_key,
                action=_guest_audit_action(operation),
                target_id=str(request.params.get("name", "")),
                cluster_id=request.cluster_id,
                project=request.project,
                result="success",
                details={
                    "operation": operation,
                    **_guest_audit_details(operation, request.params or {}),
                    "exit_code": result_data.get("exit_code"),
                    "stdout_bytes": len(result_data.get("stdout") or ""),
                    "stderr_bytes": len(result_data.get("stderr") or ""),
                    "truncated": result_data.get("truncated"),
                },
                external_user_email=request.external_user_email,
                target_organization_id=_guest_org_id,
                target_organization_slug=_guest_org_slug,
            )

            # One session row per session, not one per action, so a tenant's session list shows
            # that an agent connected to a guest rather than every mouse move. `session_started`
            # is set by the desktop entry point on the call that established the session.
            if result_data.get("session_started") and operation.startswith("desktop_"):
                from app.services.session_audit import fire_and_forget_connect
                from app.services.session_models import SessionProtocol

                fire_and_forget_connect(
                    session_id=str(result_data.get("session_id") or ""),
                    instance_name=str(request.params.get("name", "")),
                    host=request.cluster_id,
                    project=request.project or "default",
                    protocol=SessionProtocol.MCP_DESKTOP.value,
                    # important: no fabricated person. The columns are nullable precisely so that
                    # an agent session is not given a human identity it does not have, which a
                    # reader could not tell from a real one afterwards.
                    user_id=None,
                    user_email=None,
                    user_role="integration",
                    target_organization_id=_guest_org_id,
                    target_org_slug=_guest_org_slug,
                )

        # An accepted-but-unfinished operation carries its id at the top level too, so a
        # caller does not have to know it is nested in data.
        operation_id = (
            result_data.get("operation_id") if isinstance(result_data, dict) else None
        )

        # Announce the lifecycle change to any add-on subscribed to it, after the operation and
        # before the response: the cluster has already changed, so the event describes something
        # true, and `emit_for_proxy_operation` never raises, so a notification problem cannot turn
        # a completed create into a failed one.
        from app.services.webhooks.emitter import emit_for_proxy_operation

        # delete_project announces from inside its own shielded task, and only for a project that
        # was deleted, so a delete of a name that was never there sends no event.
        if operation != "delete_project":
            await emit_for_proxy_operation(
                operation,
                cluster_id=request.cluster_id,
                project=request.project,
                params=request.params,
                result=result_data,
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
        if request.operation in LIST_MUTATIONS:
            # Whether the cluster changed before the failure is unknown, and a list that hides
            # a change is the worse mistake: clear it.
            get_cache_manager().invalidate_after_external_write()
        if request.operation in GUEST_ACCESS_OPERATIONS:
            # important: a call that outlived the budget still reached the guest. Auditing only
            # the ones that answered in time would leave the longest-running, which are the ones
            # most worth reviewing, out of the record entirely.
            _audit_key_action(
                api_key,
                action=_guest_audit_action(request.operation),
                target_id=str(request.params.get("name", "")),
                cluster_id=request.cluster_id,
                project=request.project,
                result="failure",
                details={
                    "operation": request.operation,
                    **_guest_audit_details(request.operation, request.params or {}),
                    "outcome": error_code,
                },
                external_user_email=request.external_user_email,
                error_message=error_code,
                target_organization_id=_guest_org_id,
                target_organization_slug=_guest_org_slug,
            )
        if request.operation in GUEST_ACCESS_OPERATIONS and error_code == "timeout":
            # important: a socket read can still expire before the budget on the pooled path, and
            # the underlying message is a urllib3 string naming the LXD host's address, which is
            # infrastructure an integration has no business learning from a timeout.
            return ProxyResponse(
                success=False,
                error_code=error_code,
                error=_still_running_message(request.operation, request.cluster_id),
            )
        # important: a socket read timeout reaches here rather than as BlockingTimeout, with the
        # same hazard: the cluster keeps working after the wait ends, so a create that timed out
        # this way can still leave a real instance behind. Same guidance, whichever timeout fired.
        if error_code == "timeout" and request.operation in PROXY_WAIT_OPERATIONS and request.wait:
            return ProxyResponse(
                success=False,
                error_code=error_code,
                error=_still_running_message(request.operation, request.cluster_id),
            )
        return ProxyResponse(
            success=False,
            error_code=error_code,
            error=_proxy_error_text(e, error_code, request.operation, request.cluster_id),
        )


async def _delete_project_for_proxy(
    lxd_service: LXDService, request: "ProxyRequest", api_key_id: Optional[str]
) -> Dict[str, Any]:
    """delete_project through the shared deletion service; the caller only waits on it.

    important: shielded (services/project_deletion.delete_for_integration). A disconnecting
    integration or a shutdown cancels this await, never the delete and its rule revoke.
    """
    params = request.params or {}
    assert_required_params("delete_project", params)
    assert_path_params("delete_project", params)
    name = str(params["name"])
    host = request.cluster_id
    task = project_deletion.spawn(
        project_deletion.delete_for_integration(
            lxd_service, host, name, requester=api_key_id, scope_project=request.project
        )
    )
    outcome = await asyncio.shield(task)
    return {"deleted": True, "name": name, "already_deleted": outcome.already_gone}


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
    # The reason is served because elevated capabilities differ: org.sync and tenant.provision
    # write access control, while others reach inside a guest.
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

    important: the keys are `display_name` and `type`. `save_cluster_metadata` never writes
    `name`, `server_type` or `status`, so reading those would render every cluster as its raw id
    and claim it is LXD.
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

    important: status is optional. If reachability cannot be established the clusters are still
    returned, with a null status, because this endpoint says what a credential may be scoped to.
    Failing the whole read would empty the picker and make an existing grant unrevocable.
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
            # Off the loop. This is one Vault round trip per cluster, and it only runs when
            # reachability is unavailable, which is when the server is already unwell.
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
                    # The availability layer uses the literal "unknown" when it has no endpoint.
                    # Passed through, that word would read as a fact about the cluster rather
                    # than a gap in the data.
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
    # The owner key as it stands now. Only the owner syncs an organization, updates its members
    # and policies and provisions for it, so an owner that stops working freezes all three.
    # Null for a caller who is not an Admin and holds no partner row in the organization.
    owner_key_name: Optional[str] = None
    owner_key_state: Optional[str] = Field(
        "unclaimed",
        description=(
            "active, expiring, expired, revoked, inactive, missing, unknown or unclaimed; null when "
            "the caller is not an administrator or one of the organization's partners"
        ),
    )
    owner_key_expires_at: Optional[str] = None
    owner_key_expiry_state: Optional[str] = None
    cluster_ids: List[str] = Field(default_factory=list)
    # Every key in the owner set, primary first; empty when the caller may not see the owner keys
    # (the same rule as the owner_key_* fields) or the organization has no owner.
    owner_keys: List["OwnerKeyEntry"] = Field(default_factory=list)


class OwnerKeyEntry(BaseModel):
    """One key in an organization's owner set."""
    api_key_id: str
    name: Optional[str] = None
    state: str
    expires_at: Optional[str] = None
    is_primary: bool = False
    added_at: Optional[str] = None
    added_via: Optional[str] = None


IntegrationOrgResponse.model_rebuild()


async def _owner_keys(key_ids: List[str]) -> Dict[str, Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Each owner key's metadata document, read once per key and off the event loop."""
    if not key_ids:
        return {}
    vault_client = get_vault_integrations_client()
    reads = await asyncio.gather(
        *(asyncio.to_thread(_read_key_metadata_blocking, vault_client, key_id) for key_id in key_ids)
    )
    return dict(zip(key_ids, reads))


def _owner_state(key_id: Optional[str], read: Tuple[Optional[Dict[str, Any]], Optional[str]]) -> str:
    if not key_id:
        return "unclaimed"
    metadata, failure = read
    return key_state(metadata, missing=failure == "missing", unknown=failure == "unknown")


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
            # Default: only externally synced orgs (non-local).
            stmt = select(Organization).where(
                and_(Organization.source != "local", Organization.is_active == True)
            )

        # Non-admins see only organizations they created or belong to.
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

        clusters: Dict[int, List[str]] = {}
        if orgs:
            bound = await db.execute(
                select(OrganizationCluster.organization_id, OrganizationCluster.cluster_id)
                .where(OrganizationCluster.organization_id.in_([org.id for org in orgs]))
            )
            for org_id, cluster_id in bound.all():
                if cluster_id:
                    clusters.setdefault(org_id, []).append(cluster_id)

        # important: the owner key's name, state and expiry only for an Admin or a current partner of
        # that key's home organization (keys_whose_home_the_user_partners). Membership lists the
        # organization for a tenant too, OrcaHub makes a buyer's founder a partner of the buyer, and
        # created_by is the key's minter on a buyer organization provisioning made. An organization
        # with no owner key has nothing to hide and says so to its own partners.
        running: Optional[set] = None
        homes: set = set()
        if highest != Role.ADMIN:
            from app.services.cluster_parties import RUNNING_ROLES

            partner_rows = await db.execute(
                select(OrganizationMember.organization_id).where(
                    OrganizationMember.user_email == user.email,
                    OrganizationMember.role.in_(RUNNING_ROLES),
                )
            )
            running = {org_id for (org_id,) in partner_rows.all()}
        owner_sets = await owners_of(db, [org.id for org in orgs])
        every_owner = {row.api_key_id for rows in owner_sets.values() for row in rows} | {
            org.api_key_id for org in orgs if org.api_key_id}
        if running is not None:
            homes = await keys_whose_home_the_user_partners(db, user.email, every_owner)

        # The owner keys' metadata only, never a secret, and read live: a cached copy from
        # before a revoke would report a frozen organization as healthy.
        owners = await _owner_keys(sorted(every_owner))

        response = []
        for org in orgs:
            read = owners.get(org.api_key_id, (None, None)) if org.api_key_id else (None, None)
            meta = read[0] or {}
            in_set = {row.api_key_id for row in owner_sets.get(org.id, [])} | ({org.api_key_id} if org.api_key_id else set())
            # A partner of the home organization of any key in the set sees them.
            shown = running is None or (bool(in_set & homes) if org.api_key_id else org.id in running)
            response.append(IntegrationOrgResponse(
                id=org.id,
                name=org.name,
                slug=org.slug,
                description=org.description,
                source=org.source,
                external_id=org.external_id,
                api_key_id=org.api_key_id,
                app_name=meta.get("app_name") or None,
                created_at=org.created_at.isoformat() if org.created_at else None,
                updated_at=org.updated_at.isoformat() if org.updated_at else None,
                owner_key_name=(meta.get("name") or None) if shown else None,
                owner_key_state=_owner_state(org.api_key_id, read) if shown else None,
                owner_key_expires_at=meta.get("expires_at") if shown else None,
                owner_key_expiry_state=expiry_state(meta.get("expires_at")) if meta and shown else None,
                cluster_ids=sorted(set(clusters.get(org.id, []))),
                owner_keys=[
                    OwnerKeyEntry(
                        api_key_id=row.api_key_id,
                        name=(owners.get(row.api_key_id, (None, None))[0] or {}).get("name") or None,
                        state=_owner_state(row.api_key_id, owners.get(row.api_key_id, (None, None))),
                        expires_at=(owners.get(row.api_key_id, (None, None))[0] or {}).get("expires_at"),
                        is_primary=row.is_primary,
                        added_at=row.added_at.isoformat() if row.added_at else None,
                        added_via=row.added_via,
                    )
                    for row in sorted(owner_sets.get(org.id, []), key=lambda r: (not r.is_primary,))
                ] if shown and org.api_key_id else [],
            ))

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing synced organizations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not list synced organizations")


@router.post(
    "/refresh-organizations",
    summary="Refresh organizations from external app",
    description="Pull fresh organization data from connected external app (on-demand). Requires partner role.",
    dependencies=[Depends(require_partner())],
)
async def refresh_organizations(
    api_key_id: str = Query(..., description="API key ID of the external app to refresh from"),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_partner()),
):
    """On-demand pull of organizations from external app via stored callback URL."""
    import json

    from app.core.safe_http import SafeHTTPError
    from app.services.orcahub_callbacks import get_from_app

    # important: `api_key_id` comes from a query parameter, so it is scoped through
    # _authorize_api_key like every other single-key handler. Otherwise any partner could name any
    # key in any organisation and make this server issue an authenticated request to that key's
    # stored callback, then have the response upserted as organizations. Same overlap rule, same
    # side rule, same 404-for-out-of-scope shape, so an id cannot be probed apart from a
    # nonexistent one.
    metadata = await _authorize_api_key(api_key_id, user, db)
    vault_client = get_vault_integrations_client()
    # important: after the scope check, so it never confirms an id the caller cannot see, and
    # before the secret is read. Refreshing such a key would send its stored credential hash to
    # its callback URL and write the answer under the dead key's ownership. The OrcaHub callbacks
    # skip it the same way (orcahub_sync._get_orcahub_credentials).
    if classify_key(metadata) != "active":
        raise HTTPException(
            status_code=409,
            detail="This API key is revoked, inactive or expired, so it cannot be refreshed",
        )
    # important: a refresh claims and renames organizations as this key, the writes /sync gates on
    # org.sync, so the same key that /sync would refuse is refused here, before its secret is read or
    # its callback called. An add-on credential is never an OrcaHub connection, whatever it holds.
    if is_add_on_credential(metadata):
        raise HTTPException(status_code=409, detail={
            "error": "key_is_add_on",
            "message": "This API key is an add-on credential, so it cannot refresh organizations.",
        })
    missing = sync_capabilities_missing(metadata)
    if missing:
        raise HTTPException(status_code=409, detail={
            "error": "key_cannot_sync",
            "message": f"This API key does not hold {' and '.join(missing)}, so it cannot refresh organizations.",
        })

    # Retrieve API key secret for authenticating the callback
    try:
        secret_data = await asyncio.to_thread(
            vault_client.vault_read, f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/secret"
        )
        secret_hash = secret_data.get("hash", "")
    except Exception:
        raise HTTPException(status_code=500, detail="Could not read API key credentials")

    # important: only a URL the key's current secret stored. One stored with a secret a rotation has
    # replaced would receive this secret's hash. See services/integration_callback_url.py.
    app_url = bound_callback_url(metadata, secret_hash)
    if not app_url:
        raise HTTPException(
            status_code=400,
            detail=(
                "No callback URL is set for this API key's current secret. Trigger a sync from the "
                "external app first."
            ),
        )

    callback_url = f"{app_url.rstrip('/')}/api/external/organizations"
    try:
        body = await get_from_app(
            callback_url,
            headers={
                "X-API-Key-ID": api_key_id,
                "X-API-Key-Secret-Hash": secret_hash,
            },
        )
        data = json.loads(body)
    except SafeHTTPError as e:
        # important: status or category only. Logging the body would put the fetched content into
        # the log, turning a blind SSRF into a returned one for anyone who can read logs.
        if e.status_code is not None:
            logger.error("External app returned %s for the configured callback", e.status_code)
            raise HTTPException(status_code=502, detail=f"External app returned {e.status_code}")
        logger.error("Could not reach the external app for the configured callback: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach external app")
    except ValueError:
        logger.error("External app answered the configured callback with something other than JSON")
        raise HTTPException(status_code=502, detail="External app returned an unreadable answer")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="External app returned an unreadable answer")

    orgs_data = data.get("organizations", [])
    if not orgs_data:
        return {"message": "No organizations returned from external app", "organizations_synced": 0}

    sync_orgs = [SyncOrganization(**o) for o in orgs_data]
    synced = await _upsert_synced_organizations(db, sync_orgs, api_key_id)

    return {
        "message": f"Refreshed {synced} organizations from external app",
        "organizations_synced": synced,
    }


class OwnerKeyTransferRequest(BaseModel):
    """The key that becomes an organization's owner, and why. There is no release to no owner."""

    api_key_id: str = Field(
        ..., pattern=r"^[A-Za-z0-9_-]{1,128}$", description="The integration key that becomes the owner"
    )
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] = Field(
        ..., description="Why the owner changes. Kept in the audit record."
    )


class OwnerKeyTransferResponse(BaseModel):
    organization_id: int
    organization_slug: str
    external_id: Optional[str] = None
    previous_api_key_id: Optional[str] = None
    previous_key_state: str
    api_key_id: str
    api_key_name: Optional[str] = None
    app_name: Optional[str] = None
    changed: bool
    # Clusters bound to the organization that the new owner's grant does not name. Its sync still
    # works there, but provisioning tenant access on one of them is refused until the grant does.
    uncovered_cluster_ids: List[str] = Field(default_factory=list)
    # How many organizations the new owner claims after this. Anything but one stops its pushes
    # rebinding the creator's personal clusters (services/integration_cluster_rebind.py).
    claimed_organization_count: int = 0
    # The co-owners a transfer took out of the owner set besides the previous primary: a transfer
    # replaces the whole set with the new key.
    removed_owner_key_ids: List[str] = Field(default_factory=list)


def _audit_owner_transfer(
    org: Organization,
    *,
    details: Dict[str, Any],
    result: str = "success",
    error_code: Optional[str] = None,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an owner transfer or its refusal.

    The key ids travel as from_key_id and to_key_id. The audit sanitiser blanks any detail
    whose name contains api_key, except exactly api_key_id.
    """
    try:
        activity_logger.log_activity(
            action=OWNER_TRANSFER_ACTION,
            target_type="organization",
            target_id=str(org.id),
            target_organization_id=org.id,
            target_organization_slug=org.slug,
            result=result,
            error_code=error_code,
            details=details,
            before_state=before,
            after_state=after,
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the request
        logger.warning("Could not write the audit record for %s", OWNER_TRANSFER_ACTION, exc_info=True)


@router.put(
    "/organizations/{org_id}/owner-key",
    response_model=OwnerKeyTransferResponse,
    summary="Move a synced organization to another owner key",
    description=(
        "Admin only. Make another active OrcaHub key the owner of a synced organization, so that key "
        "can sync it, update its members and policies, and provision tenant access for it. The old "
        "owner loses all of that. There is no release to no owner."
    ),
)
async def transfer_organization_owner(
    org_id: int,
    body: OwnerKeyTransferRequest,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    """Move one organization to another owner key.

    An owner key that is revoked, deleted or left to expire freezes its organizations, because a
    key may sync only what it owns or what no key has claimed. This hands them to a working key.
    See services/integration_key_ownership.py.
    """
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    target = body.api_key_id
    context = {
        "organization_slug": org.slug,
        "external_id": org.external_id,
        "from_key_id": org.api_key_id,
        "to_key_id": target,
        "reason": body.reason,
        "actor_email": _rbac.email,
        "trigger": "admin",
    }
    if org.source != "orcahub":
        _audit_owner_transfer(org, details=context, result="denied", error_code="organization_not_synced")
        raise HTTPException(
            status_code=409,
            detail={
                "error": "organization_not_synced",
                "message": (
                    f"Organization '{org.slug}' was created in Orcastra, not synced from OrcaHub, "
                    "so no integration key owns it."
                ),
            },
        )

    vault_client = get_vault_integrations_client()
    previous: Optional[str] = org.api_key_id
    removed: List[str] = []
    async with serialize_key_ownership(db, target), serialize_org_owners(db, [org.id]):
        # important: the new owner is read and judged inside the lock, so a revoke of that key
        # cannot land between the check and the write and leave the organization on a dead key.
        metadata, failure = await asyncio.to_thread(_read_key_metadata_blocking, vault_client, target)
        owners_before = await owner_key_ids(db, org.id)
        # A transfer replaces the whole owner set, so one to the current primary still changes an
        # organization that has co-owners.
        changed = previous != target or any(key != target for key in owners_before)
        if changed:
            refused = owner_key_refusal(metadata, failure)
            if refused is not None:
                status, code, message = refused
                _audit_owner_transfer(org, details=context, result="denied", error_code=code)
                # A 503 detail is sanitised to a string by the app's error handler.
                raise HTTPException(
                    status_code=status,
                    detail=message if status >= 500 else {"error": code, "message": message},
                )
            moved, previous = await transfer_owner(db, org_id, target)
            if moved is None:
                raise HTTPException(status_code=404, detail="Organization not found")
            org = moved
            # The co-owners only; the previous primary is reported as previous_api_key_id.
            removed = [key for key in owners_before if key not in (target, previous)]
            await db.commit()

    uncovered = await uncovered_clusters(db, org.id, metadata)
    claimed = await claimed_organization_count(db, target)
    previous_state = "unclaimed"
    if previous == target:
        previous_state = key_state(metadata, missing=failure == "missing", unknown=failure == "unknown")
    elif previous:
        previous_state = _owner_state(
            previous, await asyncio.to_thread(_read_key_metadata_blocking, vault_client, previous)
        )
    answer = OwnerKeyTransferResponse(
        organization_id=org.id,
        organization_slug=org.slug,
        external_id=org.external_id,
        previous_api_key_id=previous,
        previous_key_state=previous_state,
        api_key_id=target,
        api_key_name=(metadata or {}).get("name"),
        app_name=(metadata or {}).get("app_name"),
        changed=changed,
        uncovered_cluster_ids=uncovered,
        claimed_organization_count=claimed,
        removed_owner_key_ids=removed,
    )
    if not changed:
        return answer

    # Here rather than in a helper, so the cache ratchet sees the handler clear the key list,
    # which carries what each key owns.
    get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
    for key_id in dict.fromkeys([previous, target, *removed]):
        if key_id:
            evict_api_key_cache(key_id)
    _audit_owner_transfer(
        org,
        details={
            **context,
            "from_key_id": previous,
            "from_key_state": previous_state,
            "to_key_name": answer.api_key_name,
            "to_key_app_name": answer.app_name,
            "uncovered_cluster_ids": uncovered,
            "removed_owner_key_ids": removed,
        },
        before={"owner_key_id": previous, "owner_key_ids": ([previous] if previous else []) + removed},
        after={"owner_key_id": target, "owner_key_ids": [target]},
    )
    logger.info(
        "Organization %s moved from api key %s to %s by %s", org.slug, previous or "<none>", target, _rbac.email
    )
    return answer


class OwnerKeyChangeRequest(BaseModel):
    """A key to put in, or make the primary of, an organization's owner set, and why."""

    api_key_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,128}$", description="The integration key")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] = Field(
        ..., description="Why the owner set changes. Kept in the audit record."
    )


class OwnerSetResponse(BaseModel):
    organization_id: int
    organization_slug: str
    external_id: Optional[str] = None
    primary_key_id: Optional[str] = None
    owner_key_ids: List[str] = Field(default_factory=list)
    changed: bool


def _require_multi_owner() -> None:
    """404 while INTEGRATION_MULTI_OWNER_ENABLED is off, as if the route did not exist.

    important: a route dependency (dependencies=[...]), which FastAPI resolves before the route's own
    parameters and its body. On the external router that is before the key is checked, so a key that is
    wrong or missing still reads 404 while the flag is off. The internal router's own dependencies (the
    login and the cluster scope) run first, so there an unauthenticated call answers 401 either way.
    """
    if not multi_owner_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


_MULTI_OWNER_ONLY = [Depends(_require_multi_owner)]

_Body = TypeVar("_Body", bound=BaseModel)


async def _json_body(request: Request, model: Type[_Body]) -> _Body:
    """A route's JSON body, read and validated by the route rather than by FastAPI.

    important: FastAPI parses a declared body before it resolves any dependency, so malformed JSON
    would answer 422 ahead of the flag's 404 and the login. Read here, the flag and the caller come first.
    The 422 never carries the input: FastAPI's own answer echoes each failing value.
    """
    return await read_json_body(request, model)


def _body_spec(model: Type[BaseModel]) -> Dict[str, Any]:
    """openapi_extra for a route that reads its body with _json_body, so the published spec still shows it."""
    return body_spec(model)


async def _owner_change_body(request: Request) -> OwnerKeyChangeRequest:
    return await _json_body(request, OwnerKeyChangeRequest)


async def _owned_org_or_refuse(db: AsyncSession, org_id: int) -> Organization:
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.source != "orcahub":
        raise HTTPException(status_code=409, detail={
            "error": "organization_not_synced",
            "message": f"Organization '{org.slug}' was created in Orcastra, not synced from OrcaHub, so no integration key owns it.",
        })
    if not org.api_key_id:
        raise HTTPException(status_code=409, detail={
            "error": "organization_unclaimed",
            "message": f"Organization '{org.slug}' has no owner yet. Give it its first owner with the owner key transfer.",
        })
    return org


def _owner_set_answer(org: Organization, owners: List[str], changed: bool) -> OwnerSetResponse:
    return OwnerSetResponse(
        organization_id=org.id, organization_slug=org.slug, external_id=org.external_id,
        primary_key_id=org.api_key_id, owner_key_ids=owners, changed=changed,
    )


def _evict_owner_keys(*key_ids: Optional[str]) -> None:
    for key_id in dict.fromkeys(key_ids):
        if key_id:
            evict_api_key_cache(key_id)


@router.post(
    "/organizations/{org_id}/owner-keys",
    response_model=OwnerSetResponse,
    dependencies=_MULTI_OWNER_ONLY,
    summary="Add a key to a synced organization's owner set",
    openapi_extra=_body_spec(OwnerKeyChangeRequest),
    description=(
        "Admin only, when INTEGRATION_MULTI_OWNER_ENABLED is on. The key may then sync the organization, "
        "update its members and policies and provision tenant access for it, alongside its other owners."
    ),
)
async def add_organization_owner(
    org_id: int,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
    body: OwnerKeyChangeRequest = Depends(_owner_change_body),
    db: AsyncSession = Depends(get_db),
):
    org = await _owned_org_or_refuse(db, org_id)
    vault_client = get_vault_integrations_client()
    async with serialize_key_ownership(db, body.api_key_id), serialize_org_owners(db, [org.id]):
        # Again under the lock, which read the organization afresh.
        org = await _owned_org_or_refuse(db, org_id)
        metadata, failure = await asyncio.to_thread(_read_key_metadata_blocking, vault_client, body.api_key_id)
        refused = owner_key_refusal(metadata, failure)
        if refused is not None:
            status, code, message = refused
            raise HTTPException(status_code=status, detail=message if status >= 500 else {"error": code, "message": message})
        before = await owner_key_ids(db, org.id)
        try:
            added = await add_owner(db, org, body.api_key_id, via="admin", by=_rbac.email)
        except OwnerSetFull:
            raise HTTPException(status_code=409, detail={
                "error": "owner_set_full",
                "message": f"Organization '{org.slug}' already has {MAX_OWNER_KEYS} owner keys. Remove one first.",
            }) from None
        if added:
            await db.commit()
        after = await owner_key_ids(db, org.id)
    if added:
        # The key list carries what each key owns (cache ratchet: cleared in the handler).
        get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
        _evict_owner_keys(body.api_key_id)
        record_owner_change(ADD_ACTION, org, before=before, after=after, primary_before=org.api_key_id,
                            primary_after=org.api_key_id, details={
                                "owner_key_id": body.api_key_id, "reason": body.reason,
                                "actor_email": _rbac.email, "trigger": "admin",
                            })
    return _owner_set_answer(org, after, added)


@router.delete(
    "/organizations/{org_id}/owner-keys/{key_id}",
    response_model=OwnerSetResponse,
    dependencies=_MULTI_OWNER_ONLY,
    summary="Remove a key from a synced organization's owner set",
    description=(
        "Admin only, when INTEGRATION_MULTI_OWNER_ENABLED is on. Removing the primary makes the oldest "
        "owner that can still sync the organization the primary. The last owner cannot be removed: "
        "transfer the organization instead."
    ),
)
async def remove_organization_owner(
    org_id: int,
    key_id: str,
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] = Query(
        ..., description="Why the key leaves. Kept in the audit record."
    ),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    org = await _owned_org_or_refuse(db, org_id)
    vault_client = get_vault_integrations_client()
    async with serialize_key_ownership(db, key_id), serialize_org_owners(db, [org.id]):
        # Again under the lock, which read the organization afresh.
        org = await _owned_org_or_refuse(db, org_id)
        before = await owner_key_ids(db, org.id)
        if key_id not in before:
            return _owner_set_answer(org, before, False)
        primary_before = org.api_key_id
        verdicts = await able_owners([k for k in before if k != key_id], _owner_metadata_reader(vault_client))
        try:
            await remove_owner(db, org, key_id, able=verdicts.get)
        except LastOwner:
            raise HTTPException(status_code=409, detail={
                "error": "last_owner",
                "message": "This is the organization's only owner key. Transfer the organization to another key instead.",
            }) from None
        except NoAbleOwner:
            raise HTTPException(status_code=409, detail={
                "error": "no_able_owner",
                "message": "No other owner key can sync the organization, so none can become its primary. Add an active owner key first.",
            }) from None
        except LookupError:
            raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
        await db.commit()
        after = await owner_key_ids(db, org.id)
    get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
    _evict_owner_keys(key_id, org.api_key_id)
    record_owner_change(REMOVE_ACTION, org, before=before, after=after, primary_before=primary_before,
                        primary_after=org.api_key_id, details={
                            "owner_key_id": key_id, "reason": reason, "actor_email": _rbac.email, "trigger": "admin",
                        })
    return _owner_set_answer(org, after, True)


@router.put(
    "/organizations/{org_id}/primary-key",
    response_model=OwnerSetResponse,
    dependencies=_MULTI_OWNER_ONLY,
    summary="Choose which owner key is a synced organization's primary",
    openapi_extra=_body_spec(OwnerKeyChangeRequest),
    description=(
        "Admin only, when INTEGRATION_MULTI_OWNER_ENABLED is on. The primary is the owner key OrcaHub "
        "callbacks are made with. It must already be in the owner set and able to sync."
    ),
)
async def set_organization_primary(
    org_id: int,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
    body: OwnerKeyChangeRequest = Depends(_owner_change_body),
    db: AsyncSession = Depends(get_db),
):
    org = await _owned_org_or_refuse(db, org_id)
    vault_client = get_vault_integrations_client()
    async with serialize_key_ownership(db, body.api_key_id), serialize_org_owners(db, [org.id]):
        # Again under the lock, which read the organization afresh.
        org = await _owned_org_or_refuse(db, org_id)
        owners = await owner_key_ids(db, org.id)
        if body.api_key_id not in owners:
            raise HTTPException(status_code=409, detail={
                "error": "key_not_an_owner",
                "message": "This key is not one of the organization's owner keys. Add it to the owner set first.",
            })
        if org.api_key_id == body.api_key_id:
            return _owner_set_answer(org, owners, False)
        metadata, failure = await asyncio.to_thread(_read_key_metadata_blocking, vault_client, body.api_key_id)
        refused = owner_key_refusal(metadata, failure)
        if refused is not None:
            status, code, message = refused
            raise HTTPException(status_code=status, detail=message if status >= 500 else {"error": code, "message": message})
        previous = set_primary(org, body.api_key_id)
        await db.commit()
    get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
    _evict_owner_keys(previous, body.api_key_id)
    record_owner_change(PRIMARY_ACTION, org, before=owners, after=owners, primary_before=previous,
                        primary_after=org.api_key_id, details={
                            "owner_key_id": body.api_key_id, "reason": body.reason,
                            "actor_email": _rbac.email, "trigger": "admin",
                        })
    return _owner_set_answer(org, owners, True)


class OwnerKeysResponse(BaseModel):
    """Whether the calling key owns an organization, and, only for an owner, the whole set."""

    status: str = Field(..., description="owner, not_owner or unclaimed")
    primary_key_id: Optional[str] = None
    owner_key_ids: Optional[List[str]] = None


class OwnerKeyEnrollRequest(BaseModel):
    organization_external_id: str = Field(..., min_length=1, max_length=255)
    new_key_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,128}$", description="The key that joins the owner set")
    # SecretStr, so a repr or log of the request never shows it. The route parses the body itself
    # (_enroll_body) because FastAPI's own 422 would echo it.
    new_key_secret: SecretStr = Field(..., min_length=1, max_length=512, description="Its secret, proving the caller holds it")


async def _enroll_body(request: Request) -> OwnerKeyEnrollRequest:
    """The enrollment body (_json_body).

    important: its 422 never carries the input, and SecretStr alone does not stop FastAPI's own answer
    echoing it, so a secret one character too long would come back in the response and any proxy log.
    """
    return await _json_body(request, OwnerKeyEnrollRequest)


def _read_key_secret_hash_blocking(vault_client, key_id: str) -> Tuple[Optional[str], Optional[str]]:
    """A key's stored secret hash, or None and why: "missing" when Vault has no such key, else "unknown".

    As _read_key_metadata_blocking, so an unreadable store is told apart from a missing key and is
    never counted as a failed guess.
    """
    from app.core.vault_client import VaultClientError

    try:
        secret = vault_client.vault_read(f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret")
    except (VaultClientError, HTTPException) as exc:
        return None, "missing" if getattr(exc, "status_code", None) == 404 else "unknown"
    except Exception:  # noqa: BLE001 - an unreadable store is reported, never raised
        return None, "unknown"
    stored = secret.get("hash") if isinstance(secret, dict) else None
    return (stored, None) if stored else (None, "missing")


class OwnerKeyEnrollResponse(BaseModel):
    organization_external_id: Optional[str] = None
    primary_key_id: Optional[str] = None
    owner_key_ids: List[str] = Field(default_factory=list)
    added: bool


class OwnerKeySelfRemoveResponse(BaseModel):
    removed: bool
    primary_key_id: Optional[str] = None
    owner_key_ids: Optional[List[str]] = None


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


async def _external_org(db: AsyncSession, external_id: str) -> Optional[Organization]:
    return (await db.execute(select(Organization).where(
        Organization.source == "orcahub", Organization.external_id == external_id,
    ))).scalar_one_or_none()


@external_router.get(
    "/external/organizations/{external_id}/owner-keys",
    response_model=OwnerKeysResponse,
    dependencies=_MULTI_OWNER_ONLY,
    summary="Whether this key owns an organization",
    description=(
        "Answers owner, not_owner or unclaimed for the calling key. Only an owner is told the primary "
        "and every owner key. Writes nothing. 404 unless INTEGRATION_MULTI_OWNER_ENABLED is on."
    ),
)
async def read_organization_owner_keys(
    external_id: str,
    api_key: Dict[str, Any] = Depends(require_capability("inventory.read")),
    db: AsyncSession = Depends(get_db),
):
    key_id = api_key.get("id", "")
    org = await _external_org(db, external_id)
    # A missing organization answers unclaimed, as it is to a sync: the first push claims it.
    # So this says no more about which ids exist than the sync probe already does.
    if org is None or not org.api_key_id:
        return OwnerKeysResponse(status="unclaimed")
    owners = await owner_key_ids(db, org.id)
    if key_id not in owners:
        return OwnerKeysResponse(status="not_owner")
    return OwnerKeysResponse(status="owner", primary_key_id=org.api_key_id, owner_key_ids=owners)


@external_router.post(
    "/external/organizations/owner-keys",
    response_model=OwnerKeyEnrollResponse,
    dependencies=_MULTI_OWNER_ONLY,
    summary="Add a key the caller also holds to an organization's owner set",
    description=(
        "Authenticated as an owner key of the organization (org.sync). The body names a second key and "
        "its secret; when the secret verifies and the key could own the organization, it joins the owner "
        "set. Idempotent. 404 unless INTEGRATION_MULTI_OWNER_ENABLED is on."
    ),
    openapi_extra=_body_spec(OwnerKeyEnrollRequest),
)
async def enroll_owner_key(
    request: Request,
    api_key: Dict[str, Any] = Depends(require_capability("org.sync")),
    db: AsyncSession = Depends(get_db),
):
    """Two-key enrollment (services/owner_key_enrollment.py).

    important: the new key's secret is in the body. It is checked against the stored hash and dropped;
    it is never logged, audited, stored or echoed, and a missing key and a wrong secret get the same
    answer.

    important: the order is the guard. Nothing about the new key is read until the calling key is found
    in the organization's owner set, under the key and organization locks (which read the organization
    afresh). A key that does not own it, a missing organization and an unclaimed one get one answer, and
    spend no organization budget and no lockout count.
    """
    body = await _enroll_body(request)
    vouching = api_key.get("id", "")
    new_key = body.new_key_id
    if new_key == vouching:
        raise _refuse(422, "same_key", "A key cannot vouch for itself. Name the other key to add.")
    if not owner_key_enrollment.within_key_limits(vouching):
        raise _refuse(429, "rate_limited", "Too many owner key enrollments. Try again later.")
    not_an_owner = _refuse(403, "not_an_owner", owner_key_enrollment.NOT_AN_OWNER)
    vault_client = get_vault_integrations_client()

    first, second = sorted([vouching, new_key])
    async with serialize_key_ownership(db, first), serialize_key_ownership(db, second):
        org = await _external_org(db, body.organization_external_id)
        if not owner_key_enrollment.is_external_org(org):
            raise not_an_owner
        async with serialize_org_owners(db, [org.id]):
            if not org.api_key_id or not await is_owner(db, org, vouching):
                raise not_an_owner
            if not owner_key_enrollment.within_org_limits(body.organization_external_id):
                raise _refuse(429, "rate_limited", "Too many owner key enrollments. Try again later.")
            vouching_metadata, vouching_failure = await asyncio.to_thread(_read_key_metadata_blocking, vault_client, vouching)
            # important: re-read here, uncached. The key that vouches must still be able to own the
            # organization (and an add-on credential never vouches, owner_key_refusal names it).
            refused = owner_key_refusal(vouching_metadata, vouching_failure)
            if refused is not None:
                if refused[0] >= 500:
                    raise HTTPException(status_code=503, detail=owner_key_enrollment.UNREADABLE)
                raise _refuse(403, "not_an_owner", "The calling key cannot own this organization, so it cannot add an owner key.")
            if owner_key_enrollment.locked_out(vouching, new_key):
                logger.warning("Owner key enrollment refused: api key %s is locked out of vouching for %s "
                               "(organization %s)", vouching, new_key, org.slug)
                raise _refuse(403, "new_key_unverified", owner_key_enrollment.UNVERIFIED)
            new_metadata, new_failure = await asyncio.to_thread(_read_key_metadata_blocking, vault_client, new_key)
            stored_hash, hash_failure = await asyncio.to_thread(_read_key_secret_hash_blocking, vault_client, new_key)
            if "unknown" in (new_failure, hash_failure):
                # Not a failed guess: an outage must not lock the pair out.
                raise HTTPException(status_code=503, detail=owner_key_enrollment.UNREADABLE)
            # The hash is computed for a missing key too, so both take the same path.
            verified = verify_api_key_secret(body.new_key_secret.get_secret_value(), stored_hash or "")
            if new_metadata is None or not verified:
                owner_key_enrollment.record_failure(vouching, new_key)
                # Key ids and the organization only, never the secret that was tried.
                logger.warning("Owner key enrollment refused: api key %s could not verify %s (organization %s); "
                               "a missing key and a wrong secret read the same", vouching, new_key, org.slug)
                raise _refuse(403, "new_key_unverified", owner_key_enrollment.UNVERIFIED)
            refused = owner_key_refusal(new_metadata, None)
            if refused is not None:
                status, code, message = refused
                if status >= 500:
                    raise HTTPException(status_code=status, detail=message)
                raise _refuse(status, code, message)
            if not await owner_key_enrollment.party_check(db, org, vouching_metadata or {}, new_metadata,
                                                          vouching_key_id=vouching):
                logger.warning("Owner key enrollment refused: the minter of %s has not been a partner of "
                               "organization %s, or of the home of %s, for a day", new_key, org.slug, vouching)
                raise _refuse(403, "new_key_outside_organization", (
                    "The new key's minter has not been a partner of this organization, or of the calling key's "
                    "own organization, for a day, so the key cannot join its owner set. Ask an Orcastra "
                    "administrator to add it."
                ))
            before = await owner_key_ids(db, org.id)
            try:
                added = await add_owner(db, org, new_key, via="enroll", by=f"key:{vouching}", proof_key_id=vouching)
            except OwnerSetFull:
                raise _refuse(409, "owner_set_full", (
                    f"This organization already has {MAX_OWNER_KEYS} owner keys. Remove one before adding another."
                )) from None
            if added:
                try:
                    await db.commit()
                except IntegrityError:
                    # A concurrent enrollment of the same key won the unique constraint.
                    await db.rollback()
                    added = False
            after = await owner_key_ids(db, org.id)
    if added:
        get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
        evict_api_key_cache(vouching)
        evict_api_key_cache(new_key)
        # The partners are not notified, as there is no notification channel. The count is recorded so
        # the record says how many would have been told; the enrollment is committed by now, so a failed
        # count must not cost it its audit record.
        try:
            partner_count: Optional[int] = len(await owner_key_enrollment.partner_emails(db, org))
        except Exception as exc:  # noqa: BLE001 - the record matters more than the count
            logger.warning("Could not count the partners of organization %s: %s", org.slug, type(exc).__name__)
            partner_count = None
        record_owner_change(ADD_ACTION, org, before=before, after=after, primary_before=org.api_key_id,
                            primary_after=org.api_key_id, details={
                                "owner_key_id": new_key, "proof_key_id": vouching, "trigger": "enroll",
                                "partner_count": partner_count,
                            })
        logger.info("Api key %s joined the owner set of organization %s, vouched for by %s", new_key, org.slug, vouching)
    return OwnerKeyEnrollResponse(
        organization_external_id=org.external_id, primary_key_id=org.api_key_id, owner_key_ids=after, added=added,
    )


@external_router.delete(
    "/external/organizations/{external_id}/owner-keys/self",
    response_model=OwnerKeySelfRemoveResponse,
    dependencies=_MULTI_OWNER_ONLY,
    summary="Take the calling key out of an organization's owner set",
    description=(
        "The calling key leaves the owner set, and the oldest owner that can still sync becomes the "
        "primary if it was. Refused (409 last_owner) while no other owner can sync the organization. "
        "404 unless INTEGRATION_MULTI_OWNER_ENABLED is on."
    ),
)
async def remove_own_owner_key(
    external_id: str,
    api_key: Dict[str, Any] = Depends(require_capability("org.sync")),
    db: AsyncSession = Depends(get_db),
):
    key_id = api_key.get("id", "")
    vault_client = get_vault_integrations_client()
    async with serialize_key_ownership(db, key_id):
        org = await _external_org(db, external_id)
        if org is None or not org.api_key_id or not await is_owner(db, org, key_id):
            return OwnerKeySelfRemoveResponse(removed=False)
        async with serialize_org_owners(db, [org.id]):
            before = await owner_key_ids(db, org.id)
            # important: again under the lock. An Administrator's transfer replaces the set holding only
            # the new key's lock, so this key may have left it while this waited; then nothing changes
            # here and nothing is recorded.
            if key_id not in before:
                return OwnerKeySelfRemoveResponse(removed=False)
            primary_before = org.api_key_id
            verdicts = await able_owners([k for k in before if k != key_id], _owner_metadata_reader(vault_client))
            if not any(verdicts.values()):
                if any(v is None for v in verdicts.values()):
                    raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL)
                raise _refuse(409, "last_owner", (
                    "No other owner key can sync this organization, so this key cannot leave it. Add "
                    "another owner key first, or ask an Orcastra administrator to transfer it."
                ))
            try:
                await remove_owner(db, org, key_id, able=verdicts.get)
            except (LastOwner, NoAbleOwner):
                raise _refuse(409, "last_owner", "No other owner key can sync this organization.") from None
            except LookupError:
                raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
            await db.commit()
            after = await owner_key_ids(db, org.id)
    get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
    evict_api_key_cache(key_id)
    record_owner_change(REMOVE_ACTION, org, before=before, after=after, primary_before=primary_before,
                        primary_after=org.api_key_id, details={"owner_key_id": key_id, "trigger": "self"})
    return OwnerKeySelfRemoveResponse(removed=True, primary_key_id=org.api_key_id, owner_key_ids=after)


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


def _assignment_expiry(raw: Optional[str]) -> Optional[datetime]:
    """An assignment expiry as an instant. A value without an offset was always written as UTC.

    Kept a string on the wire, as OrcaHub sends it. A bare `fromisoformat` would hand the database
    a naive value for a timestamptz column, which compares wrongly against an aware now().
    """
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class SyncPolicy(BaseModel):
    """Policy to sync from OrcaHub."""
    external_id: str = Field(..., description="UUID from OrcaHub")
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    org_external_id: str = Field(..., description="Organization's external_id")
    rules: List[SyncPolicyRule] = []
    assignments: List[SyncPolicyAssignment] = []
    # Same meaning as on provision-tenant-access: absent keeps the stored value, null clears it.
    term_ended_at: Optional[AwareDatetime] = Field(default=None, description="When the paid term ended or ends")
    access_ends_at: Optional[AwareDatetime] = Field(default=None, description="When the retention window ends")
    as_of: Optional[AwareDatetime] = Field(default=None, description="When the sender decided these dates")


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

    important: authenticated by the dependency the rest of this router uses, never by hand. Keys
    are stored as HMAC-SHA256 keyed with SECRET_KEY (generate_api_key_pair), and the dependency
    also applies the expiry check and the cross-worker revocation tombstone.
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
    # Read once, before any policy is touched: a registry outage is a 503 for the whole call,
    # never a half-applied sync.
    registered = await cluster_registry.registered_cluster_ids()
    if registered is None:
        raise HTTPException(
            status_code=503,
            detail="Cluster registry is temporarily unavailable; retry the policy sync",
        )

    for sync_policy in body.policies:
        try:
            # A rule naming a cluster no registration knows would count for nothing; the policy
            # is skipped whole so the delete-then-replace below never runs on a partial rule set.
            unregistered = sorted({r.cluster_id for r in sync_policy.rules if r.cluster_id not in registered})
            if unregistered:
                errors.append(
                    f"Policy '{sync_policy.name}': rule names an unregistered cluster '{unregistered[0]}'"
                )
                continue
            # important: this endpoint writes the same tenant rules provisioning does, so each rule is
            # held to the key's own grant and to projects no other party's policies name. The policy
            # is skipped whole, before its rules are replaced.
            outside = [
                reason for rule in sync_policy.rules
                if (reason := rule_outside_the_key_grant(api_key, rule.cluster_id, rule.project_name))
            ]
            if outside:
                errors.append(f"Policy '{sync_policy.name}': {outside[0]}")
                continue
            try:
                for rule in sync_policy.rules:
                    if rule.project_name != "*":
                        await refuse_a_project_another_party_holds(db, api_key, rule.cluster_id, rule.project_name)
                    # A tenant rule in a view-only `default` would hand a partner's tenant the other
                    # party's guests there; `*` reaches `default` too.
                    await refuse_key_default_operation(
                        api_key, rule.cluster_id, [_rule_reach(rule.project_name)], db=db
                    )
            except HTTPException as refused:
                errors.append(f"Policy '{sync_policy.name}': {refused.detail}")
                continue

            # find org by external_id
            # important: `source` is filtered here as every sibling lookup in this file does.
            # Without it a dashboard-created organisation that happens to carry an external_id
            # would be writable through this endpoint, which grants tenants operate and console
            # access to clusters. The ownership test below is the second half.
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
            # important: an unclaimed organisation is refused too. A NULL owner must not skip the
            # check: an organisation no key has claimed is not one this key may write to.
            if not await is_owner(db, org, api_key_id):
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

            # Refused before the policy is touched, so a stale or impossible term skips it whole.
            received_term = _received_term(sync_policy)
            try:
                merged_term = _merged_term(policy, received_term) if received_term else None
            except HTTPException as refusal:
                detail = refusal.detail if isinstance(refusal.detail, dict) else {"message": str(refusal.detail)}
                errors.append(f"Policy '{sync_policy.name}': {detail.get('message')}")
                continue

            if policy:
                policy.name = sync_policy.name
                policy.description = sync_policy.description
                policy.updated_at = datetime.utcnow()
            else:
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
            if merged_term is not None:
                policy.term_ended_at = merged_term.term_ended_at
                policy.access_ends_at = merged_term.access_ends_at
                policy.term_as_of = merged_term.as_of

            # Replace the rules.
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
            # The branding link a provision makes, for an organization only ever synced (fill-only).
            sold_on = next((r.cluster_id for r in sync_policy.rules if r.cluster_id and r.cluster_id != "*"), None)
            if sold_on:
                await provider_link.link_on_provision(
                    db, org, cluster_id=sold_on, key_minter=api_key.get("created_by"), api_key_id=api_key_id,
                )

            # Assignments merge: local ones are never removed.
            for assign_data in sync_policy.assignments:
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

                existing_assign = await db.execute(
                    select(TenantPolicyAssignment).where(
                        and_(
                            TenantPolicyAssignment.policy_id == policy.id,
                            TenantPolicyAssignment.org_member_id == member.id,
                        )
                    )
                )
                assignment = existing_assign.scalar_one_or_none()
                expires_at = _assignment_expiry(assign_data.expires_at)
                if not assignment:
                    db.add(TenantPolicyAssignment(
                        policy_id=policy.id,
                        org_member_id=member.id,
                        assigned_by=f"orcahub:{api_key_id}",
                        source="orcahub",
                        external_id=sync_policy.external_id,
                        expires_at=expires_at,
                    ))
                elif assignment.source == "orcahub" and assignment.expires_at != expires_at:
                    # An assignment OrcaHub made follows OrcaHub's expiry; one a partner made here
                    # keeps its own.
                    assignment.expires_at = expires_at

            synced_count += 1

        except Exception as e:
            logger.error(f"Error syncing policy '{sync_policy.name}': {e}")
            errors.append(f"Failed to sync '{sync_policy.name}': {str(e)}")

    await db.commit()

    # This path changes what tenants may see on behalf of an external system, with no UI open to
    # notice a stale list. The helper advances the generation the instance lists fence on, so a
    # read already in flight cannot write the previous scope back.
    get_cache_manager().invalidate_after_access_change()
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

    Authenticated by the shared key dependency, like sync_policies_from_external.
    """
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    api_key_id = api_key.get("id", "")

    stmt = (
        select(TenantAccessPolicy)
        .join(Organization, TenantAccessPolicy.organization_id == Organization.id)
        .where(Organization.is_active == True)
    )

    if org_external_id:
        stmt = stmt.where(Organization.external_id == org_external_id)

    # Only organizations this API key is an owner of.
    stmt = stmt.where(owned_by_clause([api_key_id]))

    result = await db.execute(stmt)
    policies = result.scalars().unique().all()

    response_policies = []
    for policy in policies:
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
            "term_ended_at": _iso(policy.term_ended_at),
            "access_ends_at": _iso(policy.access_ends_at),
        })

    return {"policies": response_policies, "total": len(response_policies)}


# ============================================

# ============================================
# Auto-Provision Tenant Access (called by OrcaHub during provisioning)
# ============================================

async def _refuse_an_unregistered_cluster(cluster_id: str) -> None:
    """503 while the registry cannot be read, 404 when no cluster is registered under the id.

    The message echoes the caller's own input and never the registered set: an external key
    must not be able to enumerate the fleet through an error string.
    """
    registered = await cluster_registry.registered_cluster_ids()
    if registered is None:
        raise HTTPException(
            status_code=503,
            detail="Cluster registry is temporarily unavailable; retry the provisioning call",
        )
    if cluster_id not in registered:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{cluster_id}' is not registered with this control plane",
        )


class ProvisionTenantAccessRequest(BaseModel):
    """Request from OrcaHub to auto-create tenant access for a provisioned project."""
    organization_external_id: str = Field(..., description="OrcaHub organization UUID")
    cluster_id: str = Field(..., description="Cluster ID where the project was created")
    # important: a provisioned grant names the one project that was bought. '*' would reach every
    # project on the cluster, and OrcaHub never sends it (its sanity check's project pattern).
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
    # Auto-creation fields, sent when the buyer org may not exist in Orcastra yet.
    organization_name: Optional[str] = Field(default=None, description="Org name for auto-creation")
    organization_slug: Optional[str] = Field(default=None, description="Org slug for auto-creation")
    # The paid term (core/tenant_term.py). Absent leaves the stored value, null clears it.
    term_ended_at: Optional[AwareDatetime] = Field(
        default=None,
        description=(
            "When the buyer's paid term ended or ends. From then on the access is view only: no "
            "start, stop, console or snapshot. Absent keeps the stored value; null clears it."
        ),
    )
    access_ends_at: Optional[AwareDatetime] = Field(
        default=None,
        description=(
            "When the retention window ends. From then on the access grants nothing, even if no "
            "revoke follows. Must not be before term_ended_at. Absent keeps; null clears."
        ),
    )
    as_of: Optional[AwareDatetime] = Field(
        default=None,
        description=(
            "When the sender decided these dates. A request older than the last one recorded is "
            "refused with 409 stale_provision, so a delayed retry cannot undo a later decision."
        ),
    )

    @field_validator("permissions")
    @classmethod
    def _known_permissions(cls, value: List[str]) -> List[str]:
        # important: validated here, before the organization and member rows are written.
        unknown = set(value) - {"view", "operate", "console", "snapshot", "file_transfer"}
        if unknown:
            raise ValueError(f"Invalid permissions: {sorted(unknown)}")
        return value


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
    term_ended_at: Optional[datetime] = None
    access_ends_at: Optional[datetime] = None
    phase: str = "active"


_TERM_FIELDS = ("term_ended_at", "access_ends_at", "as_of")


def _received_term(body: Any) -> Dict[str, Any]:
    """The term fields the caller sent: an absent field keeps, an explicit null clears."""
    sent = getattr(body, "model_fields_set", None) or set()
    return {name: getattr(body, name) for name in _TERM_FIELDS if name in sent}


def _merged_term(stored: Optional[Any], received: Dict[str, Any]) -> TermDates:
    """Apply the received dates to the policy's, as the HTTP error the caller should read."""
    current = TermDates(
        getattr(stored, "term_ended_at", None),
        getattr(stored, "access_ends_at", None),
        getattr(stored, "term_as_of", None),
    )
    try:
        return merge_and_validate(current, received, now=datetime.now(timezone.utc))
    except TermDatesStale as exc:
        raise HTTPException(status_code=409, detail={"code": "stale_provision", "message": str(exc)}) from exc
    except TermDatesInvalid as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_term", "message": str(exc)}) from exc


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


async def _ensure_provision_cluster_mapping(
    db: AsyncSession,
    *,
    organization_id: int,
    cluster_id: str,
    registered_by: Optional[str],
) -> bool:
    """Add the provisioning binding under the shared ownership/configuration lock."""

    from app.models.db_models import OrganizationCluster

    async with serialize_cluster_share(db, cluster_id):
        existing = (
            await db.execute(
                select(OrganizationCluster).where(
                    and_(
                        OrganizationCluster.organization_id == organization_id,
                        OrganizationCluster.cluster_id == cluster_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        await assert_cluster_safe_to_share(
            db,
            cluster_id,
            prospective_organization_id=organization_id,
            prospective_registered_by=registered_by,
        )
        db.add(
            OrganizationCluster(
                organization_id=organization_id,
                cluster_id=cluster_id,
            )
        )
        await db.flush()
        return True


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
    # endpoint applies. Without it any valid key could bind another partner's cluster to its
    # own org and hand its tenants real access. Same check as revoke_tenant_access.
    cluster_permissions = {p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])}
    if body.cluster_id not in cluster_permissions:
        refuse_a_paused_grant(api_key, body.cluster_id)
        raise HTTPException(status_code=403, detail=f"No permission for cluster {body.cluster_id}")
    allowed_projects = cluster_permissions[body.cluster_id].get("projects", ["default"])
    if body.project_name not in allowed_projects and "*" not in allowed_projects:
        raise HTTPException(status_code=403, detail=f"No permission for project {body.project_name}")

    # important: the key's grant is validated when the key is minted and can carry an id no
    # cluster is registered under; a rule written under such an id counts for nothing, so it is
    # refused here, before the organization, member and cluster rows below are written.
    await _refuse_an_unregistered_cluster(body.cluster_id)
    # A tenant rule in a view-only `default` would hand a partner's tenant the other party's guests
    # there; refused before any organization, member or policy row is written.
    await refuse_key_default_operation(api_key, body.cluster_id, [_rule_reach(body.project_name)], db=db)

    api_key_id = api_key.get("id", "")
    org_created = False
    members_created = 0
    cluster_mapped = False

    # important: a stale or impossible term is refused before any organization, member or cluster
    # row below is written, so a refused call leaves nothing behind.
    from app.models.db_models import TenantAccessPolicy

    received_term = _received_term(body)
    merged_term: Optional[TermDates] = None
    if received_term:
        stored_policy = (
            await db.execute(
                select(TenantAccessPolicy)
                .join(Organization, Organization.id == TenantAccessPolicy.organization_id)
                .where(
                    and_(
                        Organization.source == "orcahub",
                        Organization.external_id == body.organization_external_id,
                        owned_by_clause([api_key_id]),
                        TenantAccessPolicy.name == f"auto:{body.project_name}",
                    )
                )
            )
        ).scalar_one_or_none()
        merged_term = _merged_term(stored_policy, received_term)

    # Step 1: resolve or auto-create org by external UUID
    stmt = select(Organization).where(
        and_(
            Organization.source == "orcahub",
            Organization.external_id == body.organization_external_id,
            owned_by_clause([api_key_id]),
        )
    )
    result = await db.execute(stmt)
    org = result.scalar_one_or_none()

    if org:
        # important: '*' in the grant covers every project, another organization's included. The
        # rule written below widens the key creator's Partner bound and hands the listed emails the
        # project, so it is refused before the first member, cluster or policy row.
        await refuse_a_project_another_party_holds(db, api_key, body.cluster_id, body.project_name)
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
        # an existing org misses it and falls through to this create. The database would then
        # refuse on uq_org_source_external_id or ix_organizations_slug with a conflict about a
        # slug, when the reason is that another integration owns the organisation. Say so here,
        # as sync() does at its own ownership guard. This denies nothing the constraints allow.
        owner_stmt = select(Organization).where(
            and_(
                Organization.source == "orcahub",
                Organization.external_id == body.organization_external_id,
            )
        )
        owner = (await db.execute(owner_stmt)).scalar_one_or_none()
        if owner is not None and owner.api_key_id and not await is_owner(db, owner, api_key_id):
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
        # important: as in a sync, only an add-on installation's current credential may claim a new
        # organization (add_on_credential_edits.is_current_add_on_credential).
        if is_add_on_credential(api_key) and not await is_current_add_on_credential(db, api_key_id):
            raise HTTPException(status_code=409, detail={
                "error": "superseded_credential",
                "message": (
                    "This add-on credential was replaced by a rotation, so it cannot create an "
                    "organization. Provision with the installation's current credential."
                ),
            })
        # important: after the ownership refusals above, so a key that does not own the organization
        # still hears that, and before the organization row is written (see the found branch).
        await refuse_a_project_another_party_holds(db, api_key, body.cluster_id, body.project_name)

        org = Organization(
            name=body.organization_name,
            slug=body.organization_slug,
            source="orcahub",
            external_id=body.organization_external_id,
            api_key_id=api_key_id,
            is_active=True,
            # important: created_by is NOT NULL. Without it this INSERT fails and a new buyer org
            # gets no ACL at purchase (500, no policy). Stamp the provisioning key's creator.
            created_by=api_key.get("created_by") or "orcahub-provisioning",
        )
        mark_entry(org, "provision")
        db.add(org)
        try:
            await db.flush()
        except IntegrityError as exc:
            # important: the lookup above keys on (source, external_id, api_key_id), but slug
            # carries its own unique index. A slug already held by a different external_id
            # misses that lookup and violates ix_organizations_slug on INSERT, which would escape
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



    # Step 1b: the buyer shows its seller's branding until it sets its own. It is a link, so a
    # later change by the seller reaches every buyer. Fill-only and never fatal
    # (services/branding/provider_link.py).
    await provider_link.link_on_provision(
        db, org, cluster_id=body.cluster_id, key_minter=api_key.get("created_by"), api_key_id=api_key_id,
    )

    # Step 2: ensure cluster is mapped to this org
    try:
        cluster_mapped = await _ensure_provision_cluster_mapping(
            db,
            organization_id=org.id,
            cluster_id=body.cluster_id,
            registered_by=api_key.get("created_by"),
        )
    except SharedClusterGuardError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.as_detail(),
        ) from exc
    if cluster_mapped:
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

    # Find or create the policy for this project (idempotent).
    from app.models.db_models import TenantPolicyRule, TenantPolicyAssignment

    policy_name = f"auto:{body.project_name}"
    policy_stmt = select(TenantAccessPolicy).where(
        and_(
            TenantAccessPolicy.organization_id == org.id,
            TenantAccessPolicy.name == policy_name,
        )
    )
    policy_result = await db.execute(policy_stmt)
    policy = policy_result.scalar_one_or_none()
    policy_created = policy is None

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

    term_before = {
        "term_ended_at": _iso(policy.term_ended_at),
        "access_ends_at": _iso(policy.access_ends_at),
    }
    if merged_term is not None:
        policy.term_ended_at = merged_term.term_ended_at
        policy.access_ends_at = merged_term.access_ends_at
        policy.term_as_of = merged_term.as_of

    # Upsert the rule for this cluster/project/instance.
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
    permissions_before = sorted(existing_rule.permissions or []) if existing_rule else None

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

    # Assign the policy to each user email (the members exist by now).
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

    # Invalidate caches so tenants see access immediately.
    try:
        get_cache_manager().invalidate_after_access_change()
    except Exception:
        pass

    # A replay that changed nothing writes no record; a narrower permission set is the downgrade
    # OrcaHub sends at the end of a paid term, recorded as such.
    term_after = {"term_ended_at": _iso(policy.term_ended_at), "access_ends_at": _iso(policy.access_ends_at)}
    permissions_after = sorted(set(body.permissions))
    changed = (
        policy_created
        or rules_created
        or assignments_created
        or members_created
        or permissions_before != permissions_after
        or term_before != term_after
    )
    if changed:
        _audit_key_action(
            api_key,
            action="tenant.access.provision" if policy_created else "tenant.access.update",
            target_id=policy_name,
            cluster_id=body.cluster_id,
            project=body.project_name,
            details={
                "permissions_before": permissions_before,
                "permissions_after": permissions_after,
                "downgrade": permissions_before is not None and "operate" in permissions_before
                and "operate" not in permissions_after,
                "term_before": term_before,
                "term_after": term_after,
                "assignments_created": assignments_created,
                "members_created": members_created,
            },
            target_organization_id=org.id,
            target_organization_slug=org.slug,
        )
    term_state = policy_phase(
        source=policy.source,
        term_ended_at=policy.term_ended_at,
        access_ends_at=policy.access_ends_at,
        permissions=set(permissions_after),
        now=datetime.now(timezone.utc),
    )

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
        term_ended_at=policy.term_ended_at,
        access_ends_at=policy.access_ends_at,
        phase=term_state.phase,
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
    reason: Optional[str] = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z0-9_.-]+$",
        description="Why the access ends, recorded in the audit trail, for example term_ended or cancelled",
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
        refuse_a_paused_grant(api_key, body.cluster_id)
        raise HTTPException(status_code=403, detail=f"No permission for cluster {body.cluster_id}")
    allowed_projects = cluster_permissions[body.cluster_id].get("projects", ["default"])
    if body.project_name not in allowed_projects and "*" not in allowed_projects:
        raise HTTPException(status_code=403, detail=f"No permission for project {body.project_name}")

    def _invalidate_tenant_caches():
        try:
            get_cache_manager().invalidate_after_access_change()
        except Exception:
            pass

    def _audit_revoke(org: Any, *, policy_deleted: bool, rules: int, assignments: int) -> None:
        # Counts only; the emails named in the request are not copied into the trail.
        _audit_key_action(
            api_key,
            action="tenant.access.revoke",
            target_id=f"auto:{body.project_name}",
            cluster_id=body.cluster_id,
            project=body.project_name,
            details={
                "reason": body.reason,
                "policy_deleted": policy_deleted,
                "rules_deleted": rules,
                "assignments_deleted": assignments,
                "users_named": len(body.user_emails),
            },
            target_organization_id=org.id,
            target_organization_slug=org.slug,
        )

    # Resolve org as provision does; absent org = nothing to revoke (idempotent success).
    caller_key_id = api_key.get("id", "")
    org = (await db.execute(select(Organization).where(and_(
        Organization.source == "orcahub",
        Organization.external_id == body.organization_external_id,
        owned_by_clause([caller_key_id]),
    )))).scalar_one_or_none()
    if not org:
        # important: that lookup folds api_key_id in, so it misses for two different
        # reasons. No such organisation is an idempotent success. An organisation owned by
        # another key is not: a 200 would tell the caller a revoke succeeded while nothing
        # was touched. Provision refuses this exact condition by name, so revoke agrees.
        owner = (await db.execute(select(Organization).where(and_(
            Organization.source == "orcahub",
            Organization.external_id == body.organization_external_id,
        )))).scalar_one_or_none()
        if owner is not None and owner.api_key_id and not await is_owner(db, owner, caller_key_id):
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

    # A project-level revoke (no user_emails), and below a user-level one that removes the last
    # assignment, deletes the whole policy and lets CASCADE take its rules and assignments.
    if not body.user_emails:
        policy_id = policy.id
        await db.delete(policy)
        await db.commit()
        _invalidate_tenant_caches()
        _audit_revoke(org, policy_deleted=True, rules=rule_count, assignments=len(assignment_rows))
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
        _audit_revoke(org, policy_deleted=True, rules=rule_count, assignments=len(to_delete))
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
    if to_delete:
        _audit_revoke(org, policy_deleted=False, rules=0, assignments=len(to_delete))
    logger.info(f"Tenant access revoked (users): {policy_name}, org={org.slug}, assignments={len(to_delete)}")
    return RevokeTenantAccessResponse(
        policy_deleted=False, policy_id=policy.id,
        rules_deleted=0, assignments_deleted=len(to_delete),
        organization_id=org.id,
    )
