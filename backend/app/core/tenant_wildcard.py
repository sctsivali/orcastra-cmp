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
from typing import Awaitable, Callable, Iterable, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    shared: set[str],
    owned: dict[OrgCluster, set[str]],
) -> list[RuleGrant]:
    """Rewrite each wildcard project on a shared cluster into its organization's projects.

    A grant on an unshared cluster, or one naming a concrete project, is returned unchanged.
    """
    confined: list[RuleGrant] = []
    for grant in grants:
        if grant.project_name != WILDCARD or grant.cluster_id not in shared:
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


async def confine_wildcards(
    db: AsyncSession,
    grants: list[RuleGrant],
    load_shared: Callable[[AsyncSession], Awaitable[set[str]]],
) -> list[RuleGrant]:
    """Confine every wildcard grant on a shared cluster. No query runs without a wildcard."""
    if not any(grant.project_name == WILDCARD for grant in grants):
        return grants
    shared = await load_shared(db)
    pairs = {
        (grant.organization_id, grant.cluster_id)
        for grant in grants
        if grant.project_name == WILDCARD and grant.cluster_id in shared
    }
    if not pairs:
        return grants
    return confine(grants, shared, await organization_projects(db, pairs))


def tenant_project_bound(
    acl: dict[str, dict[tuple[str, str], set[str]]],
    shared: set[str],
) -> Optional[dict[str, set[str]]]:
    """The project bound for a tenant: default plus the projects its ACL names, per shared cluster.

    The ACL has already been confined, so a shared cluster carries no wildcard here. Setting the
    bound makes project_is_visible, and every gate built on it, hold the tenant to these
    projects even where the enforcement flag leaves the ACL check off.
    """
    bound = {
        cluster_id: {DEFAULT_PROJECT}
        | {project for (project, _instance) in rules if project != WILDCARD}
        for cluster_id, rules in acl.items()
        if cluster_id in shared
    }
    return bound or None
