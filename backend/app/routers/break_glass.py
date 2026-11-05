"""
Break-Glass Router

Least-privilege admin access to tenant console/terminal: an admin justifies a
time-boxed grant for a specific cluster, every grant and use is audited, and the
affected tenant can see it. Reads are open to all roles (tenants get an accountability
view of grants on their own clusters); writes are admin-only.
"""

from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, require_admin, require_tenant
from app.core.rbac import Role, resolve_roles, get_highest_role
from app.core.org_scope import get_current_allowed_clusters, get_user_org_ids, org_scope_dependency
from app.services import break_glass_service
from app.services.activity_logger import get_activity_logger
from app.models.db_models import BreakGlassGrant

router = APIRouter(prefix="/break-glass", tags=["Break-Glass"])

activity_logger = get_activity_logger()


class GrantCreateRequest(BaseModel):
    cluster_id: str = Field(..., min_length=1, description="Target cluster (LXD host key)")
    # note: naming a key makes this a grant FOR that integration rather than for the caller.
    # Omitted, the caller grants themselves, which is the original behaviour.
    api_key_id: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Grant an integration key instead of yourself. The key still needs the capability "
            "and the cluster grant; this is the time-boxed, revocable authorisation on top."
        ),
    )
    reason: str = Field(..., min_length=1, max_length=2000, description="Justification (required)")
    ticket_ref: Optional[str] = Field(None, max_length=255)
    duration_minutes: Optional[int] = Field(None, ge=1, le=1440)


class GrantResponse(BaseModel):
    id: int
    # Optional because exactly one grantee is set. A key grant has no person on this field, and
    # declaring it non-optional meant such a row could not be serialized back even if one existed.
    admin_email: Optional[str] = None
    api_key_id: Optional[str] = None
    cluster_id: str
    target_organization_id: Optional[int]
    target_org_slug: Optional[str]
    reason: str
    ticket_ref: Optional[str]
    status: str  # effective (active|expired|revoked)
    created_by: str
    created_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime]
    revoked_by: Optional[str]


def _to_response(g: BreakGlassGrant) -> GrantResponse:
    return GrantResponse(
        id=g.id,
        admin_email=g.admin_email,
        api_key_id=g.api_key_id,
        cluster_id=g.cluster_id,
        target_organization_id=g.target_organization_id,
        target_org_slug=g.target_org_slug,
        reason=g.reason,
        ticket_ref=g.ticket_ref,
        status=break_glass_service.effective_status(g),
        created_by=g.created_by,
        created_at=g.created_at,
        expires_at=g.expires_at,
        revoked_at=g.revoked_at,
        revoked_by=g.revoked_by,
    )


@router.get("/check", dependencies=[Depends(org_scope_dependency)], summary="Pre-flight: is a break-glass grant needed for a cluster?")
async def check_access(
    cluster_id: str,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    """Non-mutating: lets the UI prompt for a grant before opening console/terminal.

    important: Admin-gated to match `create_grant` below. Break-glass is an admin mechanism,
    and the response names the organisation that owns an arbitrary cluster, so under
    `require_tenant()` any authenticated user could map the whole fleet to its owners. The UI
    fails this check open (`break-glass-store.ts:38-41`) and relies on the connect-time gate,
    so a non-admin getting 403 here loses nothing it was entitled to.
    """
    return await break_glass_service.inspect_access(user, cluster_id, db)


@router.post("/grants", response_model=GrantResponse, status_code=201, summary="Request a break-glass grant")
async def create_grant(
    body: GrantCreateRequest,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    # important: the cluster's current certificate fingerprint is pinned onto a key grant.
    # `cluster_id` is a free-text Vault key with no foreign key, so a cluster deleted and
    # re-registered under a different owner matches an old grant by string alone. For a human
    # that is a session somebody is watching; for an unattended key it is hands on a stranger's
    # keyboard for as long as the grant lasts.
    fingerprint = (
        await break_glass_service.cluster_fingerprint(body.cluster_id)
        if body.api_key_id
        else None
    )
    grant = await break_glass_service.create_grant(
        db,
        created_by=user.email,
        admin_email=None if body.api_key_id else user.email,
        api_key_id=body.api_key_id,
        cluster_id=body.cluster_id,
        reason=body.reason,
        ticket_ref=body.ticket_ref,
        duration_minutes=body.duration_minutes,
        cluster_fingerprint=fingerprint,
    )
    activity_logger.log_activity(
        action="break_glass.grant.create",
        target_type="cluster",
        target_id=grant.cluster_id,
        target_organization_id=grant.target_organization_id,
        target_organization_slug=grant.target_org_slug,
        result="success",
        details={
            "reason": grant.reason,
            "expires_at": grant.expires_at.isoformat(),
            "grant_id": grant.id,
            "grantee": grant.admin_email or f"key:{grant.api_key_id}",
        },
    )
    return _to_response(grant)


@router.get("/grants", response_model=list[GrantResponse], dependencies=[Depends(org_scope_dependency)], summary="List break-glass grants")
async def list_grants(
    mine: bool = False,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin: all grants (or ?mine=true for their own). Partner/tenant: an accountability
    view of grants on clusters in their own organization scope (nothing from other orgs).
    """
    is_admin = get_highest_role(resolve_roles(user)) == Role.ADMIN
    # important: the REQUEST-scoped set org_scope_dependency enforces (narrowed by a Tenant's
    # ACL), never a fresh get_user_allowed_clusters(), as console.py's session list learned. None
    # for an Admin; an empty set matches nothing (security review 2026-11-05, finding 6).
    allowed = get_current_allowed_clusters()
    # On a cluster several organizations hold, only the grants aimed at the caller's own, or at
    # the whole cluster (no target organization).
    organizations = None if is_admin else set(await get_user_org_ids(user, db))
    grants = await break_glass_service.list_grants(
        db,
        cluster_filter=allowed,
        mine_email=(user.email if (is_admin and mine) else None),
        organization_filter=organizations,
    )
    return [_to_response(g) for g in grants]


@router.post("/grants/{grant_id}/revoke", response_model=GrantResponse, summary="Revoke a break-glass grant")
async def revoke_grant(
    grant_id: int,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    grant = await break_glass_service.revoke_grant(db, grant_id, revoked_by=user.email)
    activity_logger.log_activity(
        action="break_glass.grant.revoke",
        target_type="cluster",
        target_id=grant.cluster_id,
        target_organization_id=grant.target_organization_id,
        target_organization_slug=grant.target_org_slug,
        result="success",
        details={"grant_id": grant.id},
    )
    return _to_response(grant)
