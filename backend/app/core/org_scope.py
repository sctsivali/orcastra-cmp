"""
Organization-scoped data isolation (multi-tenancy).

Uses a request-scoped context variable to restrict which clusters
(and therefore nodes, instances, networks, etc.) a user can see.

Admin:   sees all clusters (no filter)
Partner: sees clusters bound to their organization(s)
Tenant:  sees clusters bound to assigned access policies (ACL)
"""

import contextvars
import re
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy import select, or_, and_, func, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.config import get_settings
from app.core.tenant_term import clamp, policy_phase
from app.core.sale_term import ENDED as SALE_ENDED, READ_ONLY as SALE_READ_ONLY, latest_access_end, orcahub_terms, sale_phase

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

# request-scoped project visibility for clusters that carry more than one organization.
# format: {cluster_id: set[project_name]}. A cluster absent from the map is unrestricted, and
# None means nothing is restricted at all.
#
# important: this exists because a Partner is scoped by CLUSTER and not by project.
# get_tenant_acl_rules returns None for Partner as well as Admin, so the used_by filter that
# protects the network, ACL and profile lists never runs for them. That was harmless only
# while those lists could not see past the default project. Once they can, a shared cluster
# would hand one partner another partner's network config, ACL rule bodies and instance
# inventory.
_visible_projects_var: contextvars.ContextVar[Optional[dict[str, set[str]]]] = (
    contextvars.ContextVar("visible_projects", default=None)
)

# Clusters a role_partner buyer reaches only through purchases past their paid term: it may read
# them, not change anything there (core/sale_term.py). Empty for everyone else.
_read_only_clusters_var: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "read_only_clusters", default=frozenset()
)
# Set by an interactive socket (console, terminal): driving a guest is a change, not a read.
_interactive_var: contextvars.ContextVar[bool] = contextvars.ContextVar("interactive_scope", default=False)

_READS = frozenset({"GET", "HEAD", "OPTIONS"})
# POSTs that only read, matched as whole paths: a socket ticket (the socket it opens is checked
# again) and waiting on an LXD operation.
_READ_POSTS = r"/(?:ws-tickets|operations/[^/]+/wait)/?$"


def _is_change() -> bool:
    if _interactive_var.get():
        return True
    from app.core.request_context import get_request_context

    ctx = get_request_context()
    if ctx is None or not ctx.method or ctx.method.upper() in _READS:
        return False
    prefix = re.escape(get_settings().api_prefix.rstrip("/"))
    return not (ctx.method.upper() == "POST" and re.match(f"^{prefix}{_READ_POSTS}", ctx.path or ""))


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


def get_current_visible_projects() -> Optional[dict[str, set[str]]]:
    """Return a defensive copy of the request's shared-cluster project bounds."""
    mapping = _visible_projects_var.get(None)
    if mapping is None:
        return None
    return {cluster_id: set(projects) for cluster_id, projects in mapping.items()}


def unscoped_cache_key(base_key: str) -> str:
    """The key admin and background readers share: every cluster, no tenant ACL."""
    return f"{base_key}:scope=all"


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
        return unscoped_cache_key(base_key)
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
        return f"{base_key}:scope={scope_digest}:acl={acl_digest}{_project_bound_suffix()}"

    return f"{base_key}:scope={scope_digest}{_project_bound_suffix()}"


def _project_bound_suffix() -> str:
    """The per-request project bound, as a cache-key fragment.

    Empty when nothing is bound, so an unrestricted caller keeps the key shape it had.
    """
    mapping = _visible_projects_var.get(None)
    if not mapping:
        return ""
    parts = [
        f"{cluster}:{','.join(sorted(projects))}"
        for cluster, projects in sorted(mapping.items())
    ]
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
    return f":proj={digest}"


def verify_cluster_access(cluster_id: str) -> None:
    """
    Raise 403 if the current user cannot access ``cluster_id``.
    No-op for admin / dev mode / background tasks.

    important: a blank id is rejected before the scope check, not after. Every one of the 122
    call sites passes a required parameter declared as `Query(...)`, which an empty string
    satisfies, so `?node=` reached the handler. For a scoped caller it then failed the set
    membership and answered 403, but for an admin the scope var is None and the function
    returned early, so the request went on to ask Vault for a cluster named "" and logged two
    ERRORs per attempt. Reachable from the UI: the members page fires this when its refresh is
    clicked with no cluster chosen. Guarding here covers all 122 sites at once, where adding
    min_length to 62 signatures would not.
    """
    if not (cluster_id or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A cluster id is required and must not be blank",
        )

    allowed = _allowed_clusters_var.get(None)
    if allowed is None:
        return
    if cluster_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: cluster '{cluster_id}' is not in your organization",
        )
    if cluster_id in _read_only_clusters_var.get() and _is_change():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"The paid term for your access to cluster '{cluster_id}' has ended. It is view only "
                "until the provider removes it."
            ),
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
    # A wildcard tenant grant is still subordinate to the project bound. On a cluster shared
    # by two organizations, ``*`` means every project attributable to the policy's
    # organization, not every physical LXD project on that cluster.
    if not project_is_visible(cluster_id, project_name):
        return False

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
    # Apply the organization project bound before interpreting an ACL wildcard. Returning the
    # original list for ``*`` used to undo the bound and expose a neighbour's projects.
    visible_names = [name for name in project_names if project_is_visible(cluster_id, name)]

    acl = _tenant_acl_var.get(None)
    if acl is None:
        return visible_names

    cluster_rules = acl.get(cluster_id)
    if not cluster_rules:
        return []

    # check if wildcard project exists
    for (proj, _inst), _perms in cluster_rules.items():
        if proj == "*":
            return visible_names

    # filter to allowed projects
    allowed_projects = {proj for (proj, _inst), _perms in cluster_rules.items()}
    return [p for p in visible_names if p in allowed_projects]


def visible_projects_for(cluster_id: str) -> Optional[set[str]]:
    """Projects this request may see on a cluster, or None when unrestricted."""
    mapping = _visible_projects_var.get(None)
    if not mapping:
        return None
    return mapping.get(cluster_id)


def project_is_visible(cluster_id: str, project: Optional[str]) -> bool:
    """Whether a record owned by `project` on `cluster_id` may be shown to this request."""
    allowed = visible_projects_for(cluster_id)
    if allowed is None:
        return True
    return (project or "default") in allowed


def verify_project_access(cluster_id: str, project_name: Optional[str]) -> str:
    """Authorize one concrete project and return its normalized name.

    A missing or whitespace-only project means LXD's ``default`` project. The returned value
    must be carried through to the service call: several LXD helpers interpret a falsy project
    as "search every project", which can select another organization's same-named instance.

    This gate is independent of the tenant-operation feature flag. Partner isolation on a
    shared cluster is a project boundary, not a tenant ACL feature, and must always run before
    any LXD client or service lookup.
    """
    project = (project_name or "default").strip() or "default"
    verify_cluster_access(cluster_id)
    if not project_is_visible(cluster_id, project):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this project",
        )
    return project


def can_view_instance(
    cluster_id: str,
    project_name: Optional[str],
    instance_name: str,
) -> bool:
    """Whether this request may read one instance in one concrete project.

    This deliberately combines the Partner project bound and the Tenant instance ACL. Calling
    only ``check_tenant_permission`` is insufficient because Admin and Partner carry no tenant
    ACL, while calling only ``project_is_visible`` is insufficient for a Tenant.
    """
    project = (project_name or "default").strip() or "default"
    return project_is_visible(cluster_id, project) and check_tenant_permission(
        cluster_id,
        project,
        instance_name,
        "view",
    )


def verify_instance_view_access(
    cluster_id: str,
    project_name: Optional[str],
    instance_name: str,
) -> str:
    """Authorize one instance read and return its normalized project.

    The cluster and project gates run before the LXD lookup, so a deliberately invalid request
    cannot use response timing or errors to enumerate another organization's projects.
    """
    project = verify_project_access(cluster_id, project_name)
    verify_tenant_permission(cluster_id, project, instance_name, "view")
    return project


def filter_instances_by_acl(
    cluster_id: str,
    project_name: str,
    instance_names: list[str],
) -> list[str]:
    """
    Filter a list of instance names by the current tenant ACL.
    Returns all instances if no ACL is active.
    """
    if not project_is_visible(cluster_id, project_name):
        return []

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

@dataclass(frozen=True)
class TenantPolicyGrant:
    """One active tenant grant with the policy organization kept attached.

    The public ACL map deliberately stays compact, but it cannot answer which organization a
    wildcard came from. This provenanced form is used while deriving shared-cluster project
    bounds and by the tenant self-view, then flattened only after those boundaries are known.
    """

    organization_id: int
    policy_name: str
    cluster_id: str
    project_name: str
    instance_name: str
    permissions: frozenset[str]
    # The paid term the policy stands for (core/tenant_term.py). `permissions` above is
    # already narrowed by it; these say why, for the tenant's own view of their access.
    source: str = "local"
    term_ended_at: Optional[datetime] = None
    access_ends_at: Optional[datetime] = None
    read_only: bool = False


_resolved_tenant_grants_var: contextvars.ContextVar[
    Optional[tuple[TenantPolicyGrant, ...]]
] = contextvars.ContextVar("resolved_tenant_grants", default=None)


def get_current_resolved_tenant_grants() -> Optional[tuple[TenantPolicyGrant, ...]]:
    """Resolved grants from the same authorization snapshot as the request ACL."""
    return _resolved_tenant_grants_var.get(None)


async def get_tenant_policy_grants(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Optional[list[TenantPolicyGrant]]:
    """Return active Tenant grants without discarding their policy organization.

    ``None`` means ACL filtering does not apply to this role. An empty list is a real Tenant
    result and means no active access.
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.models.db_models import (
        Organization,
        OrganizationCluster,
        OrganizationMember,
        TenantAccessPolicy,
        TenantPolicyAssignment,
        TenantPolicyRule,
    )

    highest = get_highest_role(resolve_roles(user))
    if highest in (Role.ADMIN, Role.PARTNER):
        return None

    now = datetime.now(timezone.utc)
    enforce_term = get_settings().tenant_term_enforcement_enabled
    stmt = (
        select(
            TenantPolicyRule,
            TenantAccessPolicy.organization_id,
            TenantAccessPolicy.name,
            TenantAccessPolicy.source,
            TenantAccessPolicy.term_ended_at,
            TenantAccessPolicy.access_ends_at,
            Organization.source,
            OrganizationCluster.registered_by,
        )
        .join(
            TenantAccessPolicy,
            TenantAccessPolicy.id == TenantPolicyRule.policy_id,
        )
        .join(
            TenantPolicyAssignment,
            TenantPolicyAssignment.policy_id == TenantAccessPolicy.id,
        )
        .join(
            OrganizationMember,
            OrganizationMember.id == TenantPolicyAssignment.org_member_id,
        )
        .join(
            Organization,
            and_(
                Organization.id == OrganizationMember.organization_id,
                Organization.id == TenantAccessPolicy.organization_id,
            ),
        )
        .join(
            OrganizationCluster,
            and_(
                OrganizationCluster.organization_id == Organization.id,
                OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
            ),
        )
        .where(
            and_(
                OrganizationMember.user_email == user.email,
                Organization.is_active == True,  # noqa: E712 - SQLAlchemy comparison
                or_(
                    TenantPolicyAssignment.expires_at.is_(None),
                    TenantPolicyAssignment.expires_at > now,
                ),
                # important: a policy whose access has ended grants nothing, whether or not the
                # revoke that should have followed ever arrived.
                or_(
                    not enforce_term,
                    TenantAccessPolicy.access_ends_at.is_(None),
                    TenantAccessPolicy.access_ends_at > now,
                ),
            )
        )
        .order_by(
            TenantAccessPolicy.organization_id,
            TenantAccessPolicy.name,
            TenantPolicyRule.id,
        )
    )
    rows = (await db.execute(stmt)).unique().all()
    # important: a local policy in a buyer organization naming a cluster the buyer reaches only by
    # purchase follows that purchase's term. Without this a rule a founder wrote while the term ran
    # kept granting after it ended (security review 2026-10-27, core/sale_term.py).
    sale_terms = {}
    if enforce_term:
        bought = {
            row[1] for row in rows
            if row[3] != "orcahub" and row[6] == "orcahub" and row[7] is None
        }
        sale_terms = await orcahub_terms(db, bought)
    grants = []
    for rule, organization_id, policy_name, source, term_ended_at, access_ends_at, org_source, registered_by in rows:
        stored = frozenset(rule.permissions if isinstance(rule.permissions, list) else [])
        if enforce_term and source != "orcahub" and org_source == "orcahub" and registered_by is None:
            terms = sale_terms.get((organization_id, rule.cluster_id), [])
            phase = sale_phase(terms, now)
            if phase == SALE_ENDED:
                continue
            if phase == SALE_READ_ONLY:
                # Shown with the purchase's own dates, so the notice says when it ends.
                term_ended_at = max(t for t, _a in terms if t is not None)
                access_ends_at = latest_access_end(terms)
        state = policy_phase(
            source=source, term_ended_at=term_ended_at, access_ends_at=access_ends_at,
            permissions=stored, now=now,
        )
        # Past the paid term the policy grants viewing only (owner decision 2026-10-27).
        permissions = clamp(stored, term_ended_at=term_ended_at, now=now) if enforce_term else stored
        grants.append(
            TenantPolicyGrant(
                organization_id=organization_id,
                policy_name=policy_name,
                cluster_id=rule.cluster_id,
                project_name=rule.project_name,
                instance_name=rule.instance_name,
                permissions=permissions,
                source=source or "local",
                term_ended_at=state.term_ended_at,
                access_ends_at=state.access_ends_at,
                read_only=state.read_only,
            )
        )
    logger.debug(
        "Tenant grants for %s: %d active provenanced rules",
        user.email,
        len(grants),
    )
    return grants

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


async def _ordered_org_ids_for_email(
    user_email: str,
    db: AsyncSession,
) -> list[int]:
    """
    Org IDs a user (by email) belongs to via membership or creation, with
    partner/admin (owner) orgs first so callers can pick a deterministic primary.

    Shared by ``get_user_org_ids`` and ``reconcile_personal_clusters`` so the
    "which org owns this user's clusters" answer is computed in exactly one place.
    """
    from app.models.db_models import Organization, OrganizationMember

    stmt = (
        select(Organization.id, OrganizationMember.role, Organization.created_by)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            Organization.is_active == True,
            or_(
                OrganizationMember.user_email == user_email,
                Organization.created_by == user_email,
            ),
        )
        .order_by(Organization.id)
    )
    result = await db.execute(stmt)

    primary: list[int] = []
    secondary: list[int] = []
    seen: set[int] = set()
    for org_id, role, created_by in result.all():
        if org_id in seen:
            continue
        seen.add(org_id)
        is_owner = (role in ("partner", "admin")) or (created_by == user_email)
        (primary if is_owner else secondary).append(org_id)

    return primary + secondary


async def get_user_org_ids(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> list[int]:
    """
    Return the org IDs a user belongs to (via membership or creation), with
    partner/admin (owner) orgs first so callers can pick a deterministic primary.

    Uses the SAME membership predicate as ``get_user_allowed_clusters`` so cluster
    binding at registration cannot drift from cluster visibility (the root cause of
    orphaned clusters: a role-filtered binding query vs a role-agnostic scope query).
    """
    return await _ordered_org_ids_for_email(user.email, db)


async def _query_owned_clusters(user_email: str, db: AsyncSession) -> set[str]:
    """
    Role-agnostic owned-cluster set for an email: clusters of orgs they created or
    belong to, plus any cluster they personally registered (registered_by fallback,
    incl. unbound NULL-org rows).

    important: OUTER join + is_active INSIDE the and_ - a registrant's cluster may be
    unbound (organization_id NULL), so an inner join / top-level is_active filter would
    drop the very rows the registered_by branch is meant to surface.
    """
    from app.models.db_models import (
        Organization,
        OrganizationCluster,
        OrganizationMember,
        TenantAccessPolicy,
        TenantPolicyRule,
    )

    # important: a binding provisioning wrote for a sale (registered_by NULL on an OrcaHub
    # organization) exists only because a buyer bought something on that cluster. A buyer founder
    # holding role_partner reached the cluster through it for as long as the binding stood, after
    # the sale itself had ended. Such a binding now counts only while the organization still has a
    # live access rule on that cluster. Bindings someone registered, local organizations and the
    # organization's creator are unaffected.
    sale_still_live = (
        select(TenantPolicyRule.id)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .where(
            TenantAccessPolicy.organization_id == Organization.id,
            TenantPolicyRule.cluster_id == OrganizationCluster.cluster_id,
            # important: only the purchase's own policies. A local policy a founder wrote in the
            # buyer organization would otherwise keep the binding, and the founder's reach, alive
            # after the purchase ended (security review 2026-10-27).
            TenantAccessPolicy.source == "orcahub",
            or_(
                not get_settings().tenant_term_enforcement_enabled,
                TenantAccessPolicy.access_ends_at.is_(None),
                TenantAccessPolicy.access_ends_at > datetime.now(timezone.utc),
            ),
        )
        .exists()
    )
    stmt = (
        select(OrganizationCluster.cluster_id)
        .outerjoin(Organization, OrganizationCluster.organization_id == Organization.id)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            or_(
                and_(
                    Organization.is_active == True,
                    or_(
                        OrganizationMember.user_email == user_email,
                        Organization.created_by == user_email,
                    ),
                    or_(
                        OrganizationCluster.registered_by.is_not(None),
                        Organization.source != "orcahub",
                        Organization.created_by == user_email,
                        sale_still_live,
                    ),
                ),
                OrganizationCluster.registered_by == user_email,
            )
        )
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def get_user_allowed_clusters(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Optional[set[str]]:
    """
    Query the DB to determine which clusters a user may access.
    Admin returns None (no filter / operational god-view).
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role

    roles = resolve_roles(user)
    if get_highest_role(roles) == Role.ADMIN:
        return None  # admin sees everything (operational visibility)

    cluster_ids = await _query_owned_clusters(user.email, db)
    logger.debug(
        f"Org-scope for {user.email}: allowed_clusters={cluster_ids or '(none)'}"
    )
    return cluster_ids


async def get_owned_clusters_ignoring_admin(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> set[str]:
    """
    The clusters a user actually OWNS (via org membership/creation or personal
    registration), computed regardless of the admin role short-circuit.

    Used by the break-glass gate: a pure admin owns nothing here (every tenant
    cluster is out-of-scope and needs a grant), while an admin who is ALSO a partner
    still owns their own org's clusters and must not be locked out of them.
    """
    return await _query_owned_clusters(user.email, db)


async def resolve_cluster_org(
    host: str,
    db: AsyncSession,
) -> tuple[Optional[int], Optional[str]]:
    """
    Map a cluster_id (the LXD ``host`` key) to its owning organization (id, slug).
    Returns (None, None) for an unbound or unknown cluster. A bound row wins over the
    unbound (NULL-org) ownership row that can also exist for the same cluster.
    """
    from app.models.db_models import Organization, OrganizationCluster

    stmt = (
        select(Organization.id, Organization.slug)
        .join(OrganizationCluster, OrganizationCluster.organization_id == Organization.id)
        .where(OrganizationCluster.cluster_id == host)
        .order_by(Organization.id)
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return (None, None)
    return (row[0], row[1])


async def org_to_hold_project_grant(
    user_email: str,
    host: str,
    db: AsyncSession,
) -> Optional[int]:
    """The organization a project this user creates on `host` is recorded under.

    The first of the user's organizations in the documented primary order
    (`_ordered_org_ids_for_email`: owner orgs first) that is bound to the cluster. None when
    none of them is: a rule for a cluster under an organization not bound to it is exactly the
    stale shape the policy repair removes, so there is no fallback to an unbound org.
    """
    ordered = await _ordered_org_ids_for_email(user_email, db)
    bound = {org_id for org_id, _slug in await resolve_cluster_orgs(host, db)}
    for org_id in ordered:
        if org_id in bound:
            return org_id
    return None


async def resolve_cluster_orgs(
    host: str,
    db: AsyncSession,
) -> list[tuple[int, str]]:
    """EVERY organization bound to a cluster, lowest id first.

    important: `resolve_cluster_org` answers "which one org do we attribute this to" and takes the
    lowest id, which is fine for a label and wrong for a permission. A cluster can carry more than
    one binding, and one does on this deployment today, so a rule that consults a single row is
    asking one tenant a question about another tenant's guests.
    """
    from app.models.db_models import Organization, OrganizationCluster

    stmt = (
        select(Organization.id, Organization.slug)
        .join(OrganizationCluster, OrganizationCluster.organization_id == Organization.id)
        .where(OrganizationCluster.cluster_id == host)
        .order_by(Organization.id)
    )
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]


async def _query_user_org_ids(user_email: str, db: AsyncSession) -> set[int]:
    """Organization ids a user belongs to or created."""
    from app.models.db_models import Organization, OrganizationMember

    stmt = (
        select(Organization.id)
        .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            and_(
                Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
                or_(
                    OrganizationMember.user_email == user_email,
                    Organization.created_by == user_email,
                ),
            )
        )
    )
    return set((await db.execute(stmt)).scalars().all())


async def _clusters_with_several_orgs(db: AsyncSession) -> set[str]:
    """Clusters bound to more than one organization.

    important: resolve_cluster_org answers "which one org do we attribute this to" and takes
    the lowest id, which is right for a label and wrong for a permission. A cluster can carry
    more than one binding, and one does on this deployment today.
    """
    from app.models.db_models import OrganizationCluster

    # important: organization_id is nullable by design. A cluster a partner registered before
    # their org was synced is stored with a NULL org and owned through registered_by, and
    # count(distinct col) in Postgres skips NULLs. Counting the raw column would read
    # "one org bound plus one unbound registrant" as a single owner and leave the cluster
    # unrestricted, which is the direction that leaks. coalesce gives every unbound row its
    # own identity so it counts as another owner.
    owner_key = func.coalesce(
        cast(OrganizationCluster.organization_id, String),
        "unbound:" + cast(OrganizationCluster.id, String),
    )
    stmt = (
        select(OrganizationCluster.cluster_id)
        .group_by(OrganizationCluster.cluster_id)
        .having(func.count(func.distinct(owner_key)) > 1)
    )
    return set((await db.execute(stmt)).scalars().all())


async def _resolve_tenant_grants(
    grants: list[TenantPolicyGrant],
    shared_clusters: set[str],
    db: AsyncSession,
) -> tuple[list[TenantPolicyGrant], dict[str, set[str]]]:
    """Expand shared-cluster wildcards before grants from different orgs are merged.

    A project has no first-class owner record in the current schema. The established ownership
    evidence is therefore the same conservative evidence used for Partners: ``default`` plus
    concrete project names in policies from that organization while it remains bound to the
    cluster. Expansion happens per ``(organization_id, cluster_id)`` while policy provenance is
    still attached. This ordering is the security boundary: computing one global project union
    first would let organization A's wildcard consume a narrow project grant from organization
    B when the same Tenant legitimately belongs to both.
    """
    from app.models.db_models import (
        Organization,
        OrganizationCluster,
        TenantAccessPolicy,
        TenantPolicyRule,
    )

    allowed_clusters = _allowed_clusters_var.get(None)
    bounded_clusters = (
        shared_clusters
        if allowed_clusters is None
        else shared_clusters.intersection(allowed_clusters)
    )
    visible_projects = {cluster_id: set() for cluster_id in bounded_clusters}
    projects_by_owner: dict[tuple[int, str], set[str]] = {}
    wildcard_owners: set[tuple[int, str]] = set()

    for grant in grants:
        if grant.cluster_id not in shared_clusters:
            continue
        owner = (grant.organization_id, grant.cluster_id)
        owner_projects = projects_by_owner.setdefault(owner, set())
        if grant.project_name == "*":
            wildcard_owners.add(owner)
            owner_projects.add("default")
        elif grant.project_name:
            owner_projects.add(grant.project_name)

    if wildcard_owners:
        organization_ids = {organization_id for organization_id, _cluster in wildcard_owners}
        cluster_ids = {cluster_id for _organization_id, cluster_id in wildcard_owners}
        stmt = (
            select(
                TenantAccessPolicy.organization_id,
                TenantPolicyRule.cluster_id,
                TenantPolicyRule.project_name,
            )
            .join(
                TenantAccessPolicy,
                TenantAccessPolicy.id == TenantPolicyRule.policy_id,
            )
            .join(
                Organization,
                Organization.id == TenantAccessPolicy.organization_id,
            )
            .join(
                OrganizationCluster,
                and_(
                    OrganizationCluster.organization_id
                    == TenantAccessPolicy.organization_id,
                    OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
                ),
            )
            .where(
                and_(
                    TenantAccessPolicy.organization_id.in_(organization_ids),
                    TenantPolicyRule.cluster_id.in_(cluster_ids),
                    TenantPolicyRule.project_name != "*",
                    Organization.is_active == True,  # noqa: E712 - SQLAlchemy comparison
                )
            )
            .distinct()
        )
        for organization_id, cluster_id, project_name in (await db.execute(stmt)).all():
            owner = (organization_id, cluster_id)
            if owner in wildcard_owners and project_name:
                projects_by_owner[owner].add(project_name)

    resolved: list[TenantPolicyGrant] = []
    for grant in grants:
        if grant.cluster_id in shared_clusters and grant.project_name == "*":
            owner = (grant.organization_id, grant.cluster_id)
            for project_name in sorted(projects_by_owner.get(owner, set())):
                resolved.append(
                    TenantPolicyGrant(
                        organization_id=grant.organization_id,
                        policy_name=grant.policy_name,
                        cluster_id=grant.cluster_id,
                        project_name=project_name,
                        instance_name=grant.instance_name,
                        permissions=grant.permissions,
                    )
                )
        else:
            resolved.append(grant)

    for grant in resolved:
        if grant.cluster_id in visible_projects and grant.project_name != "*":
            visible_projects[grant.cluster_id].add(grant.project_name)
    return resolved, visible_projects


async def get_visible_projects(
    user: AuthenticatedUser,
    db: AsyncSession,
    tenant_grants: Optional[list[TenantPolicyGrant]] = None,
) -> Optional[dict[str, set[str]]]:
    """Which LXD projects this user may see, on clusters that carry more than one org.

    Returns None when nothing needs restricting, and otherwise a map of cluster to the set of
    project names that may be shown. A cluster absent from the map is unrestricted.

    Admin is unrestricted here. A Partner is scoped by cluster alone, so the safe default on a
    shared cluster is ``default`` widened by concrete projects their own policies name. A
    Tenant also needs a project bound: their ACL can contain ``project_name='*'``, which must
    mean the policy organization's projects rather than every project another organization
    happens to run on the same cluster.
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.models.db_models import (
        OrganizationCluster,
        TenantAccessPolicy,
        TenantPolicyRule,
    )

    highest = get_highest_role(resolve_roles(user))
    if highest == Role.ADMIN:
        return None

    shared = await _clusters_with_several_orgs(db)
    if not shared:
        return None

    if highest == Role.TENANT:
        grants = (
            tenant_grants
            if tenant_grants is not None
            else await get_tenant_policy_grants(user, db)
        )
        # get_tenant_policy_grants returns a list for Tenant. Keep the fallback fail-closed if a
        # future role resolution changes that contract.
        _resolved, mapping = await _resolve_tenant_grants(grants or [], shared, db)
        return mapping or None

    org_ids = await _query_user_org_ids(user.email, db)
    mapping: dict[str, set[str]] = {}
    for cluster_id in shared:
        allowed = {"default"}
        if org_ids:
            stmt = (
                select(TenantPolicyRule.project_name)
                .join(
                    TenantAccessPolicy,
                    TenantAccessPolicy.id == TenantPolicyRule.policy_id,
                )
                .join(
                    OrganizationCluster,
                    and_(
                        OrganizationCluster.organization_id
                        == TenantAccessPolicy.organization_id,
                        OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
                    ),
                )
                .where(
                    and_(
                        TenantPolicyRule.cluster_id == cluster_id,
                        TenantAccessPolicy.organization_id.in_(org_ids),
                    )
                )
                .distinct()
            )
            named = set((await db.execute(stmt)).scalars().all())
            # important: '*' does NOT lift the bound. project_name defaults to '*' on the
            # model and on the create schema, so any organization that had ever written a
            # single tenant policy for this cluster would have been unrestricted, which is the
            # control switched off by default rather than an edge case. A wildcard also cannot
            # answer the question being asked here, which is WHICH projects belong to this
            # organization; on a cluster shared with another org it names theirs too.
            allowed |= {n for n in named if n and n != "*"}
        mapping[cluster_id] = allowed

    return mapping or None


async def reconcile_personal_clusters(
    db: AsyncSession,
    user_email: str,
    # note: called as (cluster_id, from_org, to_org) for each row it moves, before the commit
    on_rebind: Optional[Callable[[str, Optional[int], int], None]] = None,
    # important: when given, the caller has decided the destination and the primary-org
    # ordering is not consulted. An integration push passes the one org its key claimed.
    organization_id: Optional[int] = None,
) -> int:
    """
    Bind a user's still-unbound (organization_id NULL) clusters to their primary org.

    Called once a partner becomes linked to an org (creates an API key, or their org
    is synced from OrcaHub), so clusters they registered before that point stop being
    "personal" and become visible to org-mates / eligible for tenant policies + webhooks.

    Idempotent: filtered on ``organization_id IS NULL`` so a second run is a no-op, and
    returns 0 when the user has no org or no unbound clusters. A row whose cluster is
    already bound to the primary org is deleted (would violate uq_org_cluster), not
    duplicated. The rebind is a plain UPDATE on a standalone row - never routed through
    ``Organization.clusters`` so the delete-orphan cascade cannot fire.
    """
    from app.models.db_models import OrganizationCluster
    from app.services.shared_cluster_audit import (
        assert_cluster_safe_to_share,
        serialize_cluster_share,
    )

    if organization_id is not None:
        primary = organization_id
    else:
        org_ids = await _ordered_org_ids_for_email(user_email, db)
        if not org_ids:
            return 0
        primary = org_ids[0]

    # PostgreSQL transaction advisory locks live until the commit below. Acquire several
    # cluster locks in one deterministic order so two concurrent reconciliations cannot form
    # an A-then-B / B-then-A deadlock.
    rows = sorted(
        (
            await db.execute(
                select(OrganizationCluster).where(
                    OrganizationCluster.organization_id.is_(None),
                    OrganizationCluster.registered_by == user_email,
                )
            )
        ).scalars().all(),
        key=lambda row: (row.cluster_id, row.id or 0),
    )
    if not rows:
        return 0

    rebound = 0
    for row in rows:
        async with serialize_cluster_share(db, row.cluster_id):
            already_bound = (
                await db.execute(
                    select(OrganizationCluster.id).where(
                        OrganizationCluster.organization_id == primary,
                        OrganizationCluster.cluster_id == row.cluster_id,
                    )
                )
            ).scalar_one_or_none()
            if already_bound is not None:
                # No new identity is created. Drop the obsolete ambiguous row without daemon I/O.
                await db.delete(row)
            else:
                await assert_cluster_safe_to_share(
                    db,
                    row.cluster_id,
                    prospective_organization_id=primary,
                    prospective_registered_by=user_email,
                    force_audit=True,
                )
                if on_rebind is not None:
                    on_rebind(row.cluster_id, row.organization_id, primary)
                row.organization_id = primary
                rebound += 1
            # Establish the DB write while the SQLite/test fallback lock is still held. On
            # PostgreSQL the transaction advisory lock remains held through commit below.
            await db.flush()
    await db.commit()
    if rebound:
        logger.info(
            f"Reconciled {rebound} unbound cluster(s) to org {primary} for {user_email}"
        )
    return rebound


async def get_tenant_acl_rules(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Optional[dict]:
    """
    Build the resolved ACL map for a tenant user.
    """
    acl, _grants, _visible_projects = await get_tenant_scope(user, db)
    return acl


def _tenant_acl_from_grants(
    grants: Optional[list[TenantPolicyGrant]],
) -> Optional[dict[str, dict[tuple[str, str], set[str]]]]:
    """Flatten provenanced grants only after the project-bound inputs are retained."""
    if grants is None:
        return None

    # build ACL map: merge permissions across policies
    acl: dict[str, dict[tuple[str, str], set[str]]] = {}
    for grant in grants:
        cluster_id = grant.cluster_id
        if cluster_id not in acl:
            acl[cluster_id] = {}
        key = (grant.project_name, grant.instance_name)
        if key not in acl[cluster_id]:
            acl[cluster_id][key] = set()
        acl[cluster_id][key].update(grant.permissions)

    return acl


async def get_tenant_scope(
    user: AuthenticatedUser,
    db: AsyncSession,
) -> tuple[
    Optional[dict[str, dict[tuple[str, str], set[str]]]],
    Optional[list[TenantPolicyGrant]],
    Optional[dict[str, set[str]]],
]:
    """Resolve ACL, per-org-expanded grants, and the shared-cluster project bound."""
    grants = await get_tenant_policy_grants(user, db)
    if grants is None:
        return None, None, None

    shared = await _clusters_with_several_orgs(db)
    resolved_grants, visible_projects = await _resolve_tenant_grants(
        grants,
        shared,
        db,
    )
    acl = _tenant_acl_from_grants(resolved_grants)
    if acl is not None:
        logger.debug(
            "Tenant ACL for %s: %d clusters, %d rules",
            user.email,
            len(acl),
            sum(len(rules) for rules in acl.values()),
        )
    return acl, resolved_grants, visible_projects or None


# ---------------------------------------------------------------------------
# Tenant ACL narrowing (shared by the HTTP dependency and the WS setter)
# ---------------------------------------------------------------------------

def _narrow_by_tenant_acl(
    allowed: Optional[set[str]],
    acl: Optional[dict],
) -> Optional[set[str]]:
    """
    Intersect the org-derived cluster set with the clusters the tenant's ACL grants.

    important: an EMPTY acl dict is a real answer, not a missing one. It means the tenant holds
    no active policy assignment, and check_tenant_permission already denies every cluster for
    it. Keeping the org-wide set here contradicted that: the tenant saw zero instances while
    unreachable_nodes still named every down cluster in the partner's organisation, and every
    power route whose ACL check sits behind TENANT_PERM_ENFORCEMENT_ENABLED still passed
    verify_cluster_access. Empty ACL means empty scope, so the two layers agree.

    note: None stays unrestricted and is returned untouched. get_user_allowed_clusters returns
    None only for an admin and get_tenant_acl_rules returns None only for admin or partner, so
    both are non-None for tenants and only for tenants. The guard is kept anyway: without it a
    future role answering None for one and a dict for the other would raise inside a
    router-level dependency, which is a 500 on every request rather than a visible mistake.
    """
    if acl is None or allowed is None:
        return allowed
    return allowed.intersection(acl.keys())


# ---------------------------------------------------------------------------
# FastAPI dependency (add to router-level dependencies list)
# ---------------------------------------------------------------------------

async def _partner_read_only(
    user: "AuthenticatedUser", db: AsyncSession, allowed: Optional[set[str]],
) -> frozenset:
    """The clusters this Partner may only read: reached through purchases past their paid term."""
    from app.core.rbac import Role, get_highest_role, resolve_roles
    from app.core.sale_term import partner_read_only_clusters

    if allowed is None or db is None or not get_settings().tenant_term_enforcement_enabled:
        return frozenset()
    if get_highest_role(resolve_roles(user)) != Role.PARTNER:
        return frozenset()
    return frozenset(await partner_read_only_clusters(db, user.email) & allowed)


async def org_scope_dependency(
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Router-level dependency that sets the org-scope context variable
    and tenant ACL if applicable.
    """
    _interactive_var.set(False)
    _read_only_clusters_var.set(frozenset())
    if user is None:
        # dev mode - no filter
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        _visible_projects_var.set(None)
        _resolved_tenant_grants_var.set(None)
        return

    allowed = await get_user_allowed_clusters(user, db)
    _allowed_clusters_var.set(allowed)

    # Resolve once so the compact ACL and the project bound are derived from exactly the same
    # provenanced grant rows. Querying the assignment join twice costs every API request and can
    # also observe two sides of a concurrent revoke.
    acl, tenant_grants, tenant_visible_projects = await get_tenant_scope(user, db)
    _tenant_acl_var.set(acl)
    _resolved_tenant_grants_var.set(
        None if tenant_grants is None else tuple(tenant_grants)
    )

    if tenant_grants is None:
        # A Partner is scoped by cluster alone, so a cluster shared with another organization
        # needs a project bound as well. Admin remains unrestricted.
        tenant_visible_projects = await get_visible_projects(user, db)
    _visible_projects_var.set(tenant_visible_projects)
    _read_only_clusters_var.set(await _partner_read_only(user, db, allowed))

    # for tenants: restrict to the clusters their ACL grants (empty ACL means empty scope)
    _allowed_clusters_var.set(_narrow_by_tenant_acl(allowed, acl))

    # push org names into RequestContext for audit logs
    from app.core.request_context import get_request_context
    org_names = await _resolve_user_org_names(user, db)
    ctx = get_request_context()
    if ctx is not None:
        ctx.user_orgs = org_names


async def set_org_scope_for_ws(user: Optional["AuthenticatedUser"], *, interactive: bool = False) -> None:
    """
    Set org scope and tenant ACL for WebSocket handlers where DI is unavailable.

    `interactive` for a socket that drives a guest (console, terminal): on a cluster a buyer reaches
    only through an ended paid term, that is refused like any other change.
    """
    _interactive_var.set(interactive)
    _read_only_clusters_var.set(frozenset())
    if user is None:
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        _visible_projects_var.set(None)
        _resolved_tenant_grants_var.set(None)
        return

    from app.core.rbac import resolve_roles, get_highest_role, Role

    roles = resolve_roles(user)
    highest = get_highest_role(roles)
    if highest == Role.ADMIN:
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        _visible_projects_var.set(None)
        _resolved_tenant_grants_var.set(None)
        return

    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        allowed = await get_user_allowed_clusters(user, db)
        _allowed_clusters_var.set(allowed)

        acl, tenant_grants, tenant_visible_projects = await get_tenant_scope(user, db)
        _tenant_acl_var.set(acl)
        _resolved_tenant_grants_var.set(
            None if tenant_grants is None else tuple(tenant_grants)
        )

        if tenant_grants is None:
            tenant_visible_projects = await get_visible_projects(user, db)
        _visible_projects_var.set(tenant_visible_projects)
        _read_only_clusters_var.set(await _partner_read_only(user, db, allowed))

        # important: the same narrowing as the HTTP dependency, through the same helper. These
        # were two duplicated blocks, which is how the empty-ACL fallback survived: fixing one
        # copy leaves console, terminal and events on the old behaviour, with nothing to notice.
        _allowed_clusters_var.set(_narrow_by_tenant_acl(allowed, acl))

        # push org names into RequestContext for audit logs
        from app.core.request_context import get_request_context
        org_names = await _resolve_user_org_names(user, db)
        ctx = get_request_context()
        if ctx is not None:
            ctx.user_orgs = org_names
