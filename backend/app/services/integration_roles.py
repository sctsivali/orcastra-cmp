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
  * role_tenant is added only to a user holding no role group, which grants nothing a user
    without groups does not already have. It is the only group an orphan revoke strips.

The decisions are pure functions over the user's current groups and the owners of their
organizations. Authentik reads and writes happen in the apply helpers.
"""

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationMember

logger = logging.getLogger(__name__)

ROLE_ADMIN, ROLE_PARTNER, ROLE_TENANT = "role_admin", "role_partner", "role_tenant"

# note: reasons that are an ordinary no-op on every scheduled sync, logged below WARNING.
_ROUTINE = frozenset({"already_partner", "already_tenant", "still_a_member", "nothing_held"})


@dataclass(frozen=True)
class RoleChange:
    add: Optional[str]
    remove: frozenset
    reason: str

    @property
    def is_noop(self) -> bool:
        return self.add is None and not self.remove


def _keep(reason: str) -> RoleChange:
    return RoleChange(None, frozenset(), reason)


def decide_upsert(
    target_role: str,
    current: Optional[set],
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
        return RoleChange(ROLE_PARTNER, frozenset({ROLE_TENANT}) & frozenset(current), "grant_partner")
    if ROLE_PARTNER in current:
        return _keep("no_demotion")
    if ROLE_TENANT in current:
        return _keep("already_tenant")
    # note: role_tenant grants nothing a user without groups does not already have
    return RoleChange(ROLE_TENANT, frozenset(), "grant_tenant")


def decide_orphan_revoke(current: Optional[set], remaining: int) -> RoleChange:
    """The role an integration may strip once a user's last membership is gone."""
    if remaining > 0:
        return _keep("still_a_member")
    if current is None:
        return _keep("authentik_unknown")
    if current & {ROLE_ADMIN, ROLE_PARTNER}:
        return _keep("no_demotion")
    if ROLE_TENANT in current:
        return RoleChange(None, frozenset({ROLE_TENANT}), "revoke_tenant")
    return _keep("nothing_held")


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
    db: AsyncSession, sync_svc: Any, email: str, target_role: str, api_key_id: str
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


async def revoke_if_orphaned(db: AsyncSession, sync_svc: Any, email: str) -> RoleChange:
    """Strip role_tenant once the user belongs to no organization. Never demotes further."""
    remaining = len((await db.execute(
        select(OrganizationMember.id).where(func.lower(OrganizationMember.user_email) == email.lower())
    )).scalars().all())
    if remaining > 0:
        return decide_orphan_revoke(None, remaining)
    change = decide_orphan_revoke(await sync_svc.get_user_role_groups(email), remaining)
    if change.is_noop:
        _log(None, email, change)
        return change
    await sync_svc.change_integration_role_groups(email, remove=change.remove)
    return change
