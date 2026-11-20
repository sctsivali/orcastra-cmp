"""The identity directory behind get_authentik_sync() in Mini: the local identity store.

It answers the same questions as the Authentik directory (the current role of an email, whether
the account is live, sync status) and refuses the writes Mini does not delegate. A global role
changes only on the Operators page: never through Settings > User Groups (not offered in Mini) and
never through an integration key, which could otherwise raise an operator's privileges.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from app.core.auth import AuthenticatedUser
from app.editions.mini import identities, tokens
from app.editions.mini.verifier import session_refusal
from app.services.authentik_role_reading import RoleReading, known_roles, unknown_role
from app.services.authentik_sync import INTEGRATION_ROLE_GROUPS

logger = logging.getLogger(__name__)


class LocalDirectory:
    enabled = True

    async def read_current_roles(self, user_email: str) -> RoleReading:
        try:
            snapshot = await identities.snapshot_by_email(user_email)
        except Exception as exc:  # noqa: BLE001 - "could not say", which never ends a session alone
            logger.warning("Identity store unreadable for a role read: %s", type(exc).__name__)
            return unknown_role("directory_unavailable")
        if snapshot is None:
            return unknown_role("user_not_found")
        if not snapshot.active:
            return unknown_role("user_inactive")
        return known_roles([f"role_{snapshot.role}"])

    async def get_user_role_groups(self, user_email: str) -> Optional[set[str]]:
        reading = await self.read_current_roles(user_email)
        if reading.roles is None:
            return None
        return {f"role_{role.value}" for role in reading.roles}

    async def session_ended(self, user: AuthenticatedUser) -> Optional[str]:
        """Ends an open socket whose certificate, role or identity changed since it opened."""
        claims = user.raw_claims or {}
        if claims.get("iss") != tokens.ISSUER or not isinstance(claims.get("cnf"), dict):
            return "session_unbound"
        try:
            snapshot = await identities.snapshot_by_uuid(claims.get("sub", ""))
        except Exception:  # noqa: BLE001 - an outage does not cut every shell; the next minute retries
            return None
        return session_refusal(snapshot, claims, None)

    async def sync_user_role(self, user_email: str, target_role: str) -> bool:
        logger.warning("Role change through User Groups refused: Mini changes roles on the Operators page")
        return False

    async def remove_user_from_all_roles(self, user_email: str) -> bool:
        logger.warning("Role removal through User Groups refused: Mini revokes on the Operators page")
        return False

    async def change_integration_role_groups(
        self, user_email: str, add: Optional[str] = None, remove: Iterable[str] = frozenset(),
    ) -> bool:
        removing = frozenset(remove)
        if removing:
            raise ValueError("an integration may not remove account role groups")
        illegal = (removing | ({add} if add else frozenset())) - INTEGRATION_ROLE_GROUPS
        if illegal:
            raise ValueError(f"an integration may not change the role groups {sorted(illegal)}")
        if add:
            logger.warning("An integration key asked to grant %s; Mini never lets a key change an operator's role", add)
        return not add

    async def get_sync_status(self) -> dict:
        return {"enabled": False, "connected": False, "groups": {}, "provider": "local"}
