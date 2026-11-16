"""Who holds a cluster, decided by party rather than by how many organizations are bound to it.

Provisioning binds each buyer organization to its partner's cluster, so a binding count
(org_scope._clusters_with_several_orgs, used by the project bound) calls every cluster with a sale
"shared". The whole-cluster checks and key management (integrations._keys_the_caller_manages) ask
here instead, so a buyer's founder cannot manage the seller's key. The caller's party is the
organizations they run (created, or a partner row) plus every organization carrying a key they hold
(side_keys), read from the database alone. A partner row lends a key only in the key's home
organization: OrcaHub makes every founder a PARTNER, so a synced buyer carries its founder as a
partner row beside the seller's key.

important: nothing here asks Vault who minted a key (a minter can be an admin in several partners'
organizations, or the key can be deleted while its organizations stay). A key carries an
organization only through an owner row that lends a party (organization_owners.PARTY_ADDED_VIA),
so an enrolled or Administrator-added co-owner key never widens who manages, revokes or rotates a
key.

A key itself also counts the synced organizations it sold onto a cluster (key_sale_orgs), for the
rules that judge that key alone (key_held_by_another_party): its bound, its view-only `default` and
whether its purchase skips the whole-cluster audit. Who manages a key and the browser keep the
party above.

Known limits, narrowing except the last: a teammate of a partner whose home is local does not run
the buyers its key provisioned; a synced buyer under a key no run organization carries is another
party's; a rotation to a new key leaves organizations on the old one (rotate the secret in place);
a teammate added from the dashboard is a member and does not run that organization; a key's minter
is created_by on every buyer it provisioned, so a minter who left the team still runs those buyers.
"""

from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Set

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

# important: keep this an allowlist. A member added from the dashboard is stored as the legacy
# "member" role, which org_scope and the user list both read as tenant. An "admin" row is left out
# too: a platform admin passes every check by role, so an admin row reaching these checks is one a
# later demotion left behind.
RUNNING_ROLES = ("partner",)


async def run_orgs(db: AsyncSession, email: str) -> Dict[int, FrozenSet[str]]:
    """Organizations this user runs (created, or a partner row), mapped to each one's owner keys.

    important: unlike org_scope._query_user_org_ids, plain membership does not count. A partner who also buys
    from another partner is a tenant of that seller's buyer organization, and counting it would put the
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
    sync created for the key (created_by "system"; OrcaHub sends the connection's own organization
    first), and counts only when it holds a cluster one of its own partners registered and the key
    sold on (another organization on the key is bound to it). A buyer is bound to the seller's
    cluster by provisioning, never by its founder registering it, so when the oldest one is a buyer
    (the seller's home was deleted and synced again, or the seller's own org belongs to another key)
    no partner row lends the key and this fails closed. A partner row in any other keyed
    organization, a synced buyer founder's for one, counts that organization only.
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
    organizations the user created. side_keys explains the home and why a sale must prove it. With
    `settled_before`, only a partner row that joined by then counts (owner_key_enrollment.party_check).

    This is the half of side_keys that a current role decides: created_by is the key's minter on
    every buyer organization provisioning made, so it says nothing about who runs the seller today.
    """
    from app.models.db_models import Organization, OrganizationCluster, OrganizationMember, OrganizationOwnerKey
    from app.services.organization_owners import lends_party

    if not email:
        return set()
    # Per key in each organization's owner set, counting only the owner rows that lend a party
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
    # The sale must also be a binding provisioning made (no registrant). Otherwise a founder whose
    # buyer is the oldest synced organization on the key could bind a cluster of their own to it and
    # to a second buyer on the key, which reads as exactly this sale.
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
            sold.granted_by.is_(None),
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

    Not part of `run_orgs`, which every whole-cluster check reads; only the view-only `default`
    rule counts a teammate as its organization's side.
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


async def default_view_only_clusters(db: AsyncSession, email: str, cluster_ids: Iterable[str]) -> Set[str]:
    """The clusters among `cluster_ids` where `default` is view only for this user's side.

    The rule the browser (org_scope._default_view_only) and snapshot schedules read: another party
    holds the cluster, counting the user's teammate member rows as its own side. A key reads
    key_held_by_another_party with the same teammates (key_default_view_only), so the buyers that
    key sold onto the cluster count on its side too.
    """
    wanted = set(cluster_ids)
    if not wanted:
        return set()
    teammates = await member_orgs(db, email)
    held = await clusters_held_by_another_party(db, email, wanted, also_runs=teammates)
    return held & wanted


def party_key_id(api_key: Mapping[str, Any]) -> str:
    """The id whose sale organizations join a key's party (key_sale_orgs): "" for an add-on
    credential, which a third party holds and whose bound is judged by its installer alone."""
    from app.services.add_on_credential_edits import is_add_on_credential

    if is_add_on_credential(api_key):
        return ""
    return str(api_key.get("id") or "")


async def key_sale_orgs(db: AsyncSession, api_key_id: Optional[str], cluster_ids: Iterable[str]) -> Set[int]:
    """The synced organizations this key sold onto one of these clusters, which join its party there.

    An active OrcaHub organization counts when the key owns it through an owner row that lends a
    party (organization_owners.PARTY_ADDED_VIA; with owner sets off it must also be the primary,
    as organization_owners.owned_by_clause has it) and it holds the cluster by a binding
    provisioning made (no registrant and no grantor, core/sale_term.py). A buyer the sync created
    (created_by "system") otherwise joins the seller's party only through home_partner_keys,
    which fails closed, so the key that sold to it read it as another party's.

    important: pass the id from party_key_id, never an add-on credential's. Callers judge one
    cluster per call: an organization counts only on the cluster it holds by sale. An enrolled or
    Administrator-added owner row, a registered or granted binding, a local organization and an
    inactive one never count, so another key's buyer, an unbound registration and anything an
    Administrator bound still make the cluster another party's.
    """
    from app.models.db_models import Organization, OrganizationCluster, OrganizationOwnerKey
    from app.services.organization_owners import lends_party, owned_by_clause

    wanted = sorted(set(cluster_ids))
    if not api_key_id or not wanted:
        return set()
    rows = (await db.execute(
        select(Organization.id)
        .join(OrganizationCluster, OrganizationCluster.organization_id == Organization.id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            Organization.source == "orcahub",
            OrganizationCluster.cluster_id.in_(wanted),
            OrganizationCluster.registered_by.is_(None),
            OrganizationCluster.granted_by.is_(None),
            owned_by_clause([api_key_id]),
            exists().where(
                OrganizationOwnerKey.organization_id == Organization.id,
                OrganizationOwnerKey.api_key_id == api_key_id,
                lends_party(),
            ),
        )
        .distinct()
    )).scalars().all()
    return set(rows)


async def key_held_by_another_party(
    db: AsyncSession, creator: str, api_key_id: Optional[str], cluster_id: str, *, also_runs: Iterable[int] = (),
) -> bool:
    """Whether a key acting for `creator` is judged as on a cluster another party holds.

    The one rule a key's bound (key_party_bound), its view-only `default` (key_default_view_only)
    and the provisioning share audit (integrations._held_outside_the_key_party) read: some holder
    is outside the creator's party once the key's own sale organizations (key_sale_orgs) join it,
    or the creator reaches the cluster only through a purchase (sale_term.reaches_only_by_purchase),
    which keeps a buyer founder's key held whatever the key owns. `also_runs` adds organizations
    for one rule only (the view-only rule's teammates). A key with no known creator has no party,
    so its sale organizations do not count for it.
    """
    from app.core.sale_term import reaches_only_by_purchase

    side = set(also_runs)
    if (creator or "").strip():
        side |= await key_sale_orgs(db, api_key_id, {cluster_id})
    if cluster_id in await clusters_held_by_another_party(db, creator, {cluster_id}, also_runs=side):
        return True
    return await reaches_only_by_purchase(db, creator, cluster_id)


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
