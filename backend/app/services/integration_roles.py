"""What an integration key may do to a pushed member's global Authentik role.

A member sync used to call sync_user_role, which removes the user from every other role group,
role_admin included, and a membership removal stripped every role group once the user had no
membership left. Any Partner could mint a key, sync a fresh organization and push any email into
it, so any Partner could demote an Admin or promote a tenant of another organization.

The rule here: an integration may grant, never demote.
  * role_admin is never added or removed, and a user holding it is never touched.
  * role_partner is never removed. It is granted only when every organization the user belongs
    to or created is owned by the calling key, because a global partner is unfiltered on each of
    them.
  * role_tenant is the baseline for an active account. An integration may add it to a user
    holding no role group, but neither a Partner promotion nor organization offboarding removes it.

The decisions are pure functions over the user's current groups and the owners of their
organizations. Authentik reads and writes happen in the apply helpers.

note: known limit. role_partner should be granted only through the key's home organization, and
nothing records a key's home yet. Until it does, a buyer founder whose organizations all sit under
the seller's key is still promoted.
"""

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Protocol, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationMember

logger = logging.getLogger(__name__)

ROLE_ADMIN, ROLE_PARTNER, ROLE_TENANT = "role_admin", "role_partner", "role_tenant"

# note: reasons that are an ordinary no-op on every scheduled sync, logged below WARNING.
_ROUTINE = frozenset({"already_partner", "already_tenant"})

# note: OrcaHub's organization role, pushed per member, to the member role the member sync stores.
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


def decide_upsert(
    target_role: str,
    current: Optional[set[str]],
    owners: Sequence[Optional[str]],
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
        # promote someone whose organizations all sit under this key.
        if not owners or any(owner != api_key_id for owner in owners):
            return _keep("membership_outside_this_key")
        return RoleChange(ROLE_PARTNER, frozenset(), "grant_partner")
    if ROLE_PARTNER in current:
        return _keep("no_demotion")
    if ROLE_TENANT in current:
        return _keep("already_tenant")
    # note: baseline group grants application entry, while membership still scopes resources.
    return RoleChange(ROLE_TENANT, frozenset(), "grant_tenant")


def may_reconcile(org_slug: str, org_owner: Optional[str], api_key_id: str) -> bool:
    """Whether a key's offboarding reconcile may prune this organization's roster.

    important: same ownership rule as the member upsert, and stricter, because a prune deletes
    rows. An organization with no owning key is refused too. Without this a key could name another
    integration's organization in its push and purge that roster.
    """
    if org_owner == api_key_id:
        return True
    logger.warning(
        "Reconcile refused for organization %s: owned by api key %s, caller is %s",
        org_slug, org_owner, api_key_id,
    )
    return False


async def membership_owners(db: AsyncSession, email: str) -> List[Optional[str]]:
    """The owning key of every organization the email belongs to or created.

    Dashboard organizations have no api_key_id, so they come back as None and count as foreign.
    note: created organizations are included because the Partner scope includes them too.
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
    return [owner for _org_id, owner in rows]


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
