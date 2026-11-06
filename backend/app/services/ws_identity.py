"""Whether the caller behind a long-lived interactive socket still has an account.

important: a terminal or console stays open long after the token that opened it expired. Every
minute its socket asks Authentik, through the role reader the dashboard already trusts, whether
the account still exists and is enabled; an account that is gone or disabled ends the session.
Authentik not answering does not, because an identity provider outage must not cut every open
shell at once. Readings are kept 55 seconds per account, so one check per minute per account
reaches Authentik however many sessions it holds.

note: what the caller may reach (organization, grants, ownership) is read again from the
database by the session guard. This answers whether the account is still live, and with which
roles, so the guard can end a session whose caller was demoted: the ticket's groups are frozen at
mint time, and checking scope with them would keep a demoted Admin an Admin for as long as the
shell stays open.
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
        # important: no email, no way to ask Authentik about this account, ever; a session that
        # can never be re-checked ends instead of running unchecked (the mint refuses these too).
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
        # note: an answer, not a failure: the account is live but every role group was removed.
        return IdentityCheck(True, "no_role_assigned")
    return IdentityCheck(False, "active", reading.roles)


def forget_identities() -> None:
    """Drop the cached readings. For tests."""
    _readings.clear()
