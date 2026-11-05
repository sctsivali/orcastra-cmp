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

important: an integration key carries an organization here only by an owner row that lends a party
(organization_owners.PARTY_ADDED_VIA): the claim, provisioning or transfer that made it the owner, as
before organizations had several owner keys. A key another owner enrolled, or an Administrator added,
owns the organization without putting it on its minter's side or its minter's key on the
organization's side, so co-ownership never widens who manages, revokes or rotates a key.

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

from datetime import datetime
from typing import Dict, FrozenSet, Iterable, Optional, Set

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

# important: an allowlist, not "anything but tenant". A member added from the dashboard is stored as
# the legacy "member" role, which org_scope and the user list both read as tenant. An "admin" row is
# left out too: a platform admin passes every check by role, so for anyone who reaches these checks
# an admin row is one a later demotion left behind.
RUNNING_ROLES = ("partner",)


async def run_orgs(db: AsyncSession, email: str) -> Dict[int, FrozenSet[str]]:
    """Organizations this user runs (created, or a partner row), mapped to each one's owner keys.

    important: not org_scope._query_user_org_ids, which counts any membership. A partner who also buys from
    another partner is a tenant of that seller's buyer organization, and counting it would put the
    seller's integration, keys and clusters on the buyer's side.
    """
    from app.models.db_models import Organization, OrganizationMember
    from app.services.organization_owners import owners_of

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
    owners = await owners_of(db, [org_id for org_id, _primary in rows])
    return {org_id: frozenset(row.api_key_id for row in owners.get(org_id, [])) for org_id, _primary in rows}


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
    from app.models.db_models import Organization, OrganizationOwnerKey
    from app.services.organization_owners import lends_party

    if not email:
        return set()
    created = set((await db.execute(
        select(OrganizationOwnerKey.api_key_id)
        .join(Organization, Organization.id == OrganizationOwnerKey.organization_id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            Organization.created_by == email,
            lends_party(),
        )
    )).scalars().all())
    return created | await home_partner_keys(db, email)


async def home_partner_keys(db: AsyncSession, email: str, *, settled_before: Optional[datetime] = None) -> Set[str]:
    """The keys whose home organization this user holds a partner row in: side_keys without the
    organizations the user created. The home, and why it must be proven by a sale, is side_keys'. With
    `settled_before`, only a partner row that joined by then counts (owner_key_enrollment.party_check).

    note: the half of side_keys that a current role decides. created_by is the key's minter on every
    buyer organization provisioning made, so it says nothing about who runs the seller today.
    """
    from app.models.db_models import Organization, OrganizationCluster, OrganizationMember, OrganizationOwnerKey
    from app.services.organization_owners import lends_party

    if not email:
        return set()
    # note: per key in each organization's owner set, counting only the owner rows that lend a party
    # (organization_owners.PARTY_ADDED_VIA): an enrolled or Administrator-added key carries no
    # organization here. A co-owner's home is its own oldest synced organization and its own sale,
    # judged exactly as a sole owner's.
    rows = (await db.execute(
        select(OrganizationOwnerKey.api_key_id, Organization.id)
        .join(Organization, Organization.id == OrganizationOwnerKey.organization_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            OrganizationMember.user_email == email,
            OrganizationMember.role.in_(RUNNING_ROLES),
            lends_party(),
            *([OrganizationMember.joined_at <= settled_before] if settled_before is not None else []),
        )
    )).all()
    if not rows:
        return set()
    homes = dict((await db.execute(
        select(OrganizationOwnerKey.api_key_id, func.min(Organization.id))
        .join(Organization, Organization.id == OrganizationOwnerKey.organization_id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            Organization.created_by == "system",
            OrganizationOwnerKey.api_key_id.in_(sorted({key for key, _org in rows})),
            lends_party(),
        )
        .group_by(OrganizationOwnerKey.api_key_id)
    )).all())
    # important: the registered cluster must also carry another organization on the same key, a
    # cluster the key sold on, and the home's binding must be the first of that key's bindings there
    # (by row id, which a rebind renews and reconcile keeps). A buyer founder can register a cluster
    # of its own, or unbind and bind the seller's cluster again, but never ahead of the buyers.
    # important: and the sale must be a binding provisioning made (no registrant). A founder whose
    # buyer is the oldest synced organization on the key could otherwise bind a cluster of their own
    # to it and to a second buyer on the key, which reads as exactly this sale. (Not on the
    # v1.0.0-RC4-hotfix2 line, which counts a binding of any origin.)
    binding, sold = aliased(OrganizationCluster), aliased(OrganizationCluster)
    home_owner, buyer_owner = aliased(OrganizationOwnerKey), aliased(OrganizationOwnerKey)
    earlier, earlier_owner = aliased(OrganizationCluster), aliased(OrganizationOwnerKey)
    first = ~exists().where(
        earlier.cluster_id == binding.cluster_id,
        earlier.organization_id != binding.organization_id,
        earlier_owner.organization_id == earlier.organization_id,
        earlier_owner.api_key_id == home_owner.api_key_id,
        lends_party(earlier_owner.added_via),
        earlier.id < binding.id,
    )
    registering = set((await db.execute(
        select(home_owner.api_key_id, binding.organization_id)
        .select_from(binding)
        .join(home_owner, home_owner.organization_id == binding.organization_id)
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
        .join(buyer_owner, and_(
            buyer_owner.organization_id == sold.organization_id,
            buyer_owner.api_key_id == home_owner.api_key_id,
            lends_party(buyer_owner.added_via),
        ))
        .where(
            binding.organization_id.in_(sorted(set(homes.values()))),
            home_owner.api_key_id.in_(sorted(homes)),
            lends_party(home_owner.added_via),
            first,
        )
    )).all())
    return {key for key, org in rows if homes.get(key) == org and (key, org) in registering}


async def run_org_keys(db: AsyncSession, email: str) -> Set[str]:
    """The integration keys this user holds (side_keys), which key management reads as "the keys I sync"."""
    return await side_keys(db, email)


async def member_orgs(db: AsyncSession, email: str) -> Set[int]:
    """Active organizations where this user holds a `member` row: a teammate added from Members.

    note: not part of `run_orgs`, which every whole-cluster check reads; only the view-only
    `default` rule counts a teammate as its organization's side (owner decision 2026-11-05).
    """
    from app.models.db_models import Organization, OrganizationMember

    if not email:
        return set()
    rows = (await db.execute(
        select(OrganizationMember.organization_id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            func.lower(func.trim(OrganizationMember.user_email)) == email.strip().lower(),
            OrganizationMember.role == "member",
        )
    )).scalars().all()
    return set(rows)


async def clusters_held_by_another_party(
    db: AsyncSession, email: str, cluster_ids: Iterable[str], also_runs: Iterable[int] = (),
) -> Set[str]:
    """Clusters on which some holder is outside the caller's party.

    An unbound row registered by someone else counts as another holder, as it does in
    org_scope._clusters_with_several_orgs. `also_runs` adds organizations the caller's side holds
    for one rule only (org_scope._default_view_only's teammates).
    """
    from app.models.db_models import OrganizationCluster
    from app.services.organization_owners import PARTY_ADDED_VIA, owners_of

    wanted = sorted(set(cluster_ids))
    if not wanted:
        return set()
    rows = (await db.execute(
        select(
            OrganizationCluster.cluster_id,
            OrganizationCluster.organization_id,
            OrganizationCluster.registered_by,
        )
        .where(OrganizationCluster.cluster_id.in_(wanted))
    )).all()
    owners = await owners_of(db, [org_id for _c, org_id, _r in rows if org_id is not None])
    runs = await run_orgs(db, email)
    extra = set(also_runs)
    keys = await side_keys(db, email)
    held_elsewhere: Set[str] = set()
    for cluster_id, org_id, registered_by in rows:
        if org_id is None:
            if (registered_by or "").lower() != (email or "").lower():
                held_elsewhere.add(cluster_id)
        elif org_id in runs or org_id in extra or {row.api_key_id for row in owners.get(org_id, []) if row.added_via in PARTY_ADDED_VIA} & keys:
            continue
        else:
            held_elsewhere.add(cluster_id)
    return held_elsewhere
