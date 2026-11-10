"""Whether the caller behind a long-lived interactive socket still has an account.

important: a terminal or console outlives the token that opened it. Every minute its socket asks
Authentik, through the trusted role reader, whether the account still exists and is enabled; a
gone or disabled account ends the session. Authentik not answering does not, so an IdP outage
cannot cut every open shell at once. Readings are cached 55 s per account, so one check per
minute per account reaches Authentik however many sessions it holds.

The session guard re-reads scope (organization, grants, ownership) from the database; this module
supplies the current roles so a demoted caller's session ends. The ticket's groups are frozen at
mint time and would keep a demoted Admin an Admin.
"""

import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.services.authentik_role_reading import ENDED_REASONS, RoleReading  # noqa: F401 - re-exported

IDENTITY_CACHE_SECONDS = 55
# ENDED_REASONS: a second account carrying the email must not keep the first one's sessions alive
# after it is disabled, so those readings end a session rather than retrying it.

_readings: Dict[str, Tuple[float, RoleReading]] = {}


@dataclass(frozen=True)
class IdentityCheck:
    ended: bool
    reason: str
    # The roles Authentik holds now; None when it could not say.
    roles: Optional[FrozenSet[Role]] = None


async def _read(email: str) -> RoleReading:
    from app.services.authentik_sync import get_authentik_sync

    return await get_authentik_sync().read_current_roles(email)


async def check_identity(user: AuthenticatedUser) -> IdentityCheck:
    email = (user.email or "").strip().lower()
    if not email:
        # important: without an email the account can never be re-checked, so the session ends
        # rather than running unchecked (the mint refuses these too).
        return IdentityCheck(True, "email_missing")
    now = time.monotonic()
    cached = _readings.get(email)
    if cached is not None and now - cached[0] < IDENTITY_CACHE_SECONDS:
        reading = cached[1]
    else:
        reading = await _read(email)
        _readings[email] = (now, reading)
    if reading.roles is None:
        return IdentityCheck(reading.reason in ENDED_REASONS, reading.reason)
    if not reading.has_role:
        # The account is live but every role group was removed.
        return IdentityCheck(True, "no_role_assigned")
    return IdentityCheck(False, "active", reading.roles)


def forget_identities() -> None:
    """Drop the cached readings. For tests."""
    _readings.clear()
