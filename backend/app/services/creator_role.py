"""Whether an account is an Admin now, for a decision made without that account's own request.

An API key and a snapshot schedule act for the person who created them, but neither carries a
session, so the role their rule depends on is read from Authentik (authentik_role_reading). The
answer is three-valued on purpose: True and False are answers, and None means the identity
provider did not answer, which a caller turns into a retryable refusal, never into "not an Admin"
(that would turn an IdP outage into a permanent 403) and never into "an Admin".

note: an account Authentik no longer carries, has disabled, or carries twice is a firm "not an
Admin" (authentik_role_reading.ENDED_REASONS), and so is a deployment with no identity provider
configured. Answers are cached briefly per account; unknowns are not cached.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

from app.core.rbac import Role
from app.services.authentik_role_reading import ENDED_REASONS, CurrentRoleReader

READING_SECONDS = 60
_FIRM_NO = ENDED_REASONS | {"authentik_not_configured", "email_missing"}

_lock = threading.Lock()
_readings: Dict[str, Tuple[float, bool]] = {}


async def creator_is_admin(email: Optional[str], *, roles: Optional[CurrentRoleReader] = None) -> Optional[bool]:
    """True for an Admin, False for anyone else, None when the identity provider did not answer."""
    wanted = (email or "").strip().lower()
    if not wanted:
        return False
    now = time.monotonic()
    with _lock:
        cached = _readings.get(wanted)
    if cached is not None and now - cached[0] < READING_SECONDS:
        return cached[1]

    if roles is None:
        from app.services.authentik_sync import get_authentik_sync

        roles = get_authentik_sync()
    reading = await roles.read_current_roles(wanted)
    if reading.roles is None:
        if reading.reason not in _FIRM_NO:
            return None
        answer = False
    else:
        answer = Role.ADMIN in reading.roles
    with _lock:
        _readings[wanted] = (now, answer)
    return answer


def forget_creator_roles() -> None:
    """Tests only: drop every cached reading."""
    with _lock:
        _readings.clear()
