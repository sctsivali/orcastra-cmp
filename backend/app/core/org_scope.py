"""
Organization-scoped data isolation (multi-tenancy).

Uses a request-scoped context variable to restrict which clusters
(and therefore nodes, instances, networks, etc.) a user can see.

Admin:   sees all clusters (no filter)
Partner: sees clusters bound to their organization(s)
Tenant:  sees clusters bound to assigned access policies (ACL)
"""

import contextvars
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db

logger = logging.getLogger(__name__)

# request-scoped allowed clusters (None = no filter / admin)
_allowed_clusters_var: contextvars.ContextVar[Optional[set[str]]] = contextvars.ContextVar(
    "allowed_clusters", default=None
)

# request-scoped tenant ACL map (None = no ACL / admin or partner)
# format: {cluster_id: {(project_name, instance_name): set[permissions]}}
_tenant_acl_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "tenant_acl", default=None
)


# ---------------------------------------------------------------------------
# Public helpers (called from LXD services)
# ---------------------------------------------------------------------------

def apply_cluster_filter(cluster_ids: list[str]) -> list[str]:
    """
    Filter a list of cluster IDs by the current request's allowed set.
    Returns all clusters if no scope is active (admin / background task).
    """
    allowed = _allowed_clusters_var.get(None)
    if allowed is None:
        return cluster_ids
    return [cid for cid in cluster_ids if cid in allowed]


def get_current_allowed_clusters() -> Optional[set[str]]:
    """Return the current request's allowed clusters (None = all)."""
    return _allowed_clusters_var.get(None)


def get_scope_cache_key(base_key: str) -> str:
    """
    Return a cache key that includes the current org-scope AND tenant ACL.

    Admin / background (allowed=None)  -> ``base_key:scope=all``
    Scoped user                        -> ``base_key:scope=<sha256-of-clusters>:acl=<sha256-of-rules>``

    Including ACL in the key prevents cross-tenant cache poisoning where
    two tenants with the same cluster set but different policies would
    otherwise share a cache entry.
    """
    allowed = _allowed_clusters_var.get(None)
    if allowed is None:
        return f"{base_key}:scope=all"
    if not allowed:
        return f"{base_key}:scope=empty"
    scope_digest = hashlib.sha256(",".join(sorted(allowed)).encode()).hexdigest()[:12]

    # include tenant ACL rules in the cache key
    acl = _tenant_acl_var.get(None)
    if acl is not None:
        # serialize ACL to a stable string for hashing
        acl_parts = []
        for cid in sorted(acl.keys()):
            for (proj, inst), perms in sorted(acl[cid].items()):
                acl_parts.append(f"{cid}:{proj}:{inst}:{','.join(sorted(perms))}")
        acl_digest = hashlib.sha256("|".join(acl_parts).encode()).hexdigest()[:12]
        return f"{base_key}:scope={scope_digest}:acl={acl_digest}"

    return f"{base_key}:scope={scope_digest}"


def verify_cluster_access(cluster_id: str) -> None:
    """
    Raise 403 if the current user cannot access ``cluster_id``.
    No-op for admin / dev mode / background tasks.
    """
    allowed = _allowed_clusters_var.get(None)
    if allowed is None:
        return
    if cluster_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: cluster '{cluster_id}' is not in your organization",
        )


def set_allowed_clusters(clusters: Optional[set[str]]) -> None:
    """Explicitly set the allowed clusters for the current context."""
    _allowed_clusters_var.set(clusters)


# ---------------------------------------------------------------------------
# Tenant ACL helpers (second filtering layer)
# ---------------------------------------------------------------------------

def get_tenant_acl() -> Optional[dict]:
    """
    Return the current request's tenant ACL map, or None if no ACL applies.
    """
    return _tenant_acl_var.get(None)


def check_tenant_permission(
    cluster_id: str,
    project_name: str = "default",
    instance_name: Optional[str] = None,
    required_permission: str = "view",
) -> bool:
    """
    Check if the current tenant has a specific permission for a resource.
    """
    acl = _tenant_acl_var.get(None)
    if acl is None:
        return True  # no ACL = full access (admin/partner)

    cluster_rules = acl.get(cluster_id)
    if not cluster_rules:
        return False

    # collect all matching permissions (merge wildcards + specific)
    merged_perms: set[str] = set()

    for (proj, inst), perms in cluster_rules.items():
        # check project match (wildcard or exact)
        proj_match = (proj == "*") or (proj == project_name)
        if not proj_match:
            continue

        if instance_name is None:
            # project-level check: any rule for this project grants access
            merged_perms.update(perms)
        else:
            # instance-level check
            inst_match = (inst == "*") or (inst == instance_name)
            if inst_match:
                merged_perms.update(perms)

    return required_permission in merged_perms


def verify_tenant_permission(
    cluster_id: str,
    project_name: str = "default",
    instance_name: Optional[str] = None,
    required_permission: str = "view",
) -> None:
    """
    Like check_tenant_permission but raises 403 on failure.
    No-op if no ACL is active (admin/partner).
    """
    if not check_tenant_permission(cluster_id, project_name, instance_name, required_permission):
        resource = f"{cluster_id}/{project_name}"
        if instance_name:
            resource += f"/{instance_name}"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: no '{required_permission}' permission for '{resource}'",
        )


def filter_projects_by_acl(cluster_id: str, project_names: list[str]) -> list[str]:
    """
    Filter a list of project names by the current tenant ACL.
    Returns all projects if no ACL is active.
    """
    acl = _tenant_acl_var.get(None)
    if acl is None:
        return project_names

    cluster_rules = acl.get(cluster_id)
    if not cluster_rules:
        return []

    # check if wildcard project exists
    for (proj, _inst), _perms in cluster_rules.items():
        if proj == "*":
            return project_names  # wildcard = all projects

    # filter to allowed projects
    allowed_projects = {proj for (proj, _inst), _perms in cluster_rules.items()}
    return [p for p in project_names if p in allowed_projects]


def filter_instances_by_acl(
    cluster_id: str,
    project_name: str,
    instance_names: list[str],
) -> list[str]:
    """
    Filter a list of instance names by the current tenant ACL.
    Returns all instances if no ACL is active.
    """
    acl = _tenant_acl_var.get(None)
    if acl is None:
        return instance_names

    cluster_rules = acl.get(cluster_id)
    if not cluster_rules:
        return []

    # check for instance wildcard within this project (or wildcard project)
    for (proj, inst), _perms in cluster_rules.items():
        proj_match = (proj == "*") or (proj == project_name)
        if proj_match and inst == "*":
            return instance_names  # wildcard = all instances

    # filter to specific allowed instances
    allowed_instances: set[str] = set()
    for (proj, inst), _perms in cluster_rules.items():
        proj_match = (proj == "*") or (proj == project_name)
        if proj_match and inst != "*":
            allowed_instances.add(inst)

    return [i for i in instance_names if i in allowed_instances]


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

async def _resolve_user_org_names(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> list[str]:
    """
    Return a list of org names the user belongs to (via membership or creation).
    Admin gets an empty list (they see everything, no specific org binding).
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.models.db_models import Organization, OrganizationMember

    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    if highest == Role.ADMIN:
        return []  # admin is not org-scoped

    stmt = (
        select(Organization.name)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            Organization.is_active == True,
            or_(
                OrganizationMember.user_email == user.email,
                Organization.created_by == user.email,
            ),
        )
        .distinct()
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_user_allowed_clusters(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Optional[set[str]]:
    """
    Query the DB to determine which clusters a user may access.
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.models.db_models import (
        Organization,
        OrganizationMember,
        OrganizationCluster,
    )

    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    if highest == Role.ADMIN:
        return None  # admin sees everything

    # partner / tenant: clusters from orgs they created or belong to
    stmt = (
        select(OrganizationCluster.cluster_id)
        .join(Organization, OrganizationCluster.organization_id == Organization.id)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            Organization.is_active == True,
            or_(
                OrganizationMember.user_email == user.email,
                Organization.created_by == user.email,
            ),
        )
    )
    result = await db.execute(stmt)
    cluster_ids = set(result.scalars().all())
    logger.debug(
        f"Org-scope for {user.email}: allowed_clusters={cluster_ids or '(none)'}"
    )
    return cluster_ids


async def get_tenant_acl_rules(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Optional[dict]:
    """
    Build the resolved ACL map for a tenant user.
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.models.db_models import (
        OrganizationMember,
        TenantPolicyAssignment,
        TenantAccessPolicy,
        TenantPolicyRule,
    )

    roles = resolve_roles(user)
    highest = get_highest_role(roles)

    # admin/partner: no ACL filtering
    if highest in (Role.ADMIN, Role.PARTNER):
        return None

    now = datetime.now(timezone.utc)

    # find all org memberships for this user
    member_stmt = select(OrganizationMember.id).where(
        OrganizationMember.user_email == user.email
    )
    result = await db.execute(member_stmt)
    member_ids = list(result.scalars().all())

    if not member_ids:
        return {}  # no memberships = no access

    # fetch all active assignments
    assign_stmt = (
        select(TenantPolicyAssignment)
        .where(TenantPolicyAssignment.org_member_id.in_(member_ids))
    )
    result = await db.execute(assign_stmt)
    assignments = result.scalars().all()

    # filter expired
    active_policy_ids = list({
        a.policy_id for a in assignments
        if a.expires_at is None or a.expires_at > now
    })

    if not active_policy_ids:
        return {}  # no active policies = no access

    # load policy rules
    rules_stmt = (
        select(TenantPolicyRule)
        .where(TenantPolicyRule.policy_id.in_(active_policy_ids))
    )
    result = await db.execute(rules_stmt)
    rules = result.scalars().all()

    # build ACL map: merge permissions across policies
    acl: dict[str, dict[tuple[str, str], set[str]]] = {}
    for rule in rules:
        cluster_id = rule.cluster_id
        if cluster_id not in acl:
            acl[cluster_id] = {}
        key = (rule.project_name, rule.instance_name)
        perms = rule.permissions if isinstance(rule.permissions, list) else []
        if key not in acl[cluster_id]:
            acl[cluster_id][key] = set()
        acl[cluster_id][key].update(perms)

    logger.debug(
        f"Tenant ACL for {user.email}: {len(acl)} clusters, "
        f"{sum(len(v) for v in acl.values())} rules"
    )
    return acl


# ---------------------------------------------------------------------------
# FastAPI dependency (add to router-level dependencies list)
# ---------------------------------------------------------------------------

async def org_scope_dependency(
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Router-level dependency that sets the org-scope context variable
    and tenant ACL if applicable.
        )
    """
    if user is None:
        # dev mode - no filter
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        return

    allowed = await get_user_allowed_clusters(user, db)
    _allowed_clusters_var.set(allowed)

    # set tenant ACL (None for admin/partner, dict for tenant)
    acl = await get_tenant_acl_rules(user, db)
    _tenant_acl_var.set(acl)

    # for tenants with ACL: further restrict allowed clusters to ACL clusters
    if acl is not None and allowed is not None:
        acl_clusters = set(acl.keys())
        # tenant can only see clusters that are both in org scope AND in ACL
        # if ACL is empty dict, tenant has no access (newly assigned, no policy yet)
        if acl_clusters:
            allowed = allowed.intersection(acl_clusters)
        # else: keep org-scoped clusters (tenant without ACL assignment has org-level view)
        _allowed_clusters_var.set(allowed)

    # push org names into RequestContext for audit logs
    from app.core.request_context import get_request_context
    org_names = await _resolve_user_org_names(user, db)
    ctx = get_request_context()
    if ctx is not None:
        ctx.user_orgs = org_names


async def set_org_scope_for_ws(user: Optional["AuthenticatedUser"]) -> None:
    """
    Set org scope and tenant ACL for WebSocket handlers where DI is unavailable.
    """
    if user is None:
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        return

    from app.core.rbac import resolve_roles, get_highest_role, Role

    roles = resolve_roles(user)
    highest = get_highest_role(roles)
    if highest == Role.ADMIN:
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        return

    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        allowed = await get_user_allowed_clusters(user, db)
        _allowed_clusters_var.set(allowed)

        acl = await get_tenant_acl_rules(user, db)
        _tenant_acl_var.set(acl)

        # for tenants: restrict clusters to ACL scope
        if acl is not None and allowed is not None:
            acl_clusters = set(acl.keys())
            if acl_clusters:
                allowed = allowed.intersection(acl_clusters)
            _allowed_clusters_var.set(allowed)

        # push org names into RequestContext for audit logs
        from app.core.request_context import get_request_context
        org_names = await _resolve_user_org_names(user, db)
        ctx = get_request_context()
        if ctx is not None:
            ctx.user_orgs = org_names
