"""Which OrcaHub organizations hear about a project on a cluster.

note: OrcaHub events fanned out to every OrcaHub organization bound to the event's cluster, so on a
cluster two parties hold, each one's OrcaHub heard, and could bill, the other's projects (pre-1.0.0
review, 2026-11-06). An organization now hears an event when no other party holds the cluster for
it, or when the event's project is in its bound there: `default` and the projects its party's
policies name (`*` naming every one), the bound its key and its add-ons are held to. A delete names
the organizations that held what it removed, and they hear it although it has left every bound.

OrcaHub organizations are created by the system rather than a person, so the party is organization
level: the organizations sharing an owner key that lends party (organization_owners.PARTY_ADDED_VIA),
which puts a seller and the buyers it provisioned on one side.
"""

from __future__ import annotations

from typing import Collection, Dict, List, Optional, Sequence, Set

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationCluster, TenantAccessPolicy, TenantPolicyRule


async def _parties(db: AsyncSession, org_ids: Collection[int]) -> Dict[int, Set[int]]:
    from app.services.organization_owners import PARTY_ADDED_VIA, owners_of

    owners = await owners_of(db, sorted(set(org_ids)))
    lending = {
        org_id: {row.api_key_id for row in rows if row.added_via in PARTY_ADDED_VIA}
        for org_id, rows in owners.items()
    }
    return {
        org_id: {org_id} | {other for other, keys in lending.items() if keys & lending.get(org_id, set())}
        for org_id in org_ids
    }


async def _named_by(db: AsyncSession, party: Set[int], cluster_id: str) -> Set[str]:
    rows = (await db.execute(
        select(TenantPolicyRule.project_name)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .join(
            OrganizationCluster,
            and_(
                OrganizationCluster.organization_id == TenantAccessPolicy.organization_id,
                OrganizationCluster.cluster_id == TenantPolicyRule.cluster_id,
            ),
        )
        .where(TenantPolicyRule.cluster_id == cluster_id, TenantAccessPolicy.organization_id.in_(sorted(party)))
        .distinct()
    )).scalars().all()
    return {name for name in rows if name}


async def organizations_reaching(
    db: AsyncSession,
    cluster_id: str,
    project: Optional[str],
    orgs: Sequence[Organization],
    *,
    held_by: Collection[int] = (),
    deleted: bool = False,
) -> List[Organization]:
    """The organizations of `orgs` that hear about `project` on `cluster_id`.

    `held_by`: organizations a delete or a Migration Center job names as having held the project;
    an organization whose party includes one hears the event. `deleted`: a project delete, which on a
    cluster another party holds only those hear, so a party that reuses the name is not told its own
    project went away. Raises when the database cannot be read; the caller sends nothing then.
    """
    if not orgs:
        return []
    rows = (await db.execute(
        select(OrganizationCluster.organization_id).where(OrganizationCluster.cluster_id == cluster_id)
    )).scalars().all()
    holders = {org_id for org_id in rows if org_id is not None}
    # note: a row not yet bound to an organization was registered by someone outside every party
    # this can name, so it counts as another holder (cluster_parties.clusters_held_by_another_party).
    unbound = any(org_id is None for org_id in rows)
    held = set(held_by)
    # A holder named by a delete need not be bound here (a buyer's policy), so its keys are read too.
    parties = await _parties(db, holders | {org.id for org in orgs} | held)
    wanted = (project or "").strip() or "default"
    reaching: List[Organization] = []
    for org in orgs:
        party = parties.get(org.id, {org.id})
        if party & held:
            reaching.append(org)
            continue
        if not unbound and holders <= party:
            reaching.append(org)
            continue
        if deleted:
            continue
        names = await _named_by(db, party, cluster_id)
        if wanted == "default" or "*" in names or wanted in names:
            reaching.append(org)
    return reaching
