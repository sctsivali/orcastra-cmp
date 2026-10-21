"""What the signed-in caller may do here, for the dashboard's access notice (issue #17).

A user Authentik signs in but Orcastra has not assigned anything used to land on an empty
dashboard that read as a broken deployment. The frontend asks this before rendering the shell and
shows a notice instead when the answer is a known denial.

important: mounted without a require_* dependency on purpose. Every other session route refuses a
caller with no role (rbac.refuse_unmapped), and this is the one place that caller can learn why.
It answers about the caller only and reads nothing the caller could not already see.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core import org_scope
from app.core.rbac import NO_ROLE_ASSIGNED, Role, get_highest_role, mapped_roles

logger = logging.getLogger(__name__)

NO_TENANT_ACCESS = "no_tenant_access"

router = APIRouter(prefix="/me", tags=["Me"])


class TenantAccessSummary(BaseModel):
    total_clusters: int
    total_rules: int


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
    # note: a role can be granted or a policy expire at any moment, so nothing here is cached.
    response.headers["Cache-Control"] = "no-store"
    if user is None:
        # Auth disabled: every RBAC dependency treats the caller as an Administrator.
        return MyAccessVerdict(email=None, role=Role.ADMIN.value, has_role=True, blocked_reason=None)

    roles = mapped_roles(user.groups)
    if not roles:
        return MyAccessVerdict(
            email=user.email, role=None, has_role=False, blocked_reason=NO_ROLE_ASSIGNED
        )

    highest = get_highest_role(roles)
    if highest != Role.TENANT:
        return MyAccessVerdict(email=user.email, role=highest.value, has_role=True, blocked_reason=None)

    # important: the same three steps org_scope_dependency takes for every scoped route, so this
    # verdict and what the Tenant can actually reach cannot disagree.
    allowed = await org_scope.get_user_allowed_clusters(user, db)
    acl, grants, _visible_projects = await org_scope.get_tenant_scope(user, db)
    scope = org_scope._narrow_by_tenant_acl(allowed, acl)
    clusters = 0 if scope is None else len(scope)
    rules = len(grants or [])
    return MyAccessVerdict(
        email=user.email,
        role=Role.TENANT.value,
        has_role=True,
        blocked_reason=NO_TENANT_ACCESS if scope is not None and not scope else None,
        tenant_access=TenantAccessSummary(total_clusters=clusters, total_rules=rules),
    )
