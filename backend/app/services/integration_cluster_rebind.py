"""Which personal clusters an integration push may rebind, where they go, and the record each leaves.

Stops a key from pushing another partner's email into its own organization to move that partner's
unbound clusters (the share audit on that path checks only cluster configuration). Each move is
audited, a partial rebind rolls back, and `registered_by` must equal the creator's login exactly.

- Only the key's creator (`created_by`) is rebound. Other pushed emails, a key without a creator and
  an add-on credential rebind nothing.
- The push names exactly one organization and the key claims exactly that one (api_key_id, active
  or not). A seller's key also claims its buyers' and the pushed role cannot tell them apart, so
  anything wider rebinds nothing; the partner binds those clusters from the dashboard.
- The destination is active, the push names the creator a partner there, and the membership row
  reads back partner. org_scope's primary organization is not used: another key could push the
  creator into an older one and steer the next rebind.
- A creator that resolves to Admin, or whose role Authentik cannot answer for
  (authentik_role_reading), never rebinds. The role is read at push time.
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
from app.services.organization_owners import owned_by_clause

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
    move that person's clusters into an organization the add-on controls.
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

    An inactive organization counts: it keeps its api_key_id, this key's next push naming it makes
    it active again, and no dashboard path switches one off. Leaving it out would make a buyer
    organization the key's only one whenever the seller's own organization is inactive, and the
    buyer could then name the creator into it.

    Wider than integration_key_ownership.owned_organizations, which counts only the active OrcaHub
    organizations a key's end would freeze.
    """
    if not api_key_id:
        return 0
    counted = await db.execute(
        select(func.count()).select_from(Organization).where(owned_by_clause([api_key_id]))
    )
    return int(counted.scalar_one())


async def claims_organization(db: AsyncSession, api_key_id: str, external_id: str) -> bool:
    """Whether this key claimed the OrcaHub organization with this external id."""
    if not api_key_id:
        return False
    row = (await db.execute(
        select(Organization.id).where(
            owned_by_clause([api_key_id]),
            Organization.source == "orcahub",
            Organization.external_id == external_id,
        ).limit(1)
    )).first()
    return row is not None


async def claimed_destinations(
    db: AsyncSession, members: Sequence[PushedMember], *, email: str, api_key_id: str
) -> List[int]:
    """Active organizations this key claimed that this push names `email` a partner of.

    The membership row is read back too, so an upsert that failed, a later remove in the same push
    or a row holding another role leaves the organization out.
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
            owned_by_clause([api_key_id]),
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
        # important: each move is flushed before the one commit at the end. When the share audit
        # refuses a later cluster, the earlier moves stay in the session and the request's next
        # commit would write them without a record. Nothing of a failed rebind may survive.
        await db.rollback()
        raise
    # Recorded after reconcile_personal_clusters committed, so a rolled-back move leaves no record.
    for cluster_id, from_org, to_org in moved:
        _record(cluster_id, from_org, to_org, email, api_key_id)
    return rebound
