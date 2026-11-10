"""What an integration key may do to a pushed member's global Authentik role: grant, never demote.

Any Partner can mint a key, sync a fresh organization and push any email into it, so a member sync
that rewrote role groups (sync_user_role removes every other role group, role_admin included) could
demote an Admin or promote another organization's tenant.
  * role_admin is never added or removed, and a user holding it is never touched.
  * role_partner is never removed. It is granted only when the calling key owns every organization
    the user belongs to or created, because a global partner is unfiltered on each of them.
  * role_tenant (the active-account baseline) may go to a user with no role group; never removed.

Known limit: the role_partner grant does not check cluster_parties.home_partner_keys, so a buyer
founder whose organizations all sit under the seller's key is still promoted.
"""

import logging
from dataclasses import dataclass
from typing import AbstractSet, FrozenSet, Iterable, List, Optional, Protocol, Sequence, Union

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationMember
from app.services.organization_owners import owners_of

logger = logging.getLogger(__name__)

ROLE_ADMIN, ROLE_PARTNER, ROLE_TENANT = "role_admin", "role_partner", "role_tenant"

# Reasons that are an ordinary no-op on every scheduled sync, logged below WARNING.
_ROUTINE = frozenset({"already_partner", "already_tenant"})

# OrcaHub's organization role, pushed per member, to the member role the member sync stores.
_ORCAHUB_ROLE_MAP = {
    "PARTNER": "partner",
    "TENANT": "tenant",
    "USER": "tenant",  # legacy alias
}


def stored_member_role(pushed_role: str) -> str:
    """The OrganizationMember.role the member sync stores for a pushed role. Unknown is tenant."""
    return _ORCAHUB_ROLE_MAP.get(pushed_role.upper(), "tenant")


class RoleGroupStore(Protocol):
    """The two Authentik calls an integration's role change is allowed to make."""

    async def get_user_role_groups(self, user_email: str) -> Optional[set[str]]: ...

    async def change_integration_role_groups(
        self,
        user_email: str,
        add: Optional[str] = None,
        remove: Iterable[str] = frozenset(),
    ) -> bool: ...


@dataclass(frozen=True)
class RoleChange:
    add: Optional[str]
    remove: frozenset[str]
    reason: str

    @property
    def is_noop(self) -> bool:
        return self.add is None and not self.remove


def _keep(reason: str) -> RoleChange:
    return RoleChange(None, frozenset(), reason)


def _holds(owner: Union[None, str, AbstractSet[str]], api_key_id: str) -> bool:
    """Whether one organization's owner (a key, an owner set, or None for none) includes this key."""
    if isinstance(owner, AbstractSet):
        return api_key_id in owner
    return owner == api_key_id


def decide_upsert(
    target_role: str,
    current: Optional[set[str]],
    owners: Sequence[Union[None, str, AbstractSet[str]]],
    api_key_id: str,
) -> RoleChange:
    """What an integration may do to a pushed member's Authentik role. It may grant, never demote."""
    if current is None:
        return _keep("authentik_unknown")
    if ROLE_ADMIN in current:
        return _keep("holds_admin")
    if target_role == "partner":
        if ROLE_PARTNER in current:
            return _keep("already_partner")
        # important: a global partner is unfiltered on every org it belongs to, so a key may only
        # promote someone whose organizations all sit under this key (in each one's owner set).
        if not owners or any(not _holds(owner, api_key_id) for owner in owners):
            return _keep("membership_outside_this_key")
        return RoleChange(ROLE_PARTNER, frozenset(), "grant_partner")
    if ROLE_PARTNER in current:
        return _keep("no_demotion")
    if ROLE_TENANT in current:
        return _keep("already_tenant")
    # The baseline group grants application entry; membership still scopes resources.
    return RoleChange(ROLE_TENANT, frozenset(), "grant_tenant")


def may_reconcile(org_slug: str, org_owner: Union[None, str, AbstractSet[str]], api_key_id: str) -> bool:
    """Whether a key's offboarding reconcile may prune this organization's roster.

    important: same ownership rule as the member upsert, and stricter because a prune deletes
    rows: an organization with no owning key is refused too. Otherwise a key could name another
    integration's organization in its push and purge that roster.
    """
    if _holds(org_owner, api_key_id):
        return True
    logger.warning(
        "Reconcile refused for organization %s: owned by api key %s, caller is %s",
        org_slug, sorted(org_owner) if isinstance(org_owner, AbstractSet) else org_owner, api_key_id,
    )
    return False


async def membership_owners(db: AsyncSession, email: str) -> List[FrozenSet[str]]:
    """The owner set of every organization the email belongs to or created.

    Dashboard organizations have no owner, so they come back as an empty set and count as foreign.
    Created organizations are included because the Partner scope includes them too.
    """
    lowered = email.lower()
    rows = (await db.execute(
        select(Organization.id, Organization.api_key_id)
        .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(or_(
            func.lower(OrganizationMember.user_email) == lowered,
            func.lower(Organization.created_by) == lowered,
        ))
        .distinct()
    )).all()
    owners = await owners_of(db, [org_id for org_id, _primary in rows])
    return [frozenset(row.api_key_id for row in owners.get(org_id, [])) for org_id, _primary in rows]


def _log(api_key_id: Optional[str], email: str, change: RoleChange) -> None:
    level = logging.DEBUG if change.reason in _ROUTINE else logging.WARNING
    logger.log(
        level,
        "Integration %s left the Authentik role of %s unchanged (%s)",
        api_key_id or "<reconcile>", email, change.reason,
    )


async def apply_upsert_role(
    db: AsyncSession, sync_svc: RoleGroupStore, email: str, target_role: str, api_key_id: str
) -> RoleChange:
    """Decide and apply the role change a member upsert may make."""
    current = await sync_svc.get_user_role_groups(email)
    owners = await membership_owners(db, email) if target_role == "partner" else []
    change = decide_upsert(target_role, current, owners, api_key_id)
    if change.is_noop:
        _log(api_key_id, email, change)
        return change
    await sync_svc.change_integration_role_groups(email, add=change.add, remove=change.remove)
    logger.info("Integration %s changed the Authentik role of %s (%s)", api_key_id, email, change.reason)
    return change
