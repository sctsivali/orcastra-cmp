"""Two-key enrollment: an owner key vouches for another key it also holds, which then joins the owner set.

OrcaHub holds every connection's key and secret. When a provider connects a second key to the same
Orcastra, OrcaHub proves possession of both (the vouching owner key in the headers, the new key's id and
secret in the body), and the new key joins the organization's owner set. Holding an owner key already
grants everything an owner can do, so vouching gives its holder no new power over the organization;
what this guards is the new key, whose secret the endpoint checks, and which must belong to the same
side (party_check).

The secret check makes this endpoint an oracle for another key's secret. So the calling key must own
the organization before anything about the new key is read (a non-owner, a missing organization and an
unclaimed one get one answer), a missing key and a wrong secret then answer one sentence, and the route
is limited per vouching key, per organization (spent only once the caller is an owner) and per
(vouching key, new key) pair, which is locked out after repeated failures. A store that cannot answer is
503 and never counts as a failure. Every limit falls back to an in-process counter when the shared store
cannot answer, rather than open: a secret oracle must not become unlimited during a Redis outage.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationMember

# Per vouching key and per organization.
PER_MINUTE = 5
PER_DAY = 30
# Per (vouching key, new key): failed secret checks before the pair is locked out of enrollment.
FAILURES_BEFORE_LOCKOUT = 5
LOCKOUT_SECONDS = 3600
# How long a partner row must have existed before a key its partner minted may be enrolled (party_check).
PARTNER_MIN_AGE = timedelta(hours=24)

UNVERIFIED = "The new key could not be verified with the secret given, so it was not added."
NOT_AN_OWNER = "The calling key does not own this organization, so it cannot add an owner key."
UNREADABLE = "The keys could not be checked right now, so nothing was changed. Try again."

_guard = threading.Lock()
_local: Dict[str, Tuple[int, float]] = {}


def _consume(bucket: str, window: int) -> int:
    """Spend one hit in a window and return the count, shared across workers when the store answers."""
    from app.middleware.security import _shared_consume

    spent = _shared_consume(bucket, 10**9, window)
    if spent is not None:
        return spent[0]
    now = time.monotonic()
    with _guard:
        count, started = _local.get(bucket, (0, now))
        if now - started >= window:
            count, started = 0, now
        count += 1
        _local[bucket] = (count, started)
        return count


def _peek(bucket: str, window: int) -> int:
    try:
        from app.core.cache import get_cache_manager

        client = get_cache_manager().redis_client
        if client is not None:
            value = client.get(f"orcastra:ratelimit:{bucket}")
            return int(value or 0)
    except Exception:  # noqa: BLE001 - fall back to the in-process count
        pass
    now = time.monotonic()
    with _guard:
        count, started = _local.get(bucket, (0, now))
        return 0 if now - started >= window else count


def within_key_limits(vouching_key_id: str) -> bool:
    """Spend one enrollment attempt for this vouching key; False once over a limit."""
    return all([
        _consume(f"owner-enroll:key:{vouching_key_id}:m", 60) <= PER_MINUTE,
        _consume(f"owner-enroll:key:{vouching_key_id}:d", 86400) <= PER_DAY,
    ])


def within_org_limits(organization_external_id: str) -> bool:
    """Spend one enrollment attempt for this organization; False once over a limit.

    important: only once the caller is known to own the organization, so a key that does not cannot
    spend its budget.
    """
    return all([
        _consume(f"owner-enroll:org:{organization_external_id}:m", 60) <= PER_MINUTE,
        _consume(f"owner-enroll:org:{organization_external_id}:d", 86400) <= PER_DAY,
    ])


def _failure_bucket(vouching_key_id: str, new_key_id: str) -> str:
    return f"owner-enroll:fail:{vouching_key_id}:{new_key_id}"


def locked_out(vouching_key_id: str, new_key_id: str) -> bool:
    return _peek(_failure_bucket(vouching_key_id, new_key_id), LOCKOUT_SECONDS) >= FAILURES_BEFORE_LOCKOUT


def record_failure(vouching_key_id: str, new_key_id: str) -> None:
    """One failed secret check, counted per pair: one owner key's guesses never lock another's out."""
    _consume(_failure_bucket(vouching_key_id, new_key_id), LOCKOUT_SECONDS)


def reset_limits() -> None:
    """For tests: forget the in-process counters."""
    with _guard:
        _local.clear()


async def party_check(
    db: AsyncSession, org: Organization, vouching: Mapping[str, Any], new: Mapping[str, Any],
    *, vouching_key_id: Optional[str] = None, now: Optional[datetime] = None,
) -> bool:
    """Whether the new key belongs to the organization's side (decision D1).

    Its minter must be a partner of the organization; or the vouching key's minter, or the organization's
    creator, while a partner of the vouching key's home (the organization a sale through that key proves,
    cluster_parties.home_partner_keys). Either partner row must be at least PARTNER_MIN_AGE old. Otherwise
    a holder of one owner key could enroll a key minted by anyone, which would also survive the rotation of
    the key that vouched for it.

    important: never the minter relation alone. A minter who left keeps the secrets it held, so "minted
    the vouching key" or "created this organization" would let it enroll a key of its own that survives
    the rotation of the key it left with, and that becomes the primary when that key is revoked.

    important: the age, because an owner key writes the organization's members. Without it the holder of
    a leaked owner key could push its own email as a partner and enroll a key it minted in the same
    minute. A partner row records no key that wrote it (invited_by says only "orcahub-sync"), so this
    cannot count only the rows another owner wrote. Known limit: a role change keeps the row's
    joined_at, so promoting a member row older than a day to partner passes at once. That needs an
    existing member who can mint integration keys, and the leaked-key runbook checks the owner set after
    every rotation.
    """
    minter = str(new.get("created_by") or "").strip().lower()
    if not minter:
        return False
    from app.services.cluster_parties import RUNNING_ROLES, home_partner_keys

    settled = (now or datetime.now(timezone.utc)) - PARTNER_MIN_AGE
    row = await db.execute(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org.id,
            func.lower(OrganizationMember.user_email) == minter,
            OrganizationMember.role.in_(RUNNING_ROLES),
            OrganizationMember.joined_at <= settled,
        )
    )
    if row.first() is not None:
        return True
    if minter not in {str(vouching.get("created_by") or "").strip().lower(), str(org.created_by or "").strip().lower()}:
        return False
    key_id = vouching_key_id or str(vouching.get("id") or "")
    # note: the minter as the key records it. home_partner_keys matches a member's email exactly, as the
    # rotation rule passes it; the lowercased form above would miss a row stored with capitals.
    stored = str(new.get("created_by") or "").strip()
    return bool(key_id) and key_id in await home_partner_keys(db, stored, settled_before=settled)


async def partner_emails(db: AsyncSession, org: Organization) -> List[str]:
    """Who is told a key joined the owner set: the organization's current partners."""
    from app.services.cluster_parties import RUNNING_ROLES

    rows = await db.execute(
        select(OrganizationMember.user_email).where(
            OrganizationMember.organization_id == org.id, OrganizationMember.role.in_(RUNNING_ROLES)
        )
    )
    return sorted({email for email in rows.scalars().all() if email})


def owner_set_answer(org: Organization, owners: List[str], **extra: Any) -> Dict[str, Any]:
    return {"organization_external_id": org.external_id, "primary_key_id": org.api_key_id,
            "owner_key_ids": owners, **extra}


def is_external_org(org: Optional[Organization]) -> bool:
    return org is not None and org.source == "orcahub" and bool(org.is_active)
