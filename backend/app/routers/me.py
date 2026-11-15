"""What the signed-in caller may do here, for the dashboard's access notice.

A user Authentik signs in but Orcastra has not assigned anything would otherwise land on an
empty dashboard that reads as a broken deployment. The frontend asks this before rendering the
shell and shows a notice instead when the answer is a known denial.

important: mounted without a require_* dependency. Every other session route refuses a caller
with no role (rbac.refuse_unmapped), and this is the one place that caller can learn why. A caller
with no role group that an active organization lists as a member is a Tenant here as there.
It answers about the caller only and reads nothing the caller could not already see.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core import org_scope, session_role
from app.core.rbac import NO_ROLE_ASSIGNED, Role, get_highest_role, mapped_roles
from app.core.tenant_term import policy_phase

logger = logging.getLogger(__name__)

NO_TENANT_ACCESS = "no_tenant_access"

router = APIRouter(prefix="/me", tags=["Me"])


class TermItem(BaseModel):
    """One project whose access is limited or ending because of its paid term."""

    cluster_id: str
    project_name: str
    phase: Literal["ending", "read_only"]
    inferred: bool = False
    term_ended_at: Optional[datetime] = None
    access_ends_at: Optional[datetime] = None


class TenantAccessSummary(BaseModel):
    total_clusters: int
    total_rules: int
    # The paid term behind the caller's own grants (core/tenant_term.py); optional, so an older
    # client reads the verdict unchanged.
    phase: Literal["active", "ending", "read_only", "ended"] = "active"
    items: List[TermItem] = []
    ended_at: Optional[datetime] = None


class MyAccessVerdict(BaseModel):
    email: Optional[str]
    role: Optional[Literal["admin", "partner", "tenant"]]
    has_role: bool
    blocked_reason: Optional[Literal["no_role_assigned", "no_tenant_access"]]
    tenant_access: Optional[TenantAccessSummary] = None


@router.get("/access", response_model=MyAccessVerdict, summary="Whether this session has access")
async def get_my_access_verdict(
    response: Response,
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyAccessVerdict:
    # A role can be granted or a policy expire at any moment, so nothing here is cached.
    response.headers["Cache-Control"] = "no-store"
    if user is None:
        # Auth disabled: every RBAC dependency treats the caller as an Administrator.
        return MyAccessVerdict(email=None, role=Role.ADMIN.value, has_role=True, blocked_reason=None)

    roles = mapped_roles(user.groups)
    if not roles:
        # A groupless member of an active organization is a Tenant, as on every session route
        # (rbac.session_has_role); its verdict below is the Tenant one, scope and term included.
        if not await session_role.holds_membership(user.email, db):
            return MyAccessVerdict(
                email=user.email, role=None, has_role=False, blocked_reason=NO_ROLE_ASSIGNED
            )
        roles = [Role.TENANT]

    highest = get_highest_role(roles)
    if highest != Role.TENANT:
        return MyAccessVerdict(email=user.email, role=highest.value, has_role=True, blocked_reason=None)

    # important: the same three steps org_scope_dependency takes for every scoped route, so this
    # verdict cannot disagree with what the Tenant can reach.
    allowed = await org_scope.get_user_allowed_clusters(user, db)
    acl, grants, _visible_projects = await org_scope.get_tenant_scope(user, db)
    scope = org_scope._narrow_by_tenant_acl(allowed, acl)
    clusters = 0 if scope is None else len(scope)
    rules = len(grants or [])
    blocked = scope is not None and not scope
    items = _term_items(grants or [])
    ended_at = await _latest_access_end(user, db) if blocked else None
    if blocked and ended_at is not None:
        phase = "ended"
    elif any(item.phase == "read_only" for item in items):
        phase = "read_only"
    elif items:
        phase = "ending"
    else:
        phase = "active"
    return MyAccessVerdict(
        email=user.email,
        role=Role.TENANT.value,
        has_role=True,
        blocked_reason=NO_TENANT_ACCESS if blocked else None,
        tenant_access=TenantAccessSummary(
            total_clusters=clusters, total_rules=rules, phase=phase, items=items, ended_at=ended_at
        ),
    )


def _term_items(grants: Iterable[org_scope.TenantPolicyGrant]) -> List[TermItem]:
    """The caller's own projects that are read-only or ending, one entry per project."""
    now = datetime.now(timezone.utc)
    grants = list(grants)
    found: Dict[Tuple[str, str], TermItem] = {}
    for grant in grants:
        state = policy_phase(
            source=grant.source,
            term_ended_at=grant.term_ended_at,
            access_ends_at=grant.access_ends_at,
            permissions=grant.permissions,
            now=now,
        )
        if state.phase not in ("ending", "read_only"):
            continue
        key = (grant.cluster_id, grant.project_name)
        current = found.get(key)
        # A project read-only through one policy and still full through another stays full.
        if any(
            other.cluster_id == grant.cluster_id
            and other.project_name == grant.project_name
            and "operate" in other.permissions
            for other in grants
        ) and state.phase == "read_only":
            continue
        if current is None or (current.phase == "ending" and state.phase == "read_only"):
            found[key] = TermItem(
                cluster_id=grant.cluster_id,
                project_name=grant.project_name,
                phase=state.phase,
                inferred=state.inferred,
                term_ended_at=state.term_ended_at,
                access_ends_at=state.access_ends_at,
            )
    return sorted(found.values(), key=lambda item: (item.cluster_id, item.project_name))


async def _latest_access_end(user: AuthenticatedUser, db: AsyncSession) -> Optional[datetime]:
    """When the caller's most recent access ended by its term, for the "access ended" notice."""
    from app.models.db_models import OrganizationMember, TenantAccessPolicy, TenantPolicyAssignment

    now = datetime.now(timezone.utc)
    try:
        latest = (
            await db.execute(
                select(func.max(TenantAccessPolicy.access_ends_at))
                .join(TenantPolicyAssignment, TenantPolicyAssignment.policy_id == TenantAccessPolicy.id)
                .join(OrganizationMember, OrganizationMember.id == TenantPolicyAssignment.org_member_id)
                .where(
                    func.lower(OrganizationMember.user_email) == (user.email or "").lower(),
                    TenantAccessPolicy.access_ends_at <= now,
                )
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - the date is a courtesy on the notice, never a reason to fail it
        logger.warning("Could not read when the caller's access ended", exc_info=True)
        return None
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest
