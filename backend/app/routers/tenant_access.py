"""
Tenant Access Policy Router.

Provides CRUD endpoints for managing tenant access policies (ACL).
Follows AWS IAM / Azure RBAC pattern:
  1. Admin/Partner creates named policies with rules.
  2. Policies are assigned to organization members (tenants).
  3. Backend enforces cluster/project/instance access per policy rules.

Permissions model:
  - view:          See resource in dashboards, read monitoring
  - operate:       Start, stop, restart, freeze/unfreeze VMs
  - console:       Open graphical console or terminal
  - snapshot:      Create, restore, delete snapshots
  - file_transfer: Browse, change, upload and download files in an instance, as root
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, and_, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.dependencies import require_partner, require_tenant, check_read_only_mode
from app.core.org_scope import (
    org_scope_dependency,
    get_current_allowed_clusters,
    get_current_resolved_tenant_grants,
    get_tenant_scope,
    project_is_visible,
    refuse_default_operation,
)
from app.core.rbac import resolve_roles, get_highest_role, Role
from app.core.database import get_db
from app.core.cache import get_cache_manager
from app.core.config import get_settings
from app.core.tenant_term import policy_phase
from app.core.sale_term import sold_to_without_registering
from app.models.db_models import (
    Organization,
    OrganizationMember,
    OrganizationCluster,
    TenantAccessPolicy,
    TenantPolicyRule,
    TenantPolicyAssignment,
)
from app.services.migration_fence import (
    MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL,
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
    assert_project_not_migration_fenced,
    guard_migration_mutations,
)

logger = logging.getLogger(__name__)

def _aware(value: datetime) -> datetime:
    # note: SQLite test sessions hand back naive values for timestamptz columns; they were UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# valid permission types for tenant policies
VALID_PERMISSIONS = {"view", "operate", "console", "snapshot", "file_transfer"}


def _invalidate_acl_caches():
    """Flush cached API responses that depend on tenant ACL filtering.

    Called after any policy/assignment mutation so tenants immediately
    see updated access.
    """
    try:
        # ACL changes are mutations of the visible inventory. The helper is generation-aware, so
        # an older in-flight tenant read cannot restore its pre-change projection afterwards, and
        # it clears the cluster, region and monitoring lists a grant also shapes.
        get_cache_manager().invalidate_after_access_change()
        logger.info("Invalidated ACL-dependent API response caches")
    except Exception as e:
        logger.warning(f"Cache invalidation failed (non-fatal): {e}")

# ============================================
# Pydantic Schemas
# ============================================

class PolicyRuleCreate(BaseModel):
    """Request schema for creating a rule within a policy."""
    cluster_id: str = Field(..., min_length=1, max_length=255, description="Target cluster ID")
    project_name: str = Field(
        default="*",
        max_length=255,
        description=(
            "Project name or '*' for projects attributed to this policy's organization"
        ),
    )
    instance_name: str = Field(default="*", max_length=255, description="Instance name or '*' for all")
    permissions: List[str] = Field(
        default=["view"],
        description="Permission list: view, operate, console, snapshot, file_transfer",
    )

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, v: List[str]) -> List[str]:
        invalid = set(v) - VALID_PERMISSIONS
        if invalid:
            raise ValueError(f"Invalid permissions: {invalid}. Valid: {sorted(VALID_PERMISSIONS)}")
        if not v:
            raise ValueError("At least one permission is required")
        return sorted(set(v))


class PolicyRuleResponse(BaseModel):
    """Response schema for a policy rule."""
    id: int
    cluster_id: str
    project_name: str
    instance_name: str
    permissions: List[str]

    class Config:
        from_attributes = True


class PolicyCreate(BaseModel):
    """Request schema for creating a policy."""
    name: str = Field(..., min_length=1, max_length=100, description="Policy name (unique per org)")
    description: Optional[str] = Field(None, max_length=500)
    organization_id: int = Field(..., description="Organization ID")
    is_default: bool = Field(default=False, description="Auto-assign to new tenants in org")
    rules: List[PolicyRuleCreate] = Field(default_factory=list, description="Access rules")


class PolicyUpdate(BaseModel):
    """Request schema for updating a policy."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    is_default: Optional[bool] = None
    rules: Optional[List[PolicyRuleCreate]] = None


class PolicyResponse(BaseModel):
    """Response schema for a policy."""
    id: int
    organization_id: int
    name: str
    description: Optional[str]
    is_default: bool
    source: str
    external_id: Optional[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    rules: List[PolicyRuleResponse] = []
    assignments_count: int = 0
    # The paid term the policy stands for (core/tenant_term.py), null when none was given.
    term_ended_at: Optional[datetime] = None
    access_ends_at: Optional[datetime] = None
    phase: str = "active"
    phase_inferred: bool = False

    class Config:
        from_attributes = True


class PolicyListResponse(BaseModel):
    """Response for listing policies."""
    policies: List[PolicyResponse]
    total: int


class AssignmentCreate(BaseModel):
    """Request schema for assigning a policy to a tenant."""
    policy_id: int = Field(..., description="Policy to assign")
    user_email: str = Field(..., min_length=1, description="Tenant user email")
    organization_id: int = Field(..., description="Organization context")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration (null=never)")


class AssignmentResponse(BaseModel):
    """Response schema for a policy assignment."""
    id: int
    policy_id: int
    policy_name: str
    org_member_id: int
    user_email: str
    organization_id: int
    organization_name: str
    assigned_by: str
    assigned_at: datetime
    expires_at: Optional[datetime]
    source: str


class AssignmentListResponse(BaseModel):
    """Response for listing assignments."""
    assignments: List[AssignmentResponse]
    total: int


class ResolvedAccess(BaseModel):
    """A single resolved access entry for a tenant."""
    cluster_id: str
    project_name: str
    instance_name: str
    permissions: List[str]
    policy_name: str
    # Why `permissions` may be narrower than the rule: the policy's paid term (core/tenant_term.py).
    read_only: bool = False
    term_ended_at: Optional[datetime] = None
    access_ends_at: Optional[datetime] = None


class MyAccessResponse(BaseModel):
    """Response for tenant's resolved access view."""
    access: List[ResolvedAccess]
    total_clusters: int
    total_rules: int


# ============================================
# Router Setup
# ============================================

router = APIRouter(
    prefix="/tenant-access",
    tags=["Tenant Access Policies"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


# ============================================
# Helper Functions
# ============================================

async def _verify_org_access(
    user: AuthenticatedUser,
    org_id: int,
    db: AsyncSession,
    require_write: bool = False,
) -> Organization:
    """Verify user can access the organization. Returns the org."""
    org = await db.get(Organization, org_id)
    if not org or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")

    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    # admin can access any org
    if highest == Role.ADMIN:
        return org

    # A global Partner role alone does not grant authority over another organization's ACL.
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=403, detail="Access denied to this organization")
    is_creator = bool(email) and (org.created_by or "").strip().lower() == email
    member_stmt = select(OrganizationMember.role).where(
        and_(
            OrganizationMember.organization_id == org_id,
            func.lower(func.trim(OrganizationMember.user_email)) == email,
        )
    )
    result = await db.execute(member_stmt)
    member_roles = result.scalars().all()

    if not is_creator and not member_roles:
        raise HTTPException(status_code=403, detail="Access denied to this organization")

    if require_write:
        if highest == Role.TENANT:
            raise HTTPException(status_code=403, detail="Tenant users cannot manage access policies")
        if not is_creator and not any((role or "").casefold() == "partner" for role in member_roles):
            raise HTTPException(status_code=403, detail="Only organization Partners can manage access policies")

    return org


async def _policy_to_response(policy: TenantAccessPolicy) -> PolicyResponse:
    """Convert ORM model to response schema."""
    rule_permissions = {
        permission
        for rule in (policy.rules or [])
        for permission in (rule.permissions if isinstance(rule.permissions, list) else [])
    }
    term = policy_phase(
        source=policy.source,
        term_ended_at=getattr(policy, "term_ended_at", None),
        access_ends_at=getattr(policy, "access_ends_at", None),
        permissions=rule_permissions,
        now=datetime.now(timezone.utc),
    )
    return PolicyResponse(
        id=policy.id,
        organization_id=policy.organization_id,
        name=policy.name,
        description=policy.description,
        is_default=policy.is_default,
        source=policy.source,
        external_id=policy.external_id,
        created_by=policy.created_by,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        rules=[
            PolicyRuleResponse(
                id=r.id,
                cluster_id=r.cluster_id,
                project_name=r.project_name,
                instance_name=r.instance_name,
                permissions=r.permissions if isinstance(r.permissions, list) else [],
            )
            for r in (policy.rules or [])
        ],
        assignments_count=len(policy.assignments or []),
        term_ended_at=term.term_ended_at,
        access_ends_at=term.access_ends_at,
        phase=term.phase,
        phase_inferred=term.inferred,
    )


# ============================================
# Policy CRUD Endpoints
# ============================================

@router.get(
    "/policies",
    response_model=PolicyListResponse,
    summary="List access policies",
    description="List tenant access policies for the current user's organizations.",
)
async def list_policies(
    organization_id: Optional[int] = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List policies, optionally filtered by organization."""
    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    stmt = (
        select(TenantAccessPolicy)
        .options(
            selectinload(TenantAccessPolicy.rules),
            selectinload(TenantAccessPolicy.assignments),
        )
    )

    if organization_id:
        # verify access to the specific org
        await _verify_org_access(user, organization_id, db)
        stmt = stmt.where(TenantAccessPolicy.organization_id == organization_id)
    elif highest != Role.ADMIN:
        # non-admin: only policies from their orgs
        member_orgs = (
            select(OrganizationMember.organization_id)
            .where(OrganizationMember.user_email == user.email)
        )
        created_orgs = (
            select(Organization.id)
            .where(Organization.created_by == user.email)
        )
        stmt = stmt.where(
            TenantAccessPolicy.organization_id.in_(
                member_orgs.union(created_orgs)
            )
        )

    stmt = stmt.order_by(TenantAccessPolicy.organization_id, TenantAccessPolicy.name)
    if highest == Role.TENANT:
        # important: a TENANT sees only their own assignments here. Scoping stopped at the
        # organisation, which handed every tenant the org's full access matrix and, on the
        # assignments endpoint, every other member's email. `get_my_access` already exists as
        # the tenant-facing view, which is the clue that these list endpoints were never meant
        # for them. Partner and Admin keep the org-wide view they administer with.
        stmt = stmt.where(
            TenantAccessPolicy.assignments.any(
                TenantPolicyAssignment.org_member_id.in_(
                    select(OrganizationMember.id).where(
                        OrganizationMember.user_email == user.email
                    )
                )
            )
        )

    result = await db.execute(stmt)
    policies = result.scalars().unique().all()

    return PolicyListResponse(
        policies=[await _policy_to_response(p) for p in policies],
        total=len(policies),
    )



def _refuse_a_project_the_caller_cannot_see(rule) -> None:
    """A policy rule may not name a project the caller is not already allowed to see.

    important: on a cluster carrying more than one organization, `get_visible_projects` builds
    a Partner's project bound out of exactly these rows. Without this check the party the bound
    constrains is the party that writes it: a Partner could add a rule naming another
    organization's project on a cluster their own org legitimately holds, and on the next
    request that project would be inside their bound, granting read and write on its networks,
    ACLs, profiles and instance devices.

    `project_is_visible` answers True whenever no bound applies, so this is a no-op for an
    Admin and on any cluster bound to a single organization. It only bites where the bound is
    real, and there it refuses to let the bound widen itself.

    The consequence to accept: on a shared cluster a Partner cannot introduce a project that no
    rule of theirs names yet. An Admin can, because no bound applies to them.
    """
    project = (rule.project_name or "").strip()
    # A rule in a view-only `default` would hand a Partner's tenant the other party's guests there,
    # and `*` reaches `default` too (RC5 follow-up, owner decision 2026-11-06).
    refuse_default_operation(rule.cluster_id, project if project and project != "*" else "default")
    if not project or project == "*":
        return
    if not project_is_visible(rule.cluster_id, project):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Project '{project}' is not visible to you on cluster "
                f"'{rule.cluster_id}', so a rule cannot name it."
            ),
        )


@asynccontextmanager
async def _guard_rules_mutation(
    rules: List[object],
    db: AsyncSession,
    *,
    policy_id: Optional[int] = None,
) -> AsyncIterator[None]:
    """Lock a policy, then keep its current and proposed grants stable through commit."""
    current_rules: List[object] = []
    if policy_id is not None:
        try:
            locked_policy_id = (
                await db.execute(
                    select(TenantAccessPolicy.id)
                    .where(TenantAccessPolicy.id == policy_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_policy_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="The access policy changed concurrently; reload and retry",
                )
            current_rules = list(await _policy_rules(db, policy_id))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - authorization admission fails closed
            logger.error(
                "Could not lock tenant access policy %s for migration admission: %s",
                policy_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=409,
                detail=MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL,
            ) from exc

    clusters: set[str] = set()
    projects: set[tuple[str, str]] = set()
    for rule in [*current_rules, *rules]:
        cluster_id = str(getattr(rule, "cluster_id", "") or "")
        project = str(getattr(rule, "project_name", "") or "*")
        if not cluster_id:
            continue
        if project == "*":
            clusters.add(cluster_id)
        else:
            projects.add((cluster_id, project))
    if not clusters and not projects:
        yield
        return
    try:
        async with guard_migration_mutations(
            db,
            clusters=clusters,
            projects=projects,
        ):
            yield
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


async def _assert_rules_not_migration_fenced(
    rules: List[object],
    db: AsyncSession,
) -> None:
    """Compatibility probe; mutating callers use the serialized context above."""
    seen: set[tuple[str, str]] = set()
    try:
        for rule in rules:
            cluster_id = str(getattr(rule, "cluster_id", "") or "")
            project = str(getattr(rule, "project_name", "") or "*")
            target = (cluster_id, project)
            if not cluster_id or target in seen:
                continue
            seen.add(target)
            if project == "*":
                await assert_cluster_not_migration_fenced(
                    cluster_id=cluster_id,
                    db=db,
                )
            else:
                await assert_project_not_migration_fenced(
                    cluster_id=cluster_id,
                    project=project,
                    db=db,
                )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


async def _policy_rules(db: AsyncSession, policy_id: int) -> List[TenantPolicyRule]:
    result = await db.execute(
        select(TenantPolicyRule).where(TenantPolicyRule.policy_id == policy_id)
    )
    return list(result.scalars().all())


async def _refuse_rules_a_purchase_does_not_pay_for(
    db: AsyncSession, user: AuthenticatedUser, organization_id: int, cluster_ids: list[str],
) -> None:
    """A buyer organization reaches a sold cluster through its purchase, whose policies come from
    OrcaHub with the paid term on them. A rule written here would grant access no purchase pays for
    and that no term ends, so only the seller's side or an Administrator may write one.
    """
    if get_highest_role(resolve_roles(user)) == Role.ADMIN:
        return
    refused = await sold_to_without_registering(db, organization_id, cluster_ids, user.email)
    if refused:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Cluster '{sorted(refused)[0]}' reaches this organization through a purchase. Its "
                "access comes from the purchase and ends with it, so a rule there can only be written "
                "by the seller or an Administrator."
            ),
        )


@router.post(
    "/policies",
    response_model=PolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create access policy",
    dependencies=[Depends(require_partner()), Depends(check_read_only_mode)],
)
async def create_policy(
    body: PolicyCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tenant access policy with rules."""
    org = await _verify_org_access(user, body.organization_id, db, require_write=True)
    await _refuse_rules_a_purchase_does_not_pay_for(db, user, org.id, [r.cluster_id for r in body.rules])

    # validate cluster_ids in rules against org's clusters
    allowed_clusters = get_current_allowed_clusters()
    org_cluster_stmt = select(OrganizationCluster.cluster_id).where(
        OrganizationCluster.organization_id == org.id
    )
    result = await db.execute(org_cluster_stmt)
    org_cluster_ids = set(result.scalars().all())

    for rule in body.rules:
        if rule.cluster_id not in org_cluster_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Cluster '{rule.cluster_id}' is not part of organization '{org.name}'",
            )
        if allowed_clusters is not None and rule.cluster_id not in allowed_clusters:
            raise HTTPException(
                status_code=403,
                detail=f"You don't have access to cluster '{rule.cluster_id}'",
            )
        _refuse_a_project_the_caller_cannot_see(rule)

    async with _guard_rules_mutation(list(body.rules), db):
        # check name uniqueness after admission so two policy writes cannot validate against
        # different migration revisions and then commit in the opposite order.
        existing = await db.execute(
            select(TenantAccessPolicy).where(
                and_(
                    TenantAccessPolicy.organization_id == org.id,
                    TenantAccessPolicy.name == body.name,
                )
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Policy '{body.name}' already exists in this organization")

        policy = TenantAccessPolicy(
            organization_id=org.id,
            name=body.name,
            description=body.description,
            is_default=body.is_default,
            created_by=user.email,
        )
        db.add(policy)
        await db.flush()  # get policy.id

        for rule_data in body.rules:
            db.add(TenantPolicyRule(
                policy_id=policy.id,
                cluster_id=rule_data.cluster_id,
                project_name=rule_data.project_name,
                instance_name=rule_data.instance_name,
                permissions=rule_data.permissions,
            ))

        await db.commit()
        await db.refresh(policy)
    _invalidate_acl_caches()

    logger.info(f"Policy '{body.name}' created by {user.email} in org {org.slug}")
    return await _policy_to_response(policy)


@router.get(
    "/policies/{policy_id}",
    response_model=PolicyResponse,
    summary="Get access policy",
)
async def get_policy(
    policy_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single policy with its rules."""
    stmt = (
        select(TenantAccessPolicy)
        .options(
            selectinload(TenantAccessPolicy.rules),
            selectinload(TenantAccessPolicy.assignments),
        )
        .where(TenantAccessPolicy.id == policy_id)
    )
    result = await db.execute(stmt)
    policy = result.scalar_one_or_none()

    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    await _verify_org_access(user, policy.organization_id, db)
    if get_highest_role(resolve_roles(user)) == Role.TENANT:
        assignment_stmt = (
            select(TenantPolicyAssignment.id)
            .join(OrganizationMember, TenantPolicyAssignment.org_member_id == OrganizationMember.id)
            .where(
                TenantPolicyAssignment.policy_id == policy.id,
                OrganizationMember.organization_id == policy.organization_id,
                OrganizationMember.user_email == user.email,
            )
            .limit(1)
        )
        if (await db.execute(assignment_stmt)).scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Policy not found")
    return await _policy_to_response(policy)


@router.put(
    "/policies/{policy_id}",
    response_model=PolicyResponse,
    summary="Update access policy",
    dependencies=[Depends(require_partner()), Depends(check_read_only_mode)],
)
async def update_policy(
    policy_id: int,
    body: PolicyUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a policy's name, description, and/or rules."""
    stmt = (
        select(TenantAccessPolicy)
        .options(selectinload(TenantAccessPolicy.rules))
        .where(TenantAccessPolicy.id == policy_id)
    )
    result = await db.execute(stmt)
    policy = result.scalar_one_or_none()

    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    await _verify_org_access(user, policy.organization_id, db, require_write=True)
    if body.rules is not None:
        await _refuse_rules_a_purchase_does_not_pay_for(
            db, user, policy.organization_id, [r.cluster_id for r in body.rules],
        )

    async with _guard_rules_mutation(
        list(body.rules or []),
        db,
        policy_id=policy_id,
    ):
        if body.name is not None:
            dup = await db.execute(
                select(TenantAccessPolicy).where(
                    and_(
                        TenantAccessPolicy.organization_id == policy.organization_id,
                        TenantAccessPolicy.name == body.name,
                        TenantAccessPolicy.id != policy_id,
                    )
                )
            )
            if dup.scalar_one_or_none():
                raise HTTPException(status_code=409, detail=f"Policy '{body.name}' already exists")
            policy.name = body.name

        if body.description is not None:
            policy.description = body.description
        if body.is_default is not None:
            policy.is_default = body.is_default

        if body.rules is not None:
            allowed_clusters = get_current_allowed_clusters()
            org_cluster_stmt = select(OrganizationCluster.cluster_id).where(
                OrganizationCluster.organization_id == policy.organization_id
            )
            result = await db.execute(org_cluster_stmt)
            org_cluster_ids = set(result.scalars().all())

            for rule in body.rules:
                if rule.cluster_id not in org_cluster_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cluster '{rule.cluster_id}' is not in this organization",
                    )
                if allowed_clusters is not None and rule.cluster_id not in allowed_clusters:
                    raise HTTPException(
                        status_code=403,
                        detail=f"No access to cluster '{rule.cluster_id}'",
                    )
                _refuse_a_project_the_caller_cannot_see(rule)

            await db.execute(
                delete(TenantPolicyRule).where(TenantPolicyRule.policy_id == policy_id)
            )
            for rule_data in body.rules:
                db.add(TenantPolicyRule(
                    policy_id=policy_id,
                    cluster_id=rule_data.cluster_id,
                    project_name=rule_data.project_name,
                    instance_name=rule_data.instance_name,
                    permissions=rule_data.permissions,
                ))

        policy.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(policy)
    _invalidate_acl_caches()

    logger.info(f"Policy '{policy.name}' (id={policy_id}) updated by {user.email}")
    return await _policy_to_response(policy)


@router.delete(
    "/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete access policy",
    dependencies=[Depends(require_partner()), Depends(check_read_only_mode)],
)
async def delete_policy(
    policy_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a policy and all its rules/assignments (CASCADE)."""
    policy = await db.get(TenantAccessPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    await _verify_org_access(user, policy.organization_id, db, require_write=True)

    async with _guard_rules_mutation([], db, policy_id=policy_id):
        await db.delete(policy)
        await db.commit()
    _invalidate_acl_caches()
    logger.info(f"Policy '{policy.name}' (id={policy_id}) deleted by {user.email}")


# ============================================
# Assignment Endpoints
# ============================================

@router.get(
    "/assignments",
    response_model=AssignmentListResponse,
    summary="List policy assignments",
)
async def list_assignments(
    organization_id: Optional[int] = None,
    user_email: Optional[str] = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all policy assignments for the current user's organizations."""
    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    stmt = (
        select(TenantPolicyAssignment)
        .join(TenantAccessPolicy, TenantPolicyAssignment.policy_id == TenantAccessPolicy.id)
        .join(OrganizationMember, TenantPolicyAssignment.org_member_id == OrganizationMember.id)
        .join(Organization, TenantAccessPolicy.organization_id == Organization.id)
    )

    if organization_id:
        await _verify_org_access(user, organization_id, db)
        stmt = stmt.where(TenantAccessPolicy.organization_id == organization_id)
    elif highest != Role.ADMIN:
        member_orgs = (
            select(OrganizationMember.organization_id)
            .where(OrganizationMember.user_email == user.email)
        )
        created_orgs = (
            select(Organization.id)
            .where(Organization.created_by == user.email)
        )
        stmt = stmt.where(
            TenantAccessPolicy.organization_id.in_(
                member_orgs.union(created_orgs)
            )
        )

    if user_email:
        stmt = stmt.where(OrganizationMember.user_email == user_email)

    if highest == Role.TENANT:
        # important: a TENANT sees only their own assignments here. Scoping stopped at the
        # organisation, which handed every tenant the org's full access matrix and, on the
        # assignments endpoint, every other member's email. `get_my_access` already exists as
        # the tenant-facing view, which is the clue that these list endpoints were never meant
        # for them. Partner and Admin keep the org-wide view they administer with.
        stmt = stmt.where(OrganizationMember.user_email == user.email)

    result = await db.execute(stmt)
    assignments = result.scalars().unique().all()

    responses = []
    for a in assignments:
        # load related objects
        policy = await db.get(TenantAccessPolicy, a.policy_id)
        member = await db.get(OrganizationMember, a.org_member_id)
        org = await db.get(Organization, policy.organization_id) if policy else None

        if policy and member and org:
            responses.append(AssignmentResponse(
                id=a.id,
                policy_id=a.policy_id,
                policy_name=policy.name,
                org_member_id=a.org_member_id,
                user_email=member.user_email,
                organization_id=org.id,
                organization_name=org.name,
                assigned_by=a.assigned_by,
                assigned_at=a.assigned_at,
                expires_at=a.expires_at,
                source=a.source,
            ))

    return AssignmentListResponse(assignments=responses, total=len(responses))


@router.post(
    "/assignments",
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign policy to tenant",
    dependencies=[Depends(require_partner()), Depends(check_read_only_mode)],
)
async def create_assignment(
    body: AssignmentCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Assign an access policy to a tenant user."""
    # verify policy exists and belongs to the org
    policy = await db.get(TenantAccessPolicy, body.policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    if policy.organization_id != body.organization_id:
        raise HTTPException(status_code=400, detail="Policy does not belong to the specified organization")

    org = await _verify_org_access(user, body.organization_id, db, require_write=True)

    async with _guard_rules_mutation([], db, policy_id=policy.id):
        member_stmt = select(OrganizationMember).where(
            and_(
                OrganizationMember.organization_id == body.organization_id,
                OrganizationMember.user_email == body.user_email,
            )
        )
        result = await db.execute(member_stmt)
        member = result.scalar_one_or_none()

        if not member:
            raise HTTPException(
                status_code=404,
                detail=f"User '{body.user_email}' is not a member of organization '{org.name}'",
            )

        existing = await db.execute(
            select(TenantPolicyAssignment).where(
                and_(
                    TenantPolicyAssignment.policy_id == body.policy_id,
                    TenantPolicyAssignment.org_member_id == member.id,
                )
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="This policy is already assigned to this user")

        assignment = TenantPolicyAssignment(
            policy_id=body.policy_id,
            org_member_id=member.id,
            assigned_by=user.email,
            expires_at=body.expires_at,
        )
        db.add(assignment)
        await db.commit()
        await db.refresh(assignment)
    _invalidate_acl_caches()

    logger.info(f"Policy '{policy.name}' assigned to {body.user_email} by {user.email}")
    return AssignmentResponse(
        id=assignment.id,
        policy_id=assignment.policy_id,
        policy_name=policy.name,
        org_member_id=assignment.org_member_id,
        user_email=body.user_email,
        organization_id=org.id,
        organization_name=org.name,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
        expires_at=assignment.expires_at,
        source=assignment.source,
    )


@router.delete(
    "/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke policy assignment",
    dependencies=[Depends(require_partner()), Depends(check_read_only_mode)],
)
async def delete_assignment(
    assignment_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a policy assignment from a tenant."""
    assignment = await db.get(TenantPolicyAssignment, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    policy = await db.get(TenantAccessPolicy, assignment.policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Associated policy not found")

    member = await db.get(OrganizationMember, assignment.org_member_id)
    is_self = (
        member is not None
        and member.organization_id == policy.organization_id
        and bool((user.email or "").strip())
        and (member.user_email or "").strip().casefold() == (user.email or "").strip().casefold()
    )
    await _verify_org_access(user, policy.organization_id, db, require_write=not is_self)

    async with _guard_rules_mutation([], db, policy_id=policy.id):
        await db.delete(assignment)
        await db.commit()
    _invalidate_acl_caches()
    logger.info(f"Assignment {assignment_id} revoked by {user.email}")


# ============================================
# Tenant Self-View
# ============================================

@router.get(
    "/my-access",
    response_model=MyAccessResponse,
    summary="Get my resolved access",
    description="Returns the resolved access rules for the current tenant user.",
)
async def get_my_access(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve all access rules for the current user across all assigned policies."""
    highest = get_highest_role(resolve_roles(user))
    if highest == Role.TENANT:
        # Use the same active, organization-matched, cluster-bound grants as enforcement. The
        # former self-view independently loaded memberships and assignments, so it could show
        # an expired, inactive, cross-organization, or unbound rule that authorization had
        # already revoked. On shared clusters a wildcard is expanded to the concrete project
        # bound so the response never claims access to a neighbour's project.
        grants = get_current_resolved_tenant_grants()
        if grants is None:
            # Direct function calls in tests and internal tools do not necessarily execute the
            # router dependency. Production requests reuse its exact authorization snapshot so
            # a concurrent revoke cannot make enforcement and this self-view disagree.
            _acl, resolved_grants, _visible_projects = await get_tenant_scope(user, db)
            grants = tuple(resolved_grants or [])
        access_entries: List[ResolvedAccess] = []
        for grant in grants:
            access_entries.append(
                ResolvedAccess(
                    cluster_id=grant.cluster_id,
                    project_name=grant.project_name,
                    instance_name=grant.instance_name,
                    permissions=sorted(grant.permissions),
                    policy_name=grant.policy_name,
                    read_only=grant.read_only,
                    term_ended_at=grant.term_ended_at,
                    access_ends_at=grant.access_ends_at,
                )
            )

        return MyAccessResponse(
            access=access_entries,
            total_clusters=len({entry.cluster_id for entry in access_entries}),
            total_rules=len(access_entries),
        )

    now = datetime.now(timezone.utc)

    # find all org memberships
    member_stmt = select(OrganizationMember).where(
        OrganizationMember.user_email == user.email
    )
    result = await db.execute(member_stmt)
    memberships = result.scalars().all()

    if not memberships:
        return MyAccessResponse(access=[], total_clusters=0, total_rules=0)

    member_ids = [m.id for m in memberships]

    # find all active (non-expired) policy assignments
    assign_stmt = (
        select(TenantPolicyAssignment)
        .where(
            TenantPolicyAssignment.org_member_id.in_(member_ids),
        )
    )
    result = await db.execute(assign_stmt)
    assignments = result.scalars().all()

    # filter expired
    active_assignments = [
        a for a in assignments
        if a.expires_at is None or a.expires_at > now
    ]

    if not active_assignments:
        return MyAccessResponse(access=[], total_clusters=0, total_rules=0)

    # load policies with rules
    policy_ids = list({a.policy_id for a in active_assignments})
    policies_stmt = (
        select(TenantAccessPolicy)
        .options(selectinload(TenantAccessPolicy.rules))
        .where(TenantAccessPolicy.id.in_(policy_ids))
    )
    result = await db.execute(policies_stmt)
    policies = {p.id: p for p in result.scalars().unique().all()}
    if get_settings().tenant_term_enforcement_enabled:
        # Mirrors the grant query: a policy whose access has ended grants nothing.
        policies = {
            pid: p for pid, p in policies.items()
            if getattr(p, "access_ends_at", None) is None or _aware(p.access_ends_at) > now
        }

    # build resolved access
    access_entries = []
    cluster_ids = set()
    for assignment in active_assignments:
        policy = policies.get(assignment.policy_id)
        if not policy:
            continue
        for rule in (policy.rules or []):
            cluster_ids.add(rule.cluster_id)
            access_entries.append(ResolvedAccess(
                cluster_id=rule.cluster_id,
                project_name=rule.project_name,
                instance_name=rule.instance_name,
                permissions=rule.permissions if isinstance(rule.permissions, list) else [],
                policy_name=policy.name,
            ))

    return MyAccessResponse(
        access=access_entries,
        total_clusters=len(cluster_ids),
        total_rules=len(access_entries),
    )
