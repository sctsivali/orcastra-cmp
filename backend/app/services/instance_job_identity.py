"""Durable target-organization identity for asynchronous instance jobs.

Cluster ownership is not enough on a shared cluster: a project may belong to a different
organization than the first cluster binding. Resolve the exact target before a job is accepted
and persist that snapshot so terminal audit records remain attributable after a process restart
or a later ownership change. Ambiguous ownership is recorded explicitly and is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    TenantAccessPolicy,
    TenantPolicyRule,
)

OrganizationResolution = Literal["exact", "ambiguous", "unbound"]


@dataclass(frozen=True)
class InstanceJobOrganization:
    organization_id: Optional[int]
    organization_slug: Optional[str]
    resolution: OrganizationResolution


async def resolve_instance_job_organization(
    db: AsyncSession,
    *,
    cluster_id: str,
    project: str,
) -> InstanceJobOrganization:
    """Resolve an exact project owner without treating a shared default as one tenant's.

    A single, bound cluster owner is exact for every project. On a shared or otherwise
    ambiguous cluster, only one concrete project rule can establish an owner. The ``default``
    project is intentionally visible to every Partner bound to a shared cluster, so it can never
    prove one target organization.
    """

    ownership_rows = (
        await db.execute(
            select(
                OrganizationCluster.organization_id,
                Organization.slug,
            )
            .outerjoin(
                Organization,
                Organization.id == OrganizationCluster.organization_id,
            )
            .where(OrganizationCluster.cluster_id == cluster_id)
        )
    ).all()
    bound = {
        (int(organization_id), str(slug))
        for organization_id, slug in ownership_rows
        if organization_id is not None and slug
    }
    has_unbound = any(organization_id is None for organization_id, _slug in ownership_rows)

    if not bound:
        return InstanceJobOrganization(None, None, "unbound")
    if len(bound) == 1 and not has_unbound:
        organization_id, slug = next(iter(bound))
        return InstanceJobOrganization(organization_id, slug, "exact")
    if project == "default":
        return InstanceJobOrganization(None, None, "ambiguous")

    project_owners = (
        await db.execute(
            select(Organization.id, Organization.slug)
            .join(
                TenantAccessPolicy,
                TenantAccessPolicy.organization_id == Organization.id,
            )
            .join(
                TenantPolicyRule,
                TenantPolicyRule.policy_id == TenantAccessPolicy.id,
            )
            .join(
                OrganizationCluster,
                and_(
                    OrganizationCluster.organization_id == Organization.id,
                    OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
                ),
            )
            .where(
                Organization.is_active == True,  # noqa: E712 - SQLAlchemy expression
                TenantPolicyRule.cluster_id == cluster_id,
                TenantPolicyRule.project_name == project,
            )
            .distinct()
        )
    ).all()
    exact = {(int(organization_id), str(slug)) for organization_id, slug in project_owners}
    if len(exact) == 1:
        organization_id, slug = next(iter(exact))
        return InstanceJobOrganization(organization_id, slug, "exact")
    return InstanceJobOrganization(None, None, "ambiguous")


async def bound_organizations(db: AsyncSession, cluster_id: str) -> List[Tuple[int, str, str]]:
    """The active organizations bound to `cluster_id`, as (id, slug, name), by id.

    For an Admin naming the owner of an ambiguous `default`. An unbound row is never a candidate:
    it names no organization a job could be attributed to.
    """
    rows = (
        await db.execute(
            select(Organization.id, Organization.slug, Organization.name)
            .join(OrganizationCluster, OrganizationCluster.organization_id == Organization.id)
            .where(
                OrganizationCluster.cluster_id == cluster_id,
                Organization.is_active == True,  # noqa: E712 - SQLAlchemy expression
            )
            .distinct()
            .order_by(Organization.id)
        )
    ).all()
    return [(int(org_id), str(slug), str(name or slug)) for org_id, slug, name in rows]
