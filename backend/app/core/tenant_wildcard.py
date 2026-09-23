"""Hold a tenant's wildcard project rule inside its own organization on a shared cluster.

A tenant rule's project defaults to '*', and '*' matched every LXD project on the cluster. On a
cluster bound to one organization that is the intended meaning. On a cluster bound to several
organizations it also named the other organizations' projects, so a tenant holding a wildcard
could list, read and (with the flag off) power another customer's instances.

The schema has no project owner record. The ownership evidence used here is the same one the
Partner bound uses (org_scope.get_visible_projects): the default project plus every concrete
project that the organization's own policies name on that cluster while it is still bound to
it. Each wildcard is expanded with its own organization's projects BEFORE grants from several
organizations are merged into the ACL map, because after the merge the map can no longer say
which organization a wildcard came from.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.core.auth import AuthenticatedUser

WILDCARD = "*"
DEFAULT_PROJECT = "default"

OrgCluster = tuple[int, str]


@dataclass(frozen=True)
class RuleGrant:
    """One active tenant rule with the organization of its policy still attached."""

    organization_id: int
    cluster_id: str
    project_name: str
    instance_name: str
    permissions: tuple[str, ...]


def confine(
    grants: Iterable[RuleGrant],
    outside: dict[int, set[str]],
    owned: dict[OrgCluster, set[str]],
) -> list[RuleGrant]:
    """Rewrite each wildcard project into its organization's projects where another party holds the cluster.

    outside maps an organization to the clusters that carry a binding outside its party. A grant on
    any other cluster, or one naming a concrete project, is returned unchanged.
    """
    confined: list[RuleGrant] = []
    for grant in grants:
        if grant.project_name != WILDCARD or grant.cluster_id not in outside.get(grant.organization_id, set()):
            confined.append(grant)
            continue
        projects = {DEFAULT_PROJECT} | owned.get((grant.organization_id, grant.cluster_id), set())
        confined.extend(replace(grant, project_name=name) for name in sorted(projects))
    return confined


async def organization_projects(
    db: AsyncSession,
    pairs: set[OrgCluster],
) -> dict[OrgCluster, set[str]]:
    """Concrete projects each (organization, cluster) pair names in its own policies.

    note: the organization must still be bound to the cluster. A policy left behind after an
    unbind names projects the organization no longer administers there.
    """
    from app.models.db_models import (
        OrganizationCluster,
        TenantAccessPolicy,
        TenantPolicyRule,
    )

    if not pairs:
        return {}
    stmt = (
        select(
            TenantAccessPolicy.organization_id,
            TenantPolicyRule.cluster_id,
            TenantPolicyRule.project_name,
        )
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .join(
            OrganizationCluster,
            and_(
                OrganizationCluster.organization_id == TenantAccessPolicy.organization_id,
                OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
            ),
        )
        .where(
            and_(
                TenantAccessPolicy.organization_id.in_(sorted({org for org, _ in pairs})),
                TenantPolicyRule.cluster_id.in_(sorted({cluster for _, cluster in pairs})),
                TenantPolicyRule.project_name != WILDCARD,
            )
        )
        .distinct()
    )
    owned: dict[OrgCluster, set[str]] = {}
    for organization_id, cluster_id, project_name in (await db.execute(stmt)).all():
        key = (organization_id, cluster_id)
        # note: the IN filters form a cross product, so drop pairs nobody asked about
        if key in pairs and project_name:
            owned.setdefault(key, set()).add(project_name)
    return owned


async def confine_wildcards(db: AsyncSession, grants: list[RuleGrant]) -> list[RuleGrant]:
    """Confine every wildcard grant on a cluster another party holds. No query runs without a wildcard.

    important: by the policy organization's party, not by binding count. A partner's home
    organization and its buyers carry one integration key, so a '*' rule of the home organization
    on the partner's own cluster keeps meaning every project there, as it did before.
    """
    from app.services.cluster_parties import clusters_outside_org_party

    wildcards = [grant for grant in grants if grant.project_name == WILDCARD]
    if not wildcards:
        return grants
    outside = await clusters_outside_org_party(
        db, {grant.organization_id for grant in wildcards}, {grant.cluster_id for grant in wildcards}
    )
    pairs = {
        (grant.organization_id, grant.cluster_id)
        for grant in wildcards
        if grant.cluster_id in outside.get(grant.organization_id, set())
    }
    if not pairs:
        return grants
    return confine(grants, outside, await organization_projects(db, pairs))


def tenant_project_bound(
    acl: dict[str, dict[tuple[str, str], set[str]]],
    shared: set[str],
) -> Optional[dict[str, set[str]]]:
    """The project bound for a tenant: default plus the projects its ACL names, per shared cluster.

    Setting the bound makes project_is_visible, and every gate built on it, hold the tenant to these
    projects even where the enforcement flag leaves the ACL check off. The ACL has already been
    confined, so a wildcard still standing is one inside its organization's party, where it means
    every project, and that cluster gets no bound.
    """
    bound = {
        cluster_id: {DEFAULT_PROJECT}
        | {project for (project, _instance) in rules if project != WILDCARD}
        for cluster_id, rules in acl.items()
        if cluster_id in shared and not any(project == WILDCARD for (project, _instance) in rules)
    }
    return bound or None


# ---------------------------------------------------------------------------
# Tenant ACL narrowing (shared by the HTTP dependency and the WS setter)
# ---------------------------------------------------------------------------

async def project_bound_for(
    user: "AuthenticatedUser",
    db: AsyncSession,
    acl: Optional[dict],
) -> Optional[dict[str, set[str]]]:
    """The project bound for this request, for both scope setters.

    A Partner or Admin (no tenant ACL) gets get_visible_projects. A tenant on a shared cluster
    is held to default plus the projects its confined ACL names, so every project_is_visible
    gate applies to them as well and not only the flag-gated ACL check.
    """
    # note: imported here because org_scope imports this module
    from app.core import org_scope

    if acl is None:
        return await org_scope.get_visible_projects(user, db)
    if not acl:
        return None
    return tenant_project_bound(acl, await org_scope._clusters_with_several_orgs(db))


def narrow_by_tenant_acl(
    allowed: Optional[set[str]],
    acl: Optional[dict],
) -> Optional[set[str]]:
    """
    Intersect the org-derived cluster set with the clusters the tenant's ACL grants.

    important: an EMPTY acl dict is a real answer, not a missing one. It means the tenant holds
    no active policy assignment, and check_tenant_permission already denies every cluster for
    it. Keeping the org-wide set here contradicted that. The tenant saw no instances, yet every
    power route whose ACL check sits behind TENANT_PERM_ENFORCEMENT_ENABLED still passed
    verify_cluster_access, and break-glass reused the same set for the terminal. Empty ACL means
    empty scope, so the two layers agree.

    note: None stays unrestricted and is returned untouched. get_user_allowed_clusters returns
    None only for an admin and get_tenant_acl_rules returns None only for admin or partner, so
    both are non-None for tenants and only for tenants. The guard stays so that a future role
    answering None for one and a dict for the other cannot raise inside a router-level
    dependency, which would be a 500 on every request.
    """
    if acl is None or allowed is None:
        return allowed
    return allowed.intersection(acl.keys())
