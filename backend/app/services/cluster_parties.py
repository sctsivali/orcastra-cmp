"""Who holds a cluster, decided by party rather than by how many organizations are bound to it.

A cluster carries one binding per organization. Counting bindings calls every partner cluster
with a sale "shared", because provisioning binds each buyer organization to the partner's
cluster. The question "may this partner manage this integration key" needs the other answer: is
any binding held by someone outside the caller's party.

The caller's party is decided from the database alone:
  * the organizations they run: created, or joined as partner
  * any organization keyed to an integration key they hold (side_keys): the key of an
    organization they created, or of the key's home organization when they hold a partner row
    there (the key that syncs the partner's home organization is the key that provisioned its
    buyers). A partner row elsewhere lends no key: OrcaHub makes every organization's founder a
    PARTNER, so a buyer synced from OrcaHub carries its founder as a partner row and the seller's key.

note: two notions of "shared" live side by side on this line, on purpose. The partner project
bound and the whole-cluster checks in core/org_scope.py count bindings
(org_scope._clusters_with_several_orgs). Key management (routers/integrations.py,
_keys_the_caller_manages) asks this module, by party, because a binding count would let a buyer's
founder, who holds a partner row in a buyer organization on the seller's cluster, manage the
seller's integration key. A shortcut such as "a key that syncs an organization the caller runs"
has exactly that hole; only the home-organization rule in side_keys closes it.

important: nothing here asks Vault who minted a key. A minter can be an admin who is also a member
of several partners' organizations, or the key can be deleted while its organizations stay, and
both put one partner's organizations on another partner's side. The integration key an
organization carries is a fact the database holds and a caller cannot move.

note: known limits, all narrowing except the last. A teammate of a partner whose home org is local
does not run the buyers that partner's key provisioned. A buyer created by a sync (created_by
"system") under a key no run org carries is another party's. A rotation by a new key leaves the old
orgs on the old key (rotate the secret in place instead). A teammate added from the dashboard is
stored as a member, not a partner, so it does not run that organization. And provisioning stamps
a buyer with its key's minter as created_by, so a minter who left the team still runs those
buyers, as before.

Ported from the v1.0.0-RC4-hotfix2 line, where the same module also confines a tenant's '*' rule
(clusters_another_org_holds). That half is not part of this line.
"""

from typing import Dict, Iterable, Optional, Set

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

# important: an allowlist, not "anything but tenant". A member added from the dashboard is stored as
# the legacy "member" role, which org_scope and the user list both read as tenant. An "admin" row is
# left out too: a platform admin passes every check by role, so for anyone who reaches these checks
# an admin row is one a later demotion left behind.
RUNNING_ROLES = ("partner",)


async def run_orgs(db: AsyncSession, email: str) -> Dict[int, Optional[str]]:
    """Organizations this user runs (created, or a partner row), mapped to each one's key.

    important: not org_scope._query_user_org_ids, which counts any membership. A partner who also buys from
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


async def side_keys(db: AsyncSession, email: str) -> Set[str]:
    """The integration keys that put every organization carrying them on this user's side.

    important: a key counts through an organization the user created, or through the key's home
    organization when the user holds a partner row there. The home is the oldest organization the
    sync created for the key (created_by "system"), since OrcaHub sends the connection's own
    organization first, and only when it holds a cluster one of its own partners registered and the
    key sold on (another organization on the key is bound to it). A
    buyer is bound to the seller's cluster by provisioning, never by its founder registering it,
    so when the oldest one is a buyer (the seller's home was deleted and synced again, or the
    seller's own org belongs to another key) no partner row lends the key: this fails closed. A
    partner row in any other keyed organization, a synced buyer founder's for one, counts that
    organization only.
    """
    from app.models.db_models import Organization, OrganizationCluster, OrganizationMember

    if not email:
        return set()
    created = set((await db.execute(
        select(Organization.api_key_id).where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            Organization.created_by == email,
            Organization.api_key_id.is_not(None),
        )
    )).scalars().all())
    rows = (await db.execute(
        select(Organization.api_key_id, Organization.id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            Organization.api_key_id.is_not(None),
            OrganizationMember.user_email == email,
            OrganizationMember.role.in_(RUNNING_ROLES),
        )
    )).all()
    if not rows:
        return created
    homes = dict((await db.execute(
        select(Organization.api_key_id, func.min(Organization.id))
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            Organization.created_by == "system",
            Organization.api_key_id.in_(sorted({key for key, _org in rows})),
        )
        .group_by(Organization.api_key_id)
    )).all())
    # important: the registered cluster must also carry another organization on the same key, a
    # cluster the key sold on, and the home's binding must be the first of that key's bindings there
    # (by row id, which a rebind renews and reconcile keeps). A buyer founder can register a cluster
    # of its own, or unbind and bind the seller's cluster again, but never ahead of the buyers.
    # important: and the sale must be a binding provisioning made (no registrant). A founder whose
    # buyer is the oldest synced organization on the key could otherwise bind a cluster of their own
    # to it and to a second buyer on the key, which reads as exactly this sale. (Not on the
    # v1.0.0-RC4-hotfix2 line, which counts a binding of any origin.)
    binding, sold, home, buyer = (aliased(OrganizationCluster), aliased(OrganizationCluster),
                                  aliased(Organization), aliased(Organization))
    earlier, earlier_org = aliased(OrganizationCluster), aliased(Organization)
    first = ~exists().where(
        earlier.cluster_id == binding.cluster_id,
        earlier.organization_id != binding.organization_id,
        earlier.organization_id == earlier_org.id,
        earlier_org.api_key_id == home.api_key_id,
        earlier.id < binding.id,
    )
    registering = set((await db.execute(
        select(binding.organization_id)
        .join(home, home.id == binding.organization_id)
        .join(OrganizationMember, and_(
            OrganizationMember.organization_id == binding.organization_id,
            func.lower(OrganizationMember.user_email) == func.lower(binding.registered_by),
            OrganizationMember.role.in_(RUNNING_ROLES),
        ))
        .join(sold, and_(
            sold.cluster_id == binding.cluster_id,
            sold.organization_id != binding.organization_id,
            sold.registered_by.is_(None),
        ))
        .join(buyer, and_(buyer.id == sold.organization_id, buyer.api_key_id == home.api_key_id))
        .where(binding.organization_id.in_(sorted(set(homes.values()))), first)
    )).scalars().all())
    return created | {key for key, org in rows if homes.get(key) == org and org in registering}


async def run_org_keys(db: AsyncSession, email: str) -> Set[str]:
    """The integration keys this user holds (side_keys), which key management reads as "the keys I sync"."""
    return await side_keys(db, email)


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
    keys = await side_keys(db, email)
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
