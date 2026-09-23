"""Who holds a cluster, decided by party rather than by how many organizations are bound to it.

A cluster carries one binding per organization. Counting bindings calls every partner cluster
with a sale "shared", because provisioning binds each buyer organization to the partner's
cluster. The questions "may this partner grant, manage or reach the whole of this cluster" need
the other answer: is any binding held by someone outside the caller's party.

The caller's party is decided from the database alone:
  * the organizations they run: created, or joined as partner
  * any organization keyed to the same integration key as one of those (the key that syncs the
    partner's home organization is the key that provisioned its buyers)

important: nothing here asks Vault who minted a key. A minter can be an admin who is also a member
of several partners' organizations, or the key can be deleted while its organizations stay, and
both put one partner's organizations on another partner's side. The integration key an
organization carries is a fact the database holds and a caller cannot move.

note: known limits, all narrowing except the last. A teammate of a partner whose home org is local
does not run the buyers that partner's key provisioned. A buyer created by a sync (created_by
"system") under a key no run org carries is another party's. A rotation by a new key leaves the old
orgs on the old key (rotate the secret in place instead). And provisioning stamps a buyer with its
key's minter as created_by, so a minter who left the team still runs those buyers, as before.
"""

from typing import Dict, Iterable, Optional, Set

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# important: an allowlist, not "anything but tenant". A member added from the dashboard is stored as
# the legacy "member" role, which org_scope and the user list both read as tenant. An "admin" row is
# left out too: a platform admin passes every check by role, so for anyone who reaches these checks
# an admin row is one a later demotion left behind.
RUNNING_ROLES = ("partner",)


async def run_orgs(db: AsyncSession, email: str) -> Dict[int, Optional[str]]:
    """Organizations this user runs (created, or a partner row), mapped to each one's key.

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
                (OrganizationMember.user_email == email) & OrganizationMember.role.in_(RUNNING_ROLES),
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


async def clusters_outside_org_party(
    db: AsyncSession, org_ids: Iterable[int], cluster_ids: Iterable[str]
) -> Dict[int, Set[str]]:
    """For each organization, the clusters among cluster_ids that carry a binding outside its party.

    An organization's party is itself and every organization carrying its integration key, which
    is how a partner's home organization and the buyers its key provisioned belong together. An
    unbound row counts as outside, as it does in org_scope._clusters_with_several_orgs.
    """
    from app.models.db_models import Organization, OrganizationCluster

    orgs, clusters = sorted(set(org_ids)), sorted(set(cluster_ids))
    if not orgs or not clusters:
        return {org: set() for org in orgs}
    keys = dict((await db.execute(
        select(Organization.id, Organization.api_key_id).where(Organization.id.in_(orgs))
    )).all())
    rows = (await db.execute(
        select(OrganizationCluster.cluster_id, OrganizationCluster.organization_id, Organization.api_key_id)
        .outerjoin(Organization, OrganizationCluster.organization_id == Organization.id)
        .where(OrganizationCluster.cluster_id.in_(clusters))
    )).all()
    outside: Dict[int, Set[str]] = {org: set() for org in orgs}
    for org in orgs:
        key = keys.get(org)
        for cluster_id, holder, holder_key in rows:
            if holder == org or (holder is not None and key and holder_key == key):
                continue
            outside[org].add(cluster_id)
    return outside
