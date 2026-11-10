"""Settings -> User Groups: assign users by email to role groups and organizations.

An Admin assigns any role; a Partner assigns the tenant role in organizations it runs. Every
assignment, and an Admin's removal, is pushed to Authentik.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_partner
from app.core.rbac import Role, resolve_roles, get_highest_role
from app.models.db_models import OrganizationMember, Organization
from app.services.authentik_role_reading import ENDED_REASONS
from app.services.authentik_sync import get_authentik_sync
from app.services.cluster_parties import RUNNING_ROLES, run_orgs
from app.services.organization_roster import synced_partner_orgs, synced_partner_refusal

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
    role: str  # admin | partner | tenant, the highest across organizations
    organizations: list[dict] = []  # [{id, name, slug, role}]


# The legacy "member" row reads as tenant everywhere else (org_scope, the cluster parties).
_ROLE_PRIORITY = {"admin": 2, "partner": 1, "tenant": 0}


def _display_role(role: str) -> str:
    return "tenant" if role == "member" else role


def _match_key(email: str) -> str:
    """How two spellings of one address compare. Authentik matches an email case-insensitively.

    important: for comparing only. A row keeps the address as typed, because the authorization
    paths compare a token's email exactly, and a lowercased row would lock out an account whose
    identity provider email has capitals.
    """
    return (email or "").strip().lower()


def _plausible_address(email: str) -> bool:
    return 0 < len(email) <= 255 and "@" in email and not any(c.isspace() for c in email)


async def refuse_unless_a_partner_may_move(target_email: str) -> None:
    """Raise unless Authentik confirms the target holds neither Admin nor Partner.

    important: the push that follows strips every other role group, so an answer Authentik could
    not give must never read as "holds nothing". The strict read resolves alias groups the way
    the request path does; only an email no account carries counts as holding no role.
    """
    reading = await get_authentik_sync().read_current_roles(target_email)
    if reading.roles is None:
        if reading.reason == "user_not_found":
            return
        if reading.reason in ENDED_REASONS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an admin can change the role of this account",
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cannot verify the target user's current role right now; try again later",
        )
    if reading.roles & {Role.ADMIN, Role.PARTNER}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can change the role of an admin or a partner",
        )


class AssignRoleRequest(BaseModel):
    user_email: EmailStr = Field(..., max_length=255)
    role: str = Field(..., pattern="^(admin|partner|tenant)$")
    organization_id: Optional[int] = None  # required for partner/tenant

    @field_validator("user_email", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        # A pasted address often carries a space either side; EmailStr refuses it rather than trims.
        return value.strip() if isinstance(value, str) else value


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

@router.get("", response_model=list[UserGroupEntry])
async def list_user_groups(
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """List users with their role and organization memberships.

    An Admin sees everyone; a Partner sees the members of the organizations it runs.
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
        .order_by(func.lower(OrganizationMember.user_email), Organization.name)
    )

    # important: a partner sees the rosters of the organizations it runs, the same set assign and
    # remove act on. Counting any membership would show a partner that bought from another
    # partner that seller's whole customer list.
    if highest != Role.ADMIN:
        caller_orgs = sorted(await run_orgs(db, user.email))
        if not caller_orgs:
            return []
        base_query = base_query.where(OrganizationMember.organization_id.in_(caller_orgs))

    result = await db.execute(base_query)

    rows = result.fetchall()

    # Aggregate by person: rows that differ only in email case land on one entry.
    user_map: dict[str, UserGroupEntry] = {}
    for email, role, org_id, org_name, org_slug in rows:
        key = _match_key(email)
        org_role = _display_role(role)
        entry = user_map.get(key)
        if entry is None:
            entry = user_map[key] = UserGroupEntry(user_email=email, role=org_role, organizations=[])
        elif _ROLE_PRIORITY.get(org_role, 0) > _ROLE_PRIORITY.get(entry.role, 0):
            entry.role = org_role
        if any(org["id"] == org_id for org in entry.organizations):
            continue
        entry.organizations.append({
            "id": org_id,
            "name": org_name,
            "slug": org_slug,
            "role": org_role,
        })

    return list(user_map.values())


@router.post("/assign", response_model=AssignRoleResponse, dependencies=[Depends(check_read_only_mode)])
async def assign_user_role(
    body: AssignRoleRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Assign a user to a role group and, for partner or tenant, to an organization.

    An Admin may assign any role in any organization. A Partner may assign only the tenant role,
    in an organization it runs, and never to an existing Admin or Partner (403); 503 when
    Authentik cannot confirm the target's current role.
    """
    roles = resolve_roles(user)
    highest = get_highest_role(roles)
    # As typed (the validator trimmed it, EmailStr lowercased the domain); compared case-insensitively.
    target_email = body.user_email

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
                            OrganizationMember.role.in_(RUNNING_ROLES),
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

        # important: the checks above validate the organisation only. Naming an admin's email
        # with role "tenant" would make the Authentik push below strip that user's role_admin
        # globally, so a partner may only move someone who is not already an admin or partner.
        await refuse_unless_a_partner_may_move(target_email)

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
                func.lower(OrganizationMember.user_email) == _match_key(target_email),
            )
        )
        member = existing.scalars().first()
        member_role = body.role
        if member:
            member.role = member_role
        else:
            db.add(OrganizationMember(
                organization_id=body.organization_id,
                user_email=target_email,
                role=member_role,
                invited_by=user.email,
            ))
        await db.commit()

    # push to Authentik
    sync_svc = get_authentik_sync()
    synced = await sync_svc.sync_user_role(target_email, body.role)

    # push to OrcaHub (non-blocking, best-effort)
    if body.organization_id:
        try:
            from app.services.orcahub_sync import push_members_to_orcahub
            await push_members_to_orcahub(
                db,
                body.organization_id,
                [{"email": target_email, "role": body.role, "action": "upsert"}],
            )
        except Exception as e:
            logger.warning(f"OrcaHub push failed (non-blocking): {e}")

    return AssignRoleResponse(
        user_email=target_email,
        role=body.role,
        organization_id=body.organization_id,
        authentik_synced=synced,
    )


@router.delete("/{user_email}", dependencies=[Depends(check_read_only_mode)])
async def remove_user_from_all(
    user_email: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Remove a user from organizations.

    An Admin removes them from every organization and every Authentik role group (502 when
    Authentik does not confirm). A Partner removes them only from the organizations it runs: 404
    when there are none, 409 when every one left is a roster OrcaHub keeps.
    """
    roles = resolve_roles(user)
    highest = get_highest_role(roles)
    user_email = (user_email or "").strip()
    # important: refuse at the boundary. A blank address would reach the identity provider as an
    # empty search, which lists every account.
    if not _plausible_address(user_email):
        raise HTTPException(status_code=422, detail="Give the email address of the user to remove")
    same_person = func.lower(OrganizationMember.user_email) == _match_key(user_email)

    from sqlalchemy import delete

    kept: dict[int, str] = {}
    if highest == Role.ADMIN:
        # An admin removes the user from every organization.
        removed = await db.execute(
            delete(OrganizationMember)
            .where(same_person)
            .returning(OrganizationMember.organization_id)
        )
    else:
        # A partner removes only from the organizations it runs (created, or a partner row there).
        # important: OrcaHub owns the roster of an organization it synced and lets only a platform
        # admin remove a partner there, so the next sync would bring such a row back. Those rows
        # are kept and named, and the rest of the caller's organizations are still cleared.
        caller_orgs = set(await run_orgs(db, user.email))
        kept = await synced_partner_orgs(db, user_email, sorted(caller_orgs))
        removed = await db.execute(
            delete(OrganizationMember)
            .where(
                same_person,
                OrganizationMember.organization_id.in_(sorted(caller_orgs - set(kept))),
            )
            .returning(OrganizationMember.organization_id)
        )
    removed_org_ids = sorted({row[0] for row in removed.all()})
    if kept and not removed_org_ids:
        raise HTTPException(status_code=409, detail=synced_partner_refusal(user_email, list(kept.values())))
    # A removal reaches only the orgs the caller runs, which is also all its list shows; an
    # address that matches none of them gets a 404.
    if highest != Role.ADMIN and not removed_org_ids:
        raise HTTPException(
            status_code=404, detail=f"{user_email} is not a member of any organization you run"
        )
    await db.commit()

    # remove from Authentik (only admin should do this globally)
    if highest == Role.ADMIN:
        sync_svc = get_authentik_sync()
        if not await sync_svc.remove_user_from_all_roles(user_email):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Authentik role removal was not confirmed; retry this operation",
            )

    # push removal to OrcaHub for the organizations just left (non-blocking, best-effort)
    try:
        from app.services.orcahub_sync import push_member_removal_to_orcahub
        await push_member_removal_to_orcahub(db, user_email, removed_org_ids)
    except Exception as e:
        logger.warning(f"OrcaHub removal push failed (non-blocking): {e}")

    if kept:
        return {
            "detail": f"Removed from the other organizations. {synced_partner_refusal(user_email, list(kept.values()))}",
            "kept": list(kept.values()),
        }
    return {"detail": f"User {user_email} removed from organizations", "kept": []}


@router.get("/sync-status", response_model=AuthentikSyncStatus)
async def get_sync_status(
    user: AuthenticatedUser = Depends(require_partner()),
):
    """Whether Authentik role sync is configured and reachable, and whether its role groups exist."""
    sync_svc = get_authentik_sync()
    result = await sync_svc.get_sync_status()

    groups = result.get("groups", {})
    error = result.get("error", "")
    enabled = result.get("enabled", False)
    connected = result.get("connected", False)

    if not enabled:
        details = "Not configured. Set AUTHENTIK_API_URL and AUTHENTIK_API_TOKEN to enable sync"
    elif not connected:
        details = f"Configured but unreachable ({error})" if error else "Configured but unreachable"
    else:
        details = f"{len(groups)} role group(s) ready"

    return AuthentikSyncStatus(
        authentik_configured=enabled,
        authentik_reachable=connected,
        groups_exist=bool(groups),
        details=details,
    )
