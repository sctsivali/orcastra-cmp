"""Who may change an organization's roster, and which rows OrcaHub owns.

The member routes checked OrganizationService.user_has_access, which answers "may this user see
the organization" and is true for a tenant row too. A partner who only bought from another partner
could add, change and remove that partner's members with it.

A roster is changed by whoever runs the organization: an admin, its creator, or a partner row
(services.cluster_parties.RUNNING_ROLES, the same answer the party model reads). OrcaHub owns the
roster of an organization it synced and lets only a platform admin remove a partner there, so a
non-admin change to a partner row of such an organization is refused and named: the dashboard
change is not sent there for a partner, and the next sync would quietly undo it.
"""

from typing import Dict, List

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationMember
from app.services.cluster_parties import RUNNING_ROLES

__all__ = ["refuse_a_synced_partner", "synced_partner_orgs", "synced_partner_refusal", "user_runs_org"]


def synced_partner_refusal(member_email: str, slugs: List[str]) -> str:
    """Why a non-admin may not change a partner row in an organization OrcaHub manages."""
    return (
        f"{member_email} is a partner of {', '.join(slugs)}, which OrcaHub manages. An Orcastra "
        "admin can remove them here, or a platform admin in OrcaHub."
    )


async def user_runs_org(db: AsyncSession, org_id: int, user_email: str, user_role: str) -> bool:
    """Whether a user may change an organization's roster: admin, its creator, or its partner row.

    note: the creator counts whatever its own row says. create_organization writes the creator's
    row as the legacy "member" role, so created_by is what makes it run the organization.
    """
    if user_role == "admin":
        return True
    org = await db.get(Organization, org_id)
    if org is None:
        return False
    if org.created_by == user_email:
        return True
    row = await db.execute(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_email == user_email,
            OrganizationMember.role.in_(RUNNING_ROLES),
        )
    )
    return row.first() is not None


async def synced_partner_orgs(db: AsyncSession, member_email: str, org_ids: List[int]) -> Dict[int, str]:
    """OrcaHub-synced organizations, among org_ids, where member_email holds a partner row (id to slug)."""
    if not org_ids:
        return {}
    rows = await db.execute(
        select(Organization.id, Organization.slug)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            OrganizationMember.user_email == member_email,
            OrganizationMember.organization_id.in_(sorted(set(org_ids))),
            OrganizationMember.role.in_(RUNNING_ROLES),
            Organization.source == "orcahub",
            Organization.api_key_id.isnot(None),
        )
        .order_by(Organization.slug)
    )
    return {org_id: slug for org_id, slug in rows.all()}


async def refuse_a_synced_partner(db: AsyncSession, member_email: str, org_id: int) -> None:
    """Raise 409, naming the organization, when member_email is a partner of org_id and OrcaHub syncs it."""
    synced = await synced_partner_orgs(db, member_email, [org_id])
    if synced:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=synced_partner_refusal(member_email, list(synced.values())),
        )
