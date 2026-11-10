"""Whose branding an account sees: one organization, or the deployment default.

An account can belong to several organizations. The one shown is, in order:

  1. an organization the account runs: a partner row, or one it created in this dashboard;
  2. one where it holds access that has not ended (a tenant's live assignment);
  3. the earliest membership, then the lowest id, so the answer is stable.

Only active organizations count, and the address is compared case-insensitively, because
Authentik and the member table disagree on casing. An Administrator without the partner role has
no organization of its own to show, so it sees the deployment default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import (
    Organization,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
)


async def subject_organization_id(db: AsyncSession, email: Optional[str]) -> Optional[int]:
    if not email:
        return None
    now = datetime.now(timezone.utc)
    runs = or_(
        OrganizationMember.role == "partner",
        and_(func.lower(Organization.created_by) == email.lower(), Organization.source == "local"),
    )
    live_access = exists().where(
        TenantPolicyAssignment.org_member_id == OrganizationMember.id,
        TenantPolicyAssignment.policy_id == TenantAccessPolicy.id,
        or_(TenantPolicyAssignment.expires_at.is_(None), TenantPolicyAssignment.expires_at > now),
        or_(TenantAccessPolicy.access_ends_at.is_(None), TenantAccessPolicy.access_ends_at > now),
    )
    row = (await db.execute(
        select(Organization.id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            func.lower(OrganizationMember.user_email) == email.lower(),
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
        )
        .order_by(
            case((runs, 0), else_=1),
            case((live_access, 0), else_=1),
            OrganizationMember.joined_at.asc(),
            Organization.id.asc(),
        )
        .limit(1)
    )).scalar_one_or_none()
    return row
