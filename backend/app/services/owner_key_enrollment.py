"""Two-key enrollment: an owner key vouches for another key it also holds, which then joins the owner set.

OrcaHub holds every connection's key and secret. When a provider connects a second key to the same
Orcastra, OrcaHub proves possession of both (the vouching owner key in the headers, the new key's id and
secret in the body), and the new key joins the organization's owner set. Holding an owner key already
grants everything an owner can do, so vouching gives its holder no new power over the organization;
what this guards is the new key, whose secret the endpoint checks, and which must belong to the same
side (party_check).

The secret check makes this endpoint an oracle for another key's secret, so it answers a missing key and
a wrong secret with one sentence, and is limited per vouching key and per organization, with a lockout
per new key after repeated failures. Every limit falls back to an in-process counter when the shared
store cannot answer, rather than open: a secret oracle must not become unlimited during a Redis outage.
"""

import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationMember

# Per vouching key and per organization.
PER_MINUTE = 5
PER_DAY = 30
# Per new key: failed secret checks before it is locked out of enrollment.
FAILURES_BEFORE_LOCKOUT = 5
LOCKOUT_SECONDS = 3600

UNVERIFIED = "The new key could not be verified with the secret given, so it was not added."

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


def within_limits(vouching_key_id: str, organization_external_id: str) -> bool:
    """Spend one enrollment attempt for this vouching key and organization; False once over a limit."""
    counts = [
        _consume(f"owner-enroll:key:{vouching_key_id}:m", 60) <= PER_MINUTE,
        _consume(f"owner-enroll:key:{vouching_key_id}:d", 86400) <= PER_DAY,
        _consume(f"owner-enroll:org:{organization_external_id}:m", 60) <= PER_MINUTE,
        _consume(f"owner-enroll:org:{organization_external_id}:d", 86400) <= PER_DAY,
    ]
    return all(counts)


def locked_out(new_key_id: str) -> bool:
    return _peek(f"owner-enroll:fail:{new_key_id}", LOCKOUT_SECONDS) >= FAILURES_BEFORE_LOCKOUT


def record_failure(new_key_id: str) -> None:
    _consume(f"owner-enroll:fail:{new_key_id}", LOCKOUT_SECONDS)


def reset_limits() -> None:
    """For tests: forget the in-process counters."""
    with _guard:
        _local.clear()


async def party_check(
    db: AsyncSession, org: Organization, vouching: Mapping[str, Any], new: Mapping[str, Any]
) -> bool:
    """Whether the new key belongs to the organization's side (decision D1).

    Its minter must be the vouching key's minter, the organization's creator, or a current partner of
    the organization. Otherwise a holder of one owner key could enroll a key minted by anyone, which
    would also survive the rotation of the key that vouched for it.
    """
    minter = str(new.get("created_by") or "").strip().lower()
    if not minter:
        return False
    if minter == str(vouching.get("created_by") or "").strip().lower():
        return True
    if minter == str(org.created_by or "").strip().lower():
        return True
    from app.services.cluster_parties import RUNNING_ROLES

    row = await db.execute(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org.id,
            func.lower(OrganizationMember.user_email) == minter,
            OrganizationMember.role.in_(RUNNING_ROLES),
        )
    )
    return row.first() is not None


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
