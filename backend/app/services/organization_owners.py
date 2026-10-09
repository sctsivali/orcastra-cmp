"""The set of integration keys that own a synced organization.

A synced (OrcaHub) organization is owned by a set of integration keys. Every key in the set may sync
the organization, write its members and tenant access policies and provision tenant access for it;
`organizations.api_key_id` is the primary, the one key OrcaHub callbacks are made with, and is always
in the set (the before_flush listener in db_models writes its row, and on PostgreSQL a deferred
foreign key refuses a primary outside the set). An organization with a NULL primary has no owner and
is claimable as before.

A key enters a set only by claiming an unowned organization (a sync or a refresh), by provisioning
creating one, by two-key enrollment from a key already in the set, or by an Administrator. A set is
never emptied: removing the last owner is refused, and an Administrator's transfer replaces the whole
set at once.

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
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationOwnerKey

logger = logging.getLogger(__name__)

# How many keys may own one organization. Enough for a rolling rotation and a cluster key per region;
# small enough that an owner set is reviewed rather than accumulated.
MAX_OWNER_KEYS = 5

ADD_ACTION = "integration.organization.owner.add"
REMOVE_ACTION = "integration.organization.owner.remove"
PRIMARY_ACTION = "integration.organization.owner.primary"

# important: the ways in which a key comes to own an organization that also put the organization on its
# minter's side (services/cluster_parties.py), and so let that side manage the key. A key enrolled by
# another owner or added by an Administrator owns the organization but lends no party: otherwise the
# seller who provisioned a buyer would manage, and could rotate, a key the buyer's founder enrolled.
# An allowlist, so a new way in lends nothing until it is decided here.
PARTY_ADDED_VIA = ("backfill", "claim", "provision", "transfer", "carry")


class LastOwner(Exception):
    """Removing this key would leave the organization with no owner."""


class NoAbleOwner(Exception):
    """No remaining owner key can sync the organization, so none can become its primary."""


class OwnerSetFull(Exception):
    """The organization already has MAX_OWNER_KEYS owner keys."""


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


async def add_owner(
    db: AsyncSession,
    org: Organization,
    key_id: str,
    *,
    via: str,
    by: Optional[str],
    proof_key_id: Optional[str] = None,
) -> bool:
    """Put a key in an owned organization's set. False when it is already there.

    The caller holds the key and organization locks, has decided the key may own it (it passes
    integration_key_ownership.owner_key_refusal) and commits. Raises OwnerSetFull at the cap.
    """
    current = await owner_key_ids(db, org.id)
    if key_id in current:
        return False
    if len(current) >= MAX_OWNER_KEYS:
        raise OwnerSetFull()
    db.add(OrganizationOwnerKey(
        organization_id=org.id, api_key_id=key_id, added_via=via, added_by=by, proof_key_id=proof_key_id,
    ))
    return True


def choose_primary(rows: Sequence[OwnerRow], able: Callable[[str], Optional[bool]], *, excluding: str) -> str:
    """The key that becomes primary when `excluding` stops being it: the oldest owner that is able.

    `able` answers whether a key can sync the organization, or None when that could not be read.
    Raises LookupError when a candidate could not be read before an able one was found (the caller
    answers 503) and NoAbleOwner when none is able.
    """
    for row in sorted(rows, key=_order):
        if row.api_key_id == excluding:
            continue
        verdict = able(row.api_key_id)
        if verdict is None:
            raise LookupError(row.api_key_id)
        if verdict:
            return row.api_key_id
    raise NoAbleOwner()


async def remove_owner(
    db: AsyncSession, org: Organization, key_id: str, *, able: Callable[[str], Optional[bool]]
) -> Optional[str]:
    """Take a key out of an organization's set. Returns the primary afterwards, or None if it was not in it.

    When the key is the primary, the oldest able remaining owner becomes primary in the same flush.
    Raises LastOwner when it is the only owner, NoAbleOwner or LookupError from choose_primary. The
    caller holds the locks and commits.
    """
    rows = (await owners_of(db, [org.id])).get(org.id, [])
    mine = [row for row in rows if row.api_key_id == key_id]
    if not mine:
        return None
    if len(rows) == 1:
        raise LastOwner()
    if org.api_key_id == key_id:
        org.api_key_id = choose_primary(rows, able, excluding=key_id)
    removed = await db.execute(
        select(OrganizationOwnerKey).where(
            OrganizationOwnerKey.organization_id == org.id, OrganizationOwnerKey.api_key_id == key_id
        )
    )
    for row in removed.scalars().all():
        await db.delete(row)
    return org.api_key_id


def set_primary(org: Organization, key_id: str) -> Optional[str]:
    """Make a key already in the set the primary. Returns the previous primary. The caller checked."""
    previous = org.api_key_id
    org.api_key_id = key_id
    return previous


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


def record_owner_change(
    action: str, org: Organization, *, before: Sequence[str], after: Sequence[str],
    primary_before: Optional[str], primary_after: Optional[str], details: Dict[str, Any],
) -> None:
    """One audit record for a change to an organization's owner set, after its commit. Never a secret."""
    from app.services.activity_logger import get_activity_logger

    try:
        get_activity_logger().log_activity(
            action=action,
            target_type="organization",
            target_id=str(org.id),
            target_organization_id=org.id,
            target_organization_slug=org.slug,
            details={
                "organization_slug": org.slug,
                "external_id": org.external_id,
                "owner_key_ids_before": list(before),
                "owner_key_ids_after": list(after),
                **details,
            },
            before_state={"owner_key_id": primary_before, "owner_key_ids": list(before)},
            after_state={"owner_key_id": primary_after, "owner_key_ids": list(after)},
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the change
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


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


async def check_owner_sets(db: AsyncSession) -> None:
    """Write the pending owner-set changes and check the primary-in-set rule now rather than at commit.

    For a caller that must stop before an outside write (Vault) when the database would refuse the change.
    On PostgreSQL the deferred foreign key from migration 015a is checked here; SQLite has no such key.
    """
    from app.services.integration_key_ownership import _dialect_name

    await db.flush()
    if _dialect_name(db) == "postgresql":
        await db.execute(text("SET CONSTRAINTS fk_org_primary_owner_key IMMEDIATE"))
        await db.execute(text("SET CONSTRAINTS fk_org_primary_owner_key DEFERRED"))


def multi_owner_enabled() -> bool:
    """INTEGRATION_MULTI_OWNER_ENABLED: whether keys may join an owner set and the owner-key routes answer."""
    from app.core.config import get_settings

    return bool(getattr(get_settings(), "integration_multi_owner_enabled", False))
