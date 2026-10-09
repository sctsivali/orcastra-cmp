"""The set of integration keys that own a synced organization.

A synced (OrcaHub) organization is owned by a set of integration keys. Every key in the set may sync
the organization, write its members and tenant access policies and provision tenant access for it;
`organizations.api_key_id` is the primary, the one key OrcaHub callbacks are made with, and is always
in the set (the before_flush listener in db_models writes its row, and on PostgreSQL a deferred
foreign key refuses a primary outside the set). An organization with a NULL primary has no owner and
is claimable as before.

A key enters a set only by claiming an unowned organization (a sync or a refresh), by provisioning
creating one, or by an Administrator's transfer, which replaces the whole set at once.

Only this module (and the listener that keeps the primary in the set) writes `organization_owner_keys`
or `organizations.api_key_id` outside the three claim paths; the ratchets in
tests/test_an_organization_has_a_set_of_owner_keys.py pin that.

Concurrency: a change to an organization's set runs inside serialize_org_owners for it, taken after
any key locks (integration_key_ownership.serialize_key_ownership), in sorted order, never the other way
around.
"""

import asyncio
import hashlib
import logging
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationOwnerKey

logger = logging.getLogger(__name__)

# important: the ways in which a key comes to own an organization that also put the organization on its
# minter's side (services/cluster_parties.py), and so let that side manage the key. A key enrolled by
# another owner or added by an Administrator owns the organization but lends no party: otherwise the
# seller who provisioned a buyer would manage, and could rotate, a key the buyer's founder enrolled.
# An allowlist, so a new way in lends nothing until it is decided here.
PARTY_ADDED_VIA = ("backfill", "claim", "provision", "transfer", "carry")


@dataclass(frozen=True)
class OwnerRow:
    api_key_id: str
    added_at: Optional[datetime]
    added_via: str
    is_primary: bool


def owned_by_clause(key_ids: Iterable[str]) -> Any:
    """A WHERE term on Organization: one of these keys is in its owner set."""
    keys = sorted({key for key in key_ids if key})
    return exists().where(
        OrganizationOwnerKey.organization_id == Organization.id,
        OrganizationOwnerKey.api_key_id.in_(keys),
    )


def lends_party(column: Any = OrganizationOwnerKey.added_via) -> Any:
    """A WHERE term on an owner row (or an alias's added_via): it puts the organization on its key's side."""
    return column.in_(PARTY_ADDED_VIA)


def _order(row: OwnerRow) -> Tuple[datetime, str]:
    return (row.added_at or datetime.min, row.api_key_id)


async def owners_of(db: AsyncSession, org_ids: Iterable[int]) -> Dict[int, List[OwnerRow]]:
    """Each organization's owner set, oldest first (added_at, then key id). One query."""
    wanted = sorted({org_id for org_id in org_ids if org_id is not None})
    if not wanted:
        return {}
    rows = await db.execute(
        select(
            OrganizationOwnerKey.organization_id,
            OrganizationOwnerKey.api_key_id,
            OrganizationOwnerKey.added_at,
            OrganizationOwnerKey.added_via,
            Organization.api_key_id,
        )
        .join(Organization, Organization.id == OrganizationOwnerKey.organization_id)
        .where(OrganizationOwnerKey.organization_id.in_(wanted))
    )
    owners: Dict[int, List[OwnerRow]] = {}
    for org_id, key_id, added_at, added_via, primary in rows.all():
        owners.setdefault(org_id, []).append(OwnerRow(key_id, added_at, added_via, key_id == primary))
    for rows_of_org in owners.values():
        rows_of_org.sort(key=_order)
    return owners


async def owner_key_ids(db: AsyncSession, org_id: int) -> List[str]:
    return [row.api_key_id for row in (await owners_of(db, [org_id])).get(org_id, [])]


async def is_owner(db: AsyncSession, org: Organization, key_id: Optional[str]) -> bool:
    """Whether this key is in the organization's owner set. An unowned organization has no owner.

    The primary is always in the set, so it answers without a query; only another key is looked up.
    """
    if key_id is None or org.api_key_id is None:
        return False
    if org.api_key_id == key_id:
        return True
    if not key_id or not org.api_key_id:
        return False
    row = await db.execute(
        select(OrganizationOwnerKey.id).where(
            OrganizationOwnerKey.organization_id == org.id, OrganizationOwnerKey.api_key_id == key_id
        )
    )
    return row.first() is not None


async def owned_org_ids(db: AsyncSession, key_id: str) -> List[int]:
    """Every organization this key is an owner of, any source or state."""
    if not key_id:
        return []
    rows = await db.execute(
        select(OrganizationOwnerKey.organization_id).where(OrganizationOwnerKey.api_key_id == key_id)
    )
    return sorted(set(rows.scalars().all()))


def mark_entry(org: Organization, via: str, by: Optional[str] = None) -> None:
    """How the next primary written on this organization came in, for the listener that writes its row."""
    org._owner_added_via = via  # type: ignore[attr-defined]
    org._owner_added_by = by  # type: ignore[attr-defined]


async def replace_owners(
    db: AsyncSession, org: Organization, key_id: str, *, via: str, by: Optional[str]
) -> List[str]:
    """An Administrator's transfer: the set becomes this one key. Returns the keys removed.

    The new primary's row is written by the listener (via `via`) unless the key was already an owner.
    """
    rows = (await owners_of(db, [org.id])).get(org.id, [])
    removed = [row.api_key_id for row in rows if row.api_key_id != key_id]
    if org.api_key_id != key_id:
        mark_entry(org, via, by)
        org.api_key_id = key_id
    # note: a key that was already an owner now owns the organization by the transfer, so its row says so
    # (and lends the party a transfer lends, as before co-ownership)
    kept = await db.execute(
        select(OrganizationOwnerKey).where(
            OrganizationOwnerKey.organization_id == org.id, OrganizationOwnerKey.api_key_id == key_id
        )
    )
    for row in kept.scalars().all():
        row.added_via, row.added_by = via, by
    if removed:
        stale = await db.execute(
            select(OrganizationOwnerKey).where(
                OrganizationOwnerKey.organization_id == org.id, OrganizationOwnerKey.api_key_id.in_(removed)
            )
        )
        for row in stale.scalars().all():
            await db.delete(row)
    return removed


_local_locks_guard = threading.Lock()
_local_locks: Dict[Tuple[int, int], asyncio.Lock] = {}


def _advisory_key(org_id: int) -> int:
    digest = hashlib.sha256(f"orcastra:org-owners:{org_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _local_lock(org_id: int) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), org_id)
    with _local_locks_guard:
        return _local_locks.setdefault(key, asyncio.Lock())


@asynccontextmanager
async def serialize_org_owners(db: AsyncSession, org_ids: Iterable[int]) -> AsyncIterator[None]:
    """Hold these organizations' owner sets still while a caller decides and writes.

    Taken in sorted order, after any key locks, and held until the caller's commit (PostgreSQL
    transaction advisory locks) or the end of the context (the in-process fallback for SQLite and
    test sessions, as integration_key_ownership.serialize_key_ownership). The organizations are read
    again once the locks are held, so the caller decides on the rows the locks protect.
    """
    from app.services.integration_key_ownership import _dialect_name

    wanted = sorted({org_id for org_id in org_ids if org_id is not None})
    if _dialect_name(db) == "postgresql":
        for org_id in wanted:
            await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _advisory_key(org_id)})
        await _reload(db, wanted)
        yield
        return
    async with AsyncExitStack() as stack:
        for org_id in wanted:
            await stack.enter_async_context(_local_lock(org_id))
        await _reload(db, wanted)
        yield


async def _reload(db: AsyncSession, org_ids: List[int]) -> None:
    """Read these organizations again now that their sets are held, over any copy the session has.

    important: sessions do not expire on commit, so an organization a handler loaded before taking the
    lock keeps the primary it had then, and a decision on that copy can be stale. FOR UPDATE (PostgreSQL
    only; SQLite omits it) also holds the rows against a writer that does not take this lock.
    """
    if not org_ids:
        return
    # note: the rows must be fetched; populate_existing refreshes an object only as its row is read
    (await db.execute(
        select(Organization)
        .where(Organization.id.in_(org_ids))
        .order_by(Organization.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
