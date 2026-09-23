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
from sqlalchemy import select, or_, and_, func, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.tenant_wildcard import (
    narrow_by_tenant_acl as _narrow_by_tenant_acl,
    project_bound_for as _project_bound_for,
)

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

    important: the project bound is mixed in for the same reason. A Partner's tenant ACL is
    always None, so two Partners from different organizations whose allowed-cluster sets match
    produced an identical key. That was harmless while both saw only the default project's
    rows; once the lists widened, the bodies differ per organization and whoever filled the
    entry first decided what the other read for the whole TTL.
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
    from app.models.db_models import Organization, OrganizationMember, OrganizationCluster

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


async def get_visible_projects(
    user: AuthenticatedUser,
    db: AsyncSession,
    allowed: Optional[set[str]] = None,
) -> Optional[dict[str, set[str]]]:
    """Which LXD projects this user may see, on the clusters they reach that another party holds.

    `allowed` is the caller's cluster scope (get_user_allowed_clusters), read here when not given.

    Returns None when nothing needs restricting, and otherwise a map of cluster to the set of
    project names that may be shown. A cluster absent from the map is unrestricted.

    Only Partners need this. A Tenant's bound comes from its confined ACL instead (see
    _project_bound_for), and an Admin reaching a cluster they do not own is gated by
    break-glass at the cluster level. A Partner, though, is scoped by cluster alone, so on a cluster shared with
    another organization the only safe default is the behaviour they have today: the default
    project, widened by whatever their own policies name.
    """
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    if get_highest_role(resolve_roles(user)) != Role.PARTNER:
        return None

    # important: by party, not by binding count. Provisioning binds every buyer organization to the
    # partner's cluster, so counting bindings bounded a partner on its own cluster after its first
    # sale and refused its own and its buyers' projects on every gate built on this bound. Asked of
    # every cluster the caller reaches, not only those with two bindings: a partner with a member
    # row in another partner's organization reaches that partner's single-binding cluster too.
    from app.services.cluster_parties import clusters_held_by_another_party

    if allowed is None:
        allowed = await get_user_allowed_clusters(user, db)
    shared = await clusters_held_by_another_party(db, user.email, allowed or set())
    if not shared:
        return None

    from app.models.db_models import OrganizationMember, TenantPolicyAssignment
    from app.services.cluster_parties import run_orgs

    # important: widened only by the policies of the organizations the caller runs and by the
    # policies assigned to the caller's own member rows. A member row in another partner's
    # organization is how a partner reaches that partner's cluster at all, and the projects that
    # organization's policies name belong to its tenants, not to the caller.
    # important: '*' does NOT lift the bound. project_name defaults to '*' on the model and on
    # the create schema, so any organization that had ever written a single tenant policy for
    # this cluster would have been unrestricted. A wildcard also cannot answer the question
    # asked here, which is WHICH projects belong to the caller; on a shared cluster it names
    # the other party's too.
    now = datetime.now(timezone.utc)
    assigned = (
        select(TenantPolicyAssignment.policy_id)
        .join(OrganizationMember, OrganizationMember.id == TenantPolicyAssignment.org_member_id)
        .where(
            OrganizationMember.user_email == user.email,
            or_(TenantPolicyAssignment.expires_at.is_(None), TenantPolicyAssignment.expires_at > now),
        )
    )
    runs = sorted(await run_orgs(db, user.email))
    stmt = (
        select(TenantPolicyRule.cluster_id, TenantPolicyRule.project_name)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .where(
            TenantPolicyRule.cluster_id.in_(sorted(shared)),
            or_(TenantAccessPolicy.organization_id.in_(runs), TenantPolicyRule.policy_id.in_(assigned)),
        )
        .distinct()
    )
    mapping: dict[str, set[str]] = {cluster_id: {"default"} for cluster_id in shared}
    for cluster_id, project in (await db.execute(stmt)).all():
        if project and project != "*":
            mapping[cluster_id].add(project)
    return mapping or None


async def reconcile_personal_clusters(
    db: AsyncSession,
    user_email: str,
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

    org_ids = await _ordered_org_ids_for_email(user_email, db)
    if not org_ids:
        return 0
    primary = org_ids[0]

    bound = set((await db.execute(
        select(OrganizationCluster.cluster_id).where(
            OrganizationCluster.organization_id == primary
        )
    )).scalars().all())

    rows = (await db.execute(
        select(OrganizationCluster).where(
            OrganizationCluster.organization_id.is_(None),
            OrganizationCluster.registered_by == user_email,
        )
    )).scalars().all()
    if not rows:
        return 0

    rebound = 0
    for row in rows:
        if row.cluster_id in bound:
            await db.delete(row)  # already bound concretely - drop the duplicate unbound row
        else:
            row.organization_id = primary
            bound.add(row.cluster_id)
            rebound += 1
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
    from app.core.rbac import resolve_roles, get_highest_role, Role
    from app.core.tenant_wildcard import RuleGrant, confine_wildcards, grants_their_org_holds
    from app.models.db_models import (
        OrganizationMember,
        TenantAccessPolicy,
        TenantPolicyAssignment,
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

    # load policy rules with the organization of their policy attached
    rules_stmt = (
        select(TenantPolicyRule, TenantAccessPolicy.organization_id)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .where(TenantPolicyRule.policy_id.in_(active_policy_ids))
    )
    result = await db.execute(rules_stmt)
    grants = [
        RuleGrant(
            organization_id=organization_id,
            cluster_id=rule.cluster_id,
            project_name=rule.project_name,
            instance_name=rule.instance_name,
            permissions=tuple(rule.permissions if isinstance(rule.permissions, list) else []),
        )
        for rule, organization_id in result.all()
    ]

    # important: a '*' project on a cluster bound to several organizations named the other
    # organizations' projects too. Each wildcard is expanded with its own organization's
    # projects here, before the merge below loses which organization it came from. A rule on a
    # cluster its organization no longer holds is dropped first.
    grants = await confine_wildcards(db, await grants_their_org_holds(db, grants))

    # build ACL map: merge permissions across policies
    acl: dict[str, dict[tuple[str, str], set[str]]] = {}
    for grant in grants:
        key = (grant.project_name, grant.instance_name)
        acl.setdefault(grant.cluster_id, {}).setdefault(key, set()).update(grant.permissions)

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
    """
    if user is None:
        # dev mode - no filter
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        _visible_projects_var.set(None)
        return

    allowed = await get_user_allowed_clusters(user, db)
    _allowed_clusters_var.set(allowed)

    # set tenant ACL (None for admin/partner, dict for tenant)
    acl = await get_tenant_acl_rules(user, db)
    _tenant_acl_var.set(acl)

    # a cluster shared with another organization needs a project bound as well, for a Partner
    # (scoped by cluster alone) and for a tenant. See _project_bound_for.
    _visible_projects_var.set(await _project_bound_for(user, db, acl, allowed))

    # for tenants: restrict to the clusters their ACL grants (empty ACL means empty scope)
    _allowed_clusters_var.set(_narrow_by_tenant_acl(allowed, acl))

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
        _visible_projects_var.set(None)
        return

    from app.core.rbac import resolve_roles, get_highest_role, Role

    roles = resolve_roles(user)
    highest = get_highest_role(roles)
    if highest == Role.ADMIN:
        _allowed_clusters_var.set(None)
        _tenant_acl_var.set(None)
        _visible_projects_var.set(None)
        return

    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        allowed = await get_user_allowed_clusters(user, db)
        _allowed_clusters_var.set(allowed)

        acl = await get_tenant_acl_rules(user, db)
        _tenant_acl_var.set(acl)

        _visible_projects_var.set(await _project_bound_for(user, db, acl, allowed))

        # important: the same narrowing as the HTTP dependency, through the same helper. These
        # were two duplicated blocks, so fixing one copy would leave console, terminal and
        # events on the old behaviour with nothing to notice.
        _allowed_clusters_var.set(_narrow_by_tenant_acl(allowed, acl))

        # push org names into RequestContext for audit logs
        from app.core.request_context import get_request_context
        org_names = await _resolve_user_org_names(user, db)
        ctx = get_request_context()
        if ctx is not None:
            ctx.user_orgs = org_names
