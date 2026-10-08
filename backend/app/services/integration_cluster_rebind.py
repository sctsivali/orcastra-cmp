"""Which personal clusters an integration push may rebind, where they go, and the record each leaves.

A member sync ran reconcile_personal_clusters for every pushed email. A key that pushed another
partner's email into its own organization moved that partner's unbound clusters into it, where the
key's own partner is a member. The share audit on that path checks a cluster's configuration, not
anyone's consent.

The rule: a push rebinds personal clusters only for the key's creator, the `created_by` that
create_api_key writes into the key's metadata when a partner mints it. Any other pushed email
triggers no rebind, and neither does a key without a creator or an add-on credential. Every
rebind is recorded with the cluster, the organization it left, the one it joined and the key id.
Nothing else of the key reaches the record.

The push's `organizations` list must name exactly one organization, and after the push the key
must claim exactly that one: organizations.api_key_id is the key, active or not, for it and for
no other. A key claims an organization through the three claim paths (the sync upsert,
provisioning and the dashboard's refresh) or through an Administrator's owner transfer
(services/integration_key_ownership.py), which moves an organization between keys and never
releases one. A seller's key also claims every buyer organization it syncs or provisions, and the
pushed role cannot tell the seller's organization from a buyer's. In OrcaHub a buyer
organization's partner can add the seller's creator as PARTNER, and the seller's push carries
every member of every buyer organization. The one organization a key claims can itself be a
buyer's: a new key or a second connection is refused the seller organization another key holds,
and a seller slug a local organization holds is skipped by every push. So a push naming more than
one organization, or a key claiming more than one, rebinds nothing and logs the key id and the
count, and the partner binds those clusters from the dashboard.

A buyer organization is then the destination only when a push names it alone and the key claims
nothing else. OrcaHub v1.32.0 lists the connection's own organization first in every push while
that organization exists (orcastra.service.js syncOrcastraClustersUnlocked), so a one-org push from
it names the seller's own organization, and what a buyer organization's roster says never changes
where or whether the creator's clusters move.

The destination is that one organization, if it is active and this push names the creator a
partner there: the pushed role maps to partner the way the member sync maps it, and the
membership row read back holds partner. It is not the creator's primary organization from
org_scope: that ordering puts any organization where the creator is a partner first, so another
key could push the creator into its own older organization and choose where the creator's next
rebind lands.

A creator the dashboard resolves to Admin never rebinds: role_admin, or any other group rbac.py
maps to Admin. Their key may be held by an integration they do not run, and an Admin's personal
clusters are not a partner's. The role is read from Authentik at the time of the push, because
nothing else holds it: the key's metadata records who minted it, not their role, and there is no
users table. A role Authentik cannot answer for rebinds nothing: a user search or detail read that
fails, times out or answers anything but 200, an email no account carries and one that two
accounts carry (authentik_role_reading).

A rebind that fails partway is rolled back, so no move is committed without its record.

note: the match is exact. `registered_by` holds the registrant's login email and the creator is
that same login, so another spelling would find no row to move anyway.
"""

import logging
from typing import Any, List, Mapping, Optional, Protocol, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.org_scope import reconcile_personal_clusters
from app.core.rbac import Role
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services.activity_logger import get_activity_logger
from app.services.add_on_credential_edits import is_add_on_credential
from app.services.authentik_role_reading import CurrentRoleReader
from app.services.integration_roles import stored_member_role

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

REBIND_ACTION = "integration.cluster.rebind"


class PushedMember(Protocol):
    """The fields of a pushed member this module reads."""

    email: str
    organization_id: str
    role: str
    action: str


def creator_of(api_key: Mapping[str, Any]) -> Optional[str]:
    """The login that minted the key, or None for a key whose metadata does not say.

    important: None for an add-on credential. Its created_by is whoever installed or rotated it,
    an Admin included, while a third party holds it. A push through it naming that login could
    make an organization the add-on controls that person's primary one and move their clusters
    there, which is the move this module exists to prevent.
    """
    if is_add_on_credential(api_key):
        return None
    creator = api_key.get("created_by")
    return creator if isinstance(creator, str) and creator.strip() else None


def may_rebind(email: str, creator: Optional[str]) -> bool:
    return creator is not None and email == creator


async def _has_unbound_clusters(db: AsyncSession, email: str) -> bool:
    row = (await db.execute(
        select(OrganizationCluster.id).where(
            OrganizationCluster.organization_id.is_(None),
            OrganizationCluster.registered_by == email,
        ).limit(1)
    )).first()
    return row is not None


async def claimed_organization_count(db: AsyncSession, api_key_id: str) -> int:
    """How many organizations this key claimed, active or not.

    note: an inactive organization counts. It keeps its api_key_id, this key's next push naming it
    makes it active again, and no dashboard path switches one off, so it is still a party the key
    serves. Leaving it out would make a buyer organization the key's only one whenever the seller's
    own organization is inactive, and the buyer could then name the creator into it.

    note: deliberately wider than integration_key_ownership.owned_organizations, which counts only
    the active OrcaHub organizations a key's end would freeze.
    """
    if not api_key_id:
        return 0
    counted = await db.execute(
        select(func.count()).select_from(Organization).where(Organization.api_key_id == api_key_id)
    )
    return int(counted.scalar_one())


async def claims_organization(db: AsyncSession, api_key_id: str, external_id: str) -> bool:
    """Whether this key claimed the OrcaHub organization with this external id."""
    if not api_key_id:
        return False
    row = (await db.execute(
        select(Organization.id).where(
            Organization.api_key_id == api_key_id,
            Organization.source == "orcahub",
            Organization.external_id == external_id,
        ).limit(1)
    )).first()
    return row is not None


async def claimed_destinations(
    db: AsyncSession, members: Sequence[PushedMember], *, email: str, api_key_id: str
) -> List[int]:
    """Active organizations this key claimed that this push names `email` a partner of.

    note: the membership row is read back too, so an upsert that failed, a later remove in the same
    push or a row holding another role leaves the organization out.
    """
    named = {
        m.organization_id for m in members
        if m.email == email and m.action != "remove" and stored_member_role(m.role) == "partner"
    }
    if not named or not api_key_id:
        return []
    rows = await db.execute(
        select(Organization.id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            Organization.source == "orcahub",
            Organization.external_id.in_(sorted(named)),
            Organization.api_key_id == api_key_id,
            Organization.is_active.is_(True),
            OrganizationMember.user_email == email,
            OrganizationMember.role == "partner",
        )
        .distinct()
    )
    return sorted(rows.scalars().all())


async def _creator_role_allows_rebind(roles: CurrentRoleReader, email: str, api_key_id: str) -> bool:
    """False for a creator the dashboard resolves to Admin, and for one whose role is unknown."""
    reading = await roles.read_current_roles(email)
    if reading.roles is None:
        logger.warning(
            "Integration key %s rebinds no personal cluster: its creator's role could not be read (%s)",
            api_key_id, reading.reason,
        )
        return False
    if Role.ADMIN in reading.roles:
        logger.warning(
            "Integration key %s rebinds no personal cluster: its creator is an Admin", api_key_id
        )
        return False
    return True


def _record(cluster_id: str, from_org: Optional[int], to_org: int, email: str, api_key_id: str) -> None:
    try:
        activity_logger.log_activity(
            action=REBIND_ACTION,
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            target_organization_id=to_org,
            details={
                "cluster_id": cluster_id,
                "from_organization_id": from_org,
                "to_organization_id": to_org,
                "registered_by": email,
                "api_key_id": api_key_id,
            },
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the sync
        logger.warning("Could not write the audit record for %s", REBIND_ACTION, exc_info=True)


async def rebind_creator_clusters(
    db: AsyncSession,
    email: str,
    members: Sequence[PushedMember],
    *,
    api_key_id: str,
    creator: Optional[str],
    roles: CurrentRoleReader,
    pushed_organizations: Sequence[str],
) -> int:
    """Bind the creator's unbound clusters to the one organization this push names and this key claims.

    `pushed_organizations` holds the external ids of the push's `organizations` list.
    """
    if not may_rebind(email, creator):
        return 0
    if not await _has_unbound_clusters(db, email):
        return 0
    claimed = await claimed_organization_count(db, api_key_id)
    if claimed != 1:
        logger.warning(
            "Integration key %s rebinds no personal cluster: it claims %d organizations, "
            "exactly one is required",
            api_key_id, claimed,
        )
        return 0
    named = set(pushed_organizations)
    if len(named) != 1:
        logger.warning(
            "Integration key %s rebinds no personal cluster: its push names %d organizations, "
            "exactly one is required",
            api_key_id, len(named),
        )
        return 0
    if not await claims_organization(db, api_key_id, next(iter(named))):
        logger.warning(
            "Integration key %s rebinds no personal cluster: the organization its push names is "
            "not the one it claims",
            api_key_id,
        )
        return 0
    destinations = await claimed_destinations(db, members, email=email, api_key_id=api_key_id)
    if len(destinations) != 1:
        logger.warning(
            "Integration key %s rebinds no personal cluster: %d organizations it claimed name "
            "its creator in this push, exactly one is required",
            api_key_id, len(destinations),
        )
        return 0
    if not await _creator_role_allows_rebind(roles, email, api_key_id):
        return 0
    moved: List[Tuple[str, Optional[int], int]] = []
    try:
        rebound = await reconcile_personal_clusters(
            db, email,
            on_rebind=lambda cluster_id, from_org, to_org: moved.append((cluster_id, from_org, to_org)),
            organization_id=destinations[0],
        )
    except Exception:
        # important: each move is flushed before the one commit at the end, so a later cluster the
        # share audit refused left the earlier moves in the session, and the next commit of this
        # request wrote them without a record. Nothing of a failed rebind may survive it.
        await db.rollback()
        raise
    # note: recorded after reconcile_personal_clusters committed, so a rolled-back move leaves no record
    for cluster_id, from_org, to_org in moved:
        _record(cluster_id, from_org, to_org, email, api_key_id)
    return rebound
