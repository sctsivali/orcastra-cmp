"""
User Group Assignment Router

Provides endpoints for the Settings -> User Groups UI.
Admin can assign users (by email) to role groups and organizations,
with automatic push to Authentik.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, require_partner
from app.core.rbac import Role, resolve_roles, get_highest_role
from app.models.db_models import OrganizationMember, Organization
from app.services.authentik_sync import get_authentik_sync

router = APIRouter(
    prefix="/user-groups",
    tags=["User Groups"],
    dependencies=[Depends(require_partner())],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class UserGroupEntry(BaseModel):
    user_email: str
    role: str  # admin | partner | tenant
    organizations: list[dict] = []  # [{id, name, slug}]


class AssignRoleRequest(BaseModel):
    user_email: EmailStr = Field(..., max_length=255)
    role: str = Field(..., pattern="^(admin|partner|tenant)$")
    organization_id: Optional[int] = None  # required for partner/tenant


class AssignRoleResponse(BaseModel):
    user_email: str
    role: str
    organization_id: Optional[int] = None
    authentik_synced: bool = False


class AuthentikSyncStatus(BaseModel):
    authentik_configured: bool
    authentik_reachable: bool
    groups_exist: bool = False
    details: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[UserGroupEntry])
async def list_user_groups(
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """
    List users with their role and organization memberships.
    Admin sees all; partner sees only users in their own organizations.
    """
    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    base_query = (
        select(
            OrganizationMember.user_email,
            OrganizationMember.role,
            OrganizationMember.organization_id,
            Organization.name,
            Organization.slug,
        )
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .order_by(OrganizationMember.user_email)
    )

    # partner: restrict to orgs they created or belong to
    if highest != Role.ADMIN:
        from sqlalchemy import or_ as sa_or
        partner_org_ids = (
            select(Organization.id).where(
                Organization.is_active == True,
                sa_or(
                    Organization.created_by == user.email,
                    Organization.id.in_(
                        select(OrganizationMember.organization_id)
                        .where(OrganizationMember.user_email == user.email)
                    ),
                ),
            )
        )
        base_query = base_query.where(
            OrganizationMember.organization_id.in_(partner_org_ids)
        )

    result = await db.execute(base_query)

    rows = result.fetchall()

    # aggregate by user
    user_map: dict[str, UserGroupEntry] = {}
    for email, role, org_id, org_name, org_slug in rows:
        if email not in user_map:
            # normalize legacy "member" role to "tenant"
            display_role = role if role != "member" else "tenant"
            user_map[email] = UserGroupEntry(
                user_email=email,
                role=display_role,
                organizations=[],
            )
        # track highest role ("member" is legacy fallback → treat as tenant)
        role_priority = {"admin": 2, "partner": 1, "tenant": 0, "member": 0}
        if role_priority.get(role, 0) > role_priority.get(user_map[email].role, 0):
            user_map[email].role = role if role != "member" else "tenant"

        user_map[email].organizations.append({
            "id": org_id,
            "name": org_name,
            "slug": org_slug,
        })

    return list(user_map.values())


@router.post("/assign", response_model=AssignRoleResponse)
async def assign_user_role(
    body: AssignRoleRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """
    Assign a user to a role group. For partner/tenant, also assigns to an organization.
    Partners can only assign tenant role within their own organizations.
    Admin can assign any role to any organization.
    """
    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    # partner restrictions
    if highest != Role.ADMIN:
        # partners can only assign the "tenant" role
        if body.role != "tenant":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Partners can only assign the tenant role",
            )
        # partners must specify an org they own / belong to
        if not body.organization_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="organization_id is required",
            )
        from sqlalchemy import or_ as sa_or
        own_org = await db.execute(
            select(Organization.id).where(
                Organization.id == body.organization_id,
                Organization.is_active == True,
                sa_or(
                    Organization.created_by == user.email,
                    Organization.id.in_(
                        select(OrganizationMember.organization_id)
                        .where(
                            OrganizationMember.user_email == user.email,
                            OrganizationMember.role == "partner",
                        )
                    ),
                ),
            )
        )
        if not own_org.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only assign tenants to organizations you own",
            )

        # important: the checks above validate the ORGANISATION but said nothing about the
        # TARGET. A partner could name an admin's email with role="tenant" in an org they own,
        # and the Authentik push below strips that user's role_admin globally. A partner may
        # only move someone who is not already an admin or another partner. An unknown answer
        # is refused rather than assumed empty, so an Authentik outage cannot be used as the
        # opening.
        current_groups = await get_authentik_sync().get_user_role_groups(body.user_email)
        if current_groups is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cannot verify the target user's current role right now; try again later",
            )
        if current_groups & {"role_admin", "role_partner"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an admin can change the role of an admin or a partner",
            )

    # admin role doesn't require org assignment
    if body.role in ("partner", "tenant") and not body.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id is required for partner and tenant roles",
        )

    # if org specified, verify it exists
    if body.organization_id:
        org = await db.execute(
            select(Organization).where(Organization.id == body.organization_id)
        )
        if not org.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )

        # add/update org membership
        existing = await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == body.organization_id,
                OrganizationMember.user_email == body.user_email,
            )
        )
        member = existing.scalar_one_or_none()
        member_role = body.role
        if member:
            member.role = member_role
        else:
            db.add(OrganizationMember(
                organization_id=body.organization_id,
                user_email=body.user_email,
                role=member_role,
                invited_by=user.email,
            ))
        await db.commit()

    # push to Authentik
    sync_svc = get_authentik_sync()
    synced = await sync_svc.sync_user_role(body.user_email, body.role)

    # push to OrcaHub (non-blocking, best-effort)
    if body.organization_id:
        try:
            from app.services.orcahub_sync import push_members_to_orcahub
            await push_members_to_orcahub(
                db,
                body.organization_id,
                [{"email": body.user_email, "role": body.role, "action": "upsert"}],
            )
        except Exception as e:
            logger.warning(f"OrcaHub push failed (non-blocking): {e}")

    return AssignRoleResponse(
        user_email=body.user_email,
        role=body.role,
        organization_id=body.organization_id,
        authentik_synced=synced,
    )


@router.delete("/{user_email}")
async def remove_user_from_all(
    user_email: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """
    Remove a user from organizations and Authentik role groups.
    Admin: removes from all orgs. Partner: removes only from their own orgs.
    """
    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    from sqlalchemy import delete

    if highest == Role.ADMIN:
        # admin: remove from all orgs
        removed = await db.execute(
            delete(OrganizationMember)
            .where(OrganizationMember.user_email == user_email)
            .returning(OrganizationMember.organization_id, OrganizationMember.role)
        )
    else:
        # partner: only remove from orgs they own/belong to
        from sqlalchemy import or_ as sa_or
        partner_org_ids = (
            select(Organization.id).where(
                Organization.is_active == True,
                sa_or(
                    Organization.created_by == user.email,
                    Organization.id.in_(
                        select(OrganizationMember.organization_id)
                        .where(
                            OrganizationMember.user_email == user.email,
                            OrganizationMember.role == "partner",
                        )
                    ),
                ),
            )
        )
        # important: OrcaHub owns the roster of an organization it synced and lets only a platform
        # admin remove a partner there. A dashboard removal is not sent there for a partner (see
        # below), so the next sync would quietly bring them back. Refused, with the reason.
        synced_partner = (await db.execute(
            select(Organization.slug)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                OrganizationMember.user_email == user_email,
                OrganizationMember.organization_id.in_(partner_org_ids),
                OrganizationMember.role != "tenant",
                Organization.source == "orcahub",
                Organization.api_key_id.isnot(None),
            )
        )).scalars().all()
        if synced_partner:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{user_email} is a partner of {', '.join(sorted(synced_partner))}, which OrcaHub "
                    "manages. A platform admin removes a partner there, or an Orcastra admin here."
                ),
            )
        removed = await db.execute(
            delete(OrganizationMember)
            .where(
                OrganizationMember.user_email == user_email,
                OrganizationMember.organization_id.in_(partner_org_ids),
            )
            .returning(OrganizationMember.organization_id, OrganizationMember.role)
        )
    removed_rows = removed.all()
    removed_org_ids = {org_id for org_id, _role in removed_rows}
    await db.commit()

    # remove from Authentik (only admin should do this globally)
    if highest == Role.ADMIN:
        sync_svc = get_authentik_sync()
        await sync_svc.remove_user_from_all_roles(user_email)

    # important: OrcaHub lets only a platform admin remove a partner from an organization, and
    # its group sync then takes their role_partner away. So a non-admin's removal is sent only for
    # the organizations where the removed row was a tenant. The dashboard removal itself is
    # unchanged.
    pushed_org_ids = (
        removed_org_ids if highest == Role.ADMIN
        else {org_id for org_id, role in removed_rows if role == "tenant"}
    )
    # push removal to OrcaHub (non-blocking, best-effort), only for the orgs just left
    try:
        from app.services.orcahub_sync import push_member_removal_to_orcahub
        await push_member_removal_to_orcahub(db, user_email, pushed_org_ids)
    except Exception as e:
        logger.warning(f"OrcaHub removal push failed (non-blocking): {e}")

    return {"detail": f"User {user_email} removed from organizations"}


@router.get("/sync-status", response_model=AuthentikSyncStatus)
async def get_sync_status(
    user: AuthenticatedUser = Depends(require_partner()),
):
    """Check Authentik sync connectivity status."""
    sync_svc = get_authentik_sync()
    result = await sync_svc.get_sync_status()

    groups = result.get("groups", {})
    error = result.get("error", "")
    enabled = result.get("enabled", False)
    connected = result.get("connected", False)

    if not enabled:
        details = "Not configured — set AUTHENTIK_API_URL & AUTHENTIK_API_TOKEN to enable sync"
    elif not connected:
        details = f"Configured but unreachable — {error}" if error else "Configured but unreachable"
    else:
        details = f"{len(groups)} role group(s) ready"

    return AuthentikSyncStatus(
        authentik_configured=enabled,
        authentik_reachable=connected,
        groups_exist=bool(groups),
        details=details,
    )
