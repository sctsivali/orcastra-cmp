"""Who holds a cluster, decided by party rather than by how many organizations are bound to it.

A cluster carries one binding per organization. Counting bindings calls every partner cluster
with a sale "shared", because provisioning binds each buyer organization to the partner's
cluster. The questions "may this partner grant, manage or reach the whole of this cluster" need
the other answer: is any binding held by someone outside the caller's party.

The caller's party is decided from the database alone:
  * the organizations they run: created, or joined in a role above tenant
  * any organization keyed to the same integration key as one of those (the key that syncs the
    partner's home organization is the key that provisioned its buyers)

important: nothing here asks who minted a key. A minter can be an admin who is also a member of
several partners' organizations, can have left, or the key can be deleted while its organizations
stay, and each of those put one partner's organizations on another partner's side. The
integration key an organization carries is a fact the database holds and a caller cannot move.
"""

from typing import Dict, Iterable, Optional, Set

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


async def run_orgs(db: AsyncSession, email: str) -> Dict[int, Optional[str]]:
    """Organizations this user runs, mapped to the integration key each carries.

    important: not get_org_ids_for_email, which counts any membership. A partner who also buys from
    another partner is a tenant of that seller's buyer organization, and counting it would put the
    seller's integration, keys and clusters on the buyer's side.
    """
    from app.models.db_models import Organization, OrganizationMember

    if not email:
        return {}
    rows = (await db.execute(
        select(Organization.id, Organization.api_key_id)
        .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            or_(
                Organization.created_by == email,
                (OrganizationMember.user_email == email) & (OrganizationMember.role != "tenant"),
            ),
        )
    )).all()
    return {org_id: key for org_id, key in rows}


async def run_org_keys(db: AsyncSession, email: str) -> Set[str]:
    """The integration keys carried by the organizations this user runs."""
    return {key for key in (await run_orgs(db, email)).values() if key}


async def clusters_held_by_another_party(
    db: AsyncSession, email: str, cluster_ids: Iterable[str]
) -> Set[str]:
    """Clusters on which some holder is outside the caller's party.

    An unbound row registered by someone else counts as another holder, as it does in
    org_scope._clusters_with_several_orgs.
    """
    from app.models.db_models import Organization, OrganizationCluster

    wanted = sorted(set(cluster_ids))
    if not wanted:
        return set()
    rows = (await db.execute(
        select(
            OrganizationCluster.cluster_id,
            OrganizationCluster.organization_id,
            OrganizationCluster.registered_by,
            Organization.api_key_id,
        )
        .outerjoin(Organization, OrganizationCluster.organization_id == Organization.id)
        .where(OrganizationCluster.cluster_id.in_(wanted))
    )).all()
    runs = await run_orgs(db, email)
    keys = {key for key in runs.values() if key}
    held_elsewhere: Set[str] = set()
    for cluster_id, org_id, registered_by, org_key in rows:
        if org_id is None:
            if (registered_by or "").lower() != (email or "").lower():
                held_elsewhere.add(cluster_id)
        elif org_id in runs or (org_key and org_key in keys):
            continue
        else:
            held_elsewhere.add(cluster_id)
    return held_elsewhere
