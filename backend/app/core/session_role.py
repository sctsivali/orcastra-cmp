"""Whether a signed-in account holds a membership, which makes it a Tenant without a role group.

A partner deployment signs OrcaHub buyers in through its own Authentik, federated to OrcaHub with a
profile mapping that deliberately carries no groups, so a buyer never holds a role group there. The
buyer's access is the membership and policy OrcaHub provisioned for their email. An account that an
active organization lists as a member is therefore a session with a role: Tenant, and never more,
since resolve_roles already reads a groupless caller as Tenant.

important: the email is compared exactly, as org_scope compares it, so an account admitted here is
the account whose tenant scope the routes then read. The answer is cached in this process only,
briefly, and never in Redis: an unsigned shared entry would let anyone who can write to Redis admit
an account (see rbac.resolve_roles).
"""

import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select

logger = logging.getLogger(__name__)

# A removed membership stops counting within this long. The tenant scope itself is read on every
# request, so in that window the account reaches nothing it no longer holds.
MEMBERSHIP_CACHE_SECONDS = 15.0
# A "no" is remembered only long enough to absorb one page load's burst: a buyer who opened the
# dashboard just before OrcaHub's provisioning landed must count within seconds of the membership,
# not after the whole window above. Concurrent callers already share one read (_in_flight).
NO_MEMBERSHIP_CACHE_SECONDS = 2.0
# Bounds the process-local cache against a flood of distinct signed-in accounts.
MAX_CACHED_ACCOUNTS = 4096

_answers: Dict[str, Tuple[float, bool]] = {}
# One read in flight per account and process: a page load or a burst on a cold cache shares it.
_in_flight: Dict[str, "asyncio.Future[Optional[bool]]"] = {}
_now = time.monotonic


def _session() -> AbstractAsyncContextManager[Any]:
    from app.core.database import async_session_maker

    return async_session_maker()


async def _read(email: str, db: Any) -> bool:
    from app.models.db_models import Organization, OrganizationMember

    stmt = (
        select(OrganizationMember.id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            OrganizationMember.user_email == email,
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None


def _remember(email: str, held: bool, now: float) -> None:
    known = _answers.get(email)
    if known is not None and known[0] > now:
        # A read that started before the one already stored finished after it: keep the newer answer.
        return
    if email not in _answers and len(_answers) >= MAX_CACHED_ACCOUNTS:
        for stale in [key for key, (at, yes) in _answers.items()
                      if now - at >= (MEMBERSHIP_CACHE_SECONDS if yes else NO_MEMBERSHIP_CACHE_SECONDS)]:
            del _answers[stale]
        while len(_answers) >= MAX_CACHED_ACCOUNTS:
            del _answers[next(iter(_answers))]
    _answers[email] = (now, held)


async def holds_membership(
    email: Optional[str], db: Any = None, *, strict: bool = False, fresh: bool = False
) -> bool:
    """Whether an active organization lists this email as a member.

    Opens its own session when `db` is not given. A failed read answers False and is not cached:
    the account is refused as before, never admitted on an error. With `strict` the failure is
    raised instead, for a caller that counts failures before acting on them (the minute re-check
    of an open shell, which must not end every buyer's session on one database blip). With `fresh`
    the cache is skipped and the answer read now replaces it, for the access verdict that Check
    again asks for.
    """
    if not email or not email.strip():
        return False
    now = _now()
    cached = None if fresh else _answers.get(email)
    if cached is not None:
        window = MEMBERSHIP_CACHE_SECONDS if cached[1] else NO_MEMBERSHIP_CACHE_SECONDS
        if now - cached[0] < window:
            return cached[1]
    pending = None if (strict or fresh) else _in_flight.get(email)
    if pending is not None:
        # A failed shared read answers None here: refused, as for the caller that made it.
        return bool(await asyncio.shield(pending))
    shared: "Optional[asyncio.Future[Optional[bool]]]" = None
    if not (strict or fresh):
        shared = asyncio.get_running_loop().create_future()
        _in_flight[email] = shared
    try:
        if db is not None:
            held = await _read(email, db)
        else:
            async with _session() as own:
                held = await _read(email, own)
    except Exception as exc:  # noqa: BLE001 - any failure refuses; the session routes answer no_role_assigned
        # The exception type only: a driver error carries the bound email in its text.
        logger.warning("Could not read whether a groupless account holds a membership: %s", type(exc).__name__)
        if shared is not None:
            shared.set_result(None)
        if strict:
            raise
        return False
    else:
        _remember(email, held, now)
        if shared is not None:
            shared.set_result(held)
    finally:
        if shared is not None:
            if _in_flight.get(email) is shared:
                del _in_flight[email]
            if not shared.done():
                # Cancelled mid-read: waiters are refused rather than left hanging.
                shared.set_result(None)
    if held:
        logger.debug("Groupless account admitted as Tenant by organization membership: user=%s", email)
    return held


def forget_memberships() -> None:
    """Drop the cached answers. For tests."""
    _answers.clear()
    _in_flight.clear()
