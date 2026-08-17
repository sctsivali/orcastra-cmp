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
from app.core.org_scope import org_scope_dependency, get_user_allowed_clusters
from app.services import break_glass_service
from app.services.activity_logger import get_activity_logger
from app.models.db_models import BreakGlassGrant

router = APIRouter(prefix="/break-glass", tags=["Break-Glass"])

activity_logger = get_activity_logger()


class GrantCreateRequest(BaseModel):
    cluster_id: str = Field(..., min_length=1, description="Target cluster (LXD host key)")
    reason: str = Field(..., min_length=1, max_length=2000, description="Justification (required)")
    ticket_ref: Optional[str] = Field(None, max_length=255)
    duration_minutes: Optional[int] = Field(None, ge=1, le=1440)


class GrantResponse(BaseModel):
    id: int
    admin_email: str
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
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """Non-mutating: lets the UI prompt for a grant before opening console/terminal."""
    return await break_glass_service.inspect_access(user, cluster_id, db)


@router.post("/grants", response_model=GrantResponse, status_code=201, summary="Request a break-glass grant")
async def create_grant(
    body: GrantCreateRequest,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    grant = await break_glass_service.create_grant(
        db,
        admin_email=user.email,
        cluster_id=body.cluster_id,
        reason=body.reason,
        ticket_ref=body.ticket_ref,
        duration_minutes=body.duration_minutes,
    )
    activity_logger.log_activity(
        action="break_glass.grant.create",
        target_type="cluster",
        target_id=grant.cluster_id,
        target_organization_id=grant.target_organization_id,
        target_organization_slug=grant.target_org_slug,
        result="success",
        details={"reason": grant.reason, "expires_at": grant.expires_at.isoformat(), "grant_id": grant.id},
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
    # get_user_allowed_clusters returns None for admin (no filter) or the caller's own set.
    allowed = await get_user_allowed_clusters(user, db)
    grants = await break_glass_service.list_grants(
        db,
        cluster_filter=allowed,  # None=admin all; set=scoped to caller's clusters
        mine_email=(user.email if (is_admin and mine) else None),
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
