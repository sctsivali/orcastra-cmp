"""Which integration keys own an OrcaHub organization, and how an Administrator moves it.

A synced organization is owned by a set of integration keys (app.services.organization_owners);
`organizations.api_key_id` is its primary, the key OrcaHub callbacks use. Only an owner may sync it
(`_upsert_synced_organizations`), write its members and tenant access policies
(`_upsert_synced_members`, `sync_policies_from_external`) and provision tenant access for it
(`provision_tenant_access`). An organization whose owner is NULL is claimable by any key that
pushes or provisions it. That binding is what stops one integration rewriting another's
organizations, and it holds only while the owner key keeps working.

The writers of `api_key_id` are the three claim paths (the sync upsert, provisioning and the
dashboard's refresh from a key's callback) and the Administrator transfer below. There is no
release: an owner is replaced by another key, never set back to NULL, because a NULL owner is
open to whichever key names the organization next.

Nothing used to move an owner. Revoking or deleting the owner key left its organizations bound to
a key that can never authenticate again, so their sync, member updates and provisioning stopped
for good, and the only way out was SQL against production. `transfer_owner` is that way out, as
an audited Administrator action, and `refuse_if_key_owns_organizations` stops the key routes from
creating the state in the first place: a key that owns an active OrcaHub organization is not
revoked or deleted, for any caller, an Administrator included. Rotating its secret keeps the key
id, so it is the way to stop a leaked secret without freezing anything.

Concurrency: a decision about one key's ownership runs inside `serialize_key_ownership` for that
key, and the key is read and judged inside it, so two decisions about the same key never
interleave and a check cannot go stale before the write it allowed.

note: this line has no API key capabilities and no add-on credentials, so every key may sync and
none is an installation's. Ported from the dev line's PR #23, which judges both.
"""

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_expiry import classify_key, expiry_state
from app.models.db_models import Organization, OrganizationCluster, OrganizationOwnerKey
from app.services.organization_owners import owned_by_clause, owned_org_ids, replace_owners

logger = logging.getLogger(__name__)

TRANSFER_ACTION = "integration.organization.owner.transfer"

# The words an owner key's state is reported in. `unclaimed` is an organization with no owner,
# `missing` a key Vault says does not exist, `unknown` a key whose record could not be read.
OWNER_KEY_STATES = ("active", "expiring", "expired", "revoked", "inactive", "missing", "unknown", "unclaimed")

# What a refusal lists. The message names a few, the answer up to the cap, the count all of them.
MAX_LISTED_SLUGS = 20
MAX_NAMED_SLUGS = 5

OWNERSHIP_UNKNOWN_DETAIL = (
    "The organizations this key owns could not be checked, so nothing was changed. Try again."
)

_ENDING = {
    "revoke": "Revoking it",
    "delete": "Deleting it",
}

_local_locks_guard = threading.Lock()
_local_locks: Dict[Tuple[int, str], asyncio.Lock] = {}


def _owned_filter(key_ids: Iterable[str]) -> List[Any]:
    return [
        Organization.source == "orcahub",
        Organization.is_active.is_(True),
        owned_by_clause(key_ids),
    ]


async def owned_organizations(db: AsyncSession, key_id: str) -> List[Organization]:
    """The active OrcaHub organizations this key owns, by slug.

    note: not `owned_organization_count`, which counts every organization naming the key, inactive
    and of any source. This one answers what stops working when the key ends: only an active
    OrcaHub organization is synced and provisioned, so only one of those is frozen by losing its
    owner.
    """
    if not key_id:
        return []
    rows = await db.execute(select(Organization).where(*_owned_filter([key_id])).order_by(Organization.slug))
    return list(rows.scalars().all())


async def keys_whose_home_the_user_partners(db: AsyncSession, email: str, key_ids: Iterable[str]) -> Set[str]:
    """Of these keys, the ones whose home organization this user holds a current partner row in.

    The home is the one cluster_parties proves by a sale (home_partner_keys): the oldest organization
    the key's sync created, holding a cluster one of its own partners registered that the key sold on.
    A key whose home cannot be proven that way, one nobody has bought through yet included, has no home
    here, so only an Administrator acts for it.

    important: never a partner row in a buyer organization, and never created_by. OrcaHub makes every
    organization's founder a PARTNER, and provisioning stamps each buyer organization with the key's
    minter as created_by, so either would let a minter who left the seller and then bought from it act
    for the seller's key through their own buyer organization.

    important: no grace before the first sale. "Not sold yet" could only be read from the provisioning
    bindings that exist now, and deregistering a cluster deletes them, so a key whose seller
    organization and sales were gone would look unsold again and hand its oldest remaining synced
    organization, a buyer whose founder is a partner, the seller's key. This fails closed instead.
    """
    from app.services.cluster_parties import home_partner_keys

    wanted = {key_id for key_id in key_ids if key_id}
    if not wanted or not email:
        return set()
    return await home_partner_keys(db, email) & wanted


async def partners_the_home_organization(db: AsyncSession, key_id: str, email: str) -> Optional[bool]:
    """None when the key owns no active OrcaHub organization, else whether this user holds a current
    partner row in the key's home organization (keys_whose_home_the_user_partners).

    important: a secret rotation asks this of every caller but an Administrator, the key's creator
    included. A fresh secret for such a key is the sync, the member writes and the provisioning of
    every organization it owns, buyers included, so only the seller's current partners may take one.
    """
    if not await owned_organizations(db, key_id):
        return None
    return key_id in await keys_whose_home_the_user_partners(db, email, [key_id])


async def owned_by_keys(db: AsyncSession, key_ids: Iterable[str]) -> Dict[str, List[Tuple[int, str]]]:
    """For each key, the (id, slug) of every active OrcaHub organization it owns, by slug. One query."""
    wanted = {key_id for key_id in key_ids if key_id}
    if not wanted:
        return {}
    rows = await db.execute(
        select(OrganizationOwnerKey.api_key_id, Organization.id, Organization.slug)
        .join(OrganizationOwnerKey, OrganizationOwnerKey.organization_id == Organization.id)
        .where(
            Organization.source == "orcahub",
            Organization.is_active.is_(True),
            OrganizationOwnerKey.api_key_id.in_(sorted(wanted)),
        )
        .order_by(OrganizationOwnerKey.api_key_id, Organization.slug)
    )
    owned: Dict[str, List[Tuple[int, str]]] = {}
    for key_id, org_id, slug in rows.all():
        owned.setdefault(key_id, []).append((org_id, slug))
    return owned


def key_state(metadata: Optional[Mapping[str, Any]], *, missing: bool = False, unknown: bool = False) -> str:
    """One word for how an owner key stands: active, expiring, expired, revoked, inactive, missing or unknown.

    `classify_key` decides revoked, expired, inactive and active with the precedence the key badges
    and the authentication path use, so an unreadable expiry reads as expired here too. `expiring`
    is an active key inside the expiry warning window, which is when its organizations can still be
    moved before they stop syncing.
    """
    if unknown:
        return "unknown"
    if missing or not isinstance(metadata, Mapping) or not metadata:
        return "missing"
    state = classify_key(dict(metadata))
    if state == "active" and expiry_state(metadata.get("expires_at")) == "expiring":
        return "expiring"
    return state


class KeyOwnsOrganizations(HTTPException):
    """409: the key still owns active OrcaHub organizations. Carries the fields its audit record adds."""

    def __init__(self, detail: Dict[str, Any], audit_details: Dict[str, Any]) -> None:
        super().__init__(status_code=409, detail=detail)
        self.audit_details = audit_details


def _is_admin(user: Any) -> bool:
    from app.core.rbac import Role, get_highest_role, resolve_roles

    return get_highest_role(resolve_roles(user)) == Role.ADMIN


async def visible_organization_ids(db: AsyncSession, user: Any) -> Optional[Set[int]]:
    """The organizations this caller may see named, or None for an Administrator, who sees all."""
    from app.core.org_scope import get_user_org_ids

    if _is_admin(user):
        return None
    return set(await get_user_org_ids(user, db))


def name_organizations(slugs: List[str], hidden: int) -> str:
    """The slugs a message names, the rest counted: "a, b and 3 more and 1 you cannot see"."""
    shown = ", ".join(slugs[:MAX_NAMED_SLUGS])
    rest = []
    if len(slugs) > MAX_NAMED_SLUGS:
        rest.append(f"{len(slugs) - MAX_NAMED_SLUGS} more")
    if hidden:
        rest.append(f"{hidden} you cannot see")
    if not rest:
        return shown
    return f"{shown} and {' and '.join(rest)}" if shown else " and ".join(rest)


def _owned_message(action: str, total: int, slugs: List[str], hidden: int, *, admin: bool) -> str:
    one = total == 1
    them, their = ("it", "its") if one else ("them", "their")
    noun = "organization" if one else "organizations"
    effect = (
        f"{_ENDING.get(action, 'Ending it')} would leave {them} with an owner no other key can "
        f"replace, so {their} sync, member updates and provisioning would stop."
    )
    if admin:
        remedy = f"Transfer {them} to another active key first, or delete {them}."
    else:
        remedy = f"Ask an administrator to transfer {them} to another active key first, or to delete {them}."
    return " ".join([
        f"This key owns {total} OrcaHub {noun} ({name_organizations(slugs, hidden)}).",
        effect,
        remedy,
        "To stop a leaked secret now, rotate the secret instead: the key ID and its organizations stay.",
    ])


async def refuse_if_key_owns_organizations(db: AsyncSession, key_id: str, *, action: str, user: Any) -> None:
    """409 when the key owns an active OrcaHub organization, 503 when that cannot be read.

    The caller has already authorized the key (so an id outside the caller's reach is still 404),
    holds serialize_key_ownership(key_id), and has written nothing yet. There is no Administrator
    bypass: the remedy is to transfer the organizations or delete them, which an Administrator can.

    note: a caller who is not an Administrator sees only the slugs of organizations they belong to;
    the rest are counted, in the answer and in its audit record alike.

    important: the 503 detail is a fixed sentence. The exception text of a database failure names
    the database, and this answer reaches the browser.
    """
    try:
        owned = await owned_organizations(db, key_id)
        visible = await visible_organization_ids(db, user) if owned else None
    except Exception as exc:  # noqa: BLE001 - unknown ownership refuses, it never allows
        logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
    if not owned:
        return
    admin = visible is None
    slugs = [org.slug for org in owned if admin or org.id in visible]
    hidden = len(owned) - len(slugs)
    detail = {
        "error": "key_owns_organizations",
        "action": action,
        "message": _owned_message(action, len(owned), slugs, hidden, admin=admin),
        "api_key_id": key_id,
        "organization_count": len(owned),
        "organization_slugs": slugs[:MAX_LISTED_SLUGS],
        "hidden_organization_count": hidden,
        "remedies": ["transfer_owner", "delete_organization", "rotate_secret"],
    }
    # important: the same slugs the caller was shown. A refusal's record is visible to the caller who
    # triggered it, so naming the hidden organizations there would undo hiding them in the answer.
    audit = {
        "owned_organization_slugs": slugs[:MAX_LISTED_SLUGS],
        "owned_organization_count": len(owned),
        "owned_organization_hidden_count": hidden,
    }
    raise KeyOwnsOrganizations(detail, audit)


def owner_key_refusal(
    metadata: Optional[Mapping[str, Any]], failure: Optional[str]
) -> Optional[Tuple[int, str, str]]:
    """Why a key cannot become an owner, as (status, error code, message), or None when it can.

    An owner has to be able to do what owning means: sync the organization. So it is an active
    OrcaHub key. On this line every key may sync, so nothing narrower is asked of it.
    """
    if failure == "unknown":
        return 503, "target_key_unverifiable", (
            "The API key store could not be read, so the new owner key was not checked and "
            "nothing changed. Try again."
        )
    if failure == "missing" or not metadata:
        return 409, "target_key_missing", "No API key with this ID exists, so it cannot own an organization."
    state = classify_key(dict(metadata))
    if state != "active":
        return 409, f"target_key_{state}", (
            f"This API key is {state}, so it could not sync the organization. Choose an active key."
        )
    app_type = metadata.get("app_type")
    if app_type and app_type != "orcahub":
        return 409, "target_key_not_orcahub", (
            "This API key is not an OrcaHub connection, so it could not sync the organization."
        )
    return None


async def uncovered_clusters(
    db: AsyncSession, organization_id: int, metadata: Optional[Mapping[str, Any]]
) -> List[str]:
    """Clusters bound to the organization that the key's grant does not name.

    The owner still syncs the organization there, but provisioning tenant access on one of these
    clusters is refused until the key's grant names it.
    """
    bound = await db.execute(
        select(OrganizationCluster.cluster_id).where(OrganizationCluster.organization_id == organization_id)
    )
    granted = {
        grant.get("cluster_id")
        for grant in ((metadata or {}).get("cluster_permissions") or [])
        if isinstance(grant, dict)
    }
    return sorted({cluster_id for cluster_id in bound.scalars().all() if cluster_id and cluster_id not in granted})


async def owned_organization_count(db: AsyncSession, key_id: str) -> int:
    """How many organizations this key is an owner of, inactive and of any source included."""
    return len(await owned_org_ids(db, key_id))


def _dialect_name(db: AsyncSession) -> str:
    try:
        bind = db.get_bind()
        return str(bind.dialect.name).lower() if bind is not None else ""
    except (AttributeError, TypeError):
        return ""


def _advisory_key(key_id: str) -> int:
    digest = hashlib.sha256(f"orcastra:key-ownership:{key_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _local_lock(key_id: str) -> asyncio.Lock:
    # note: an asyncio lock belongs to one event loop, so it is keyed by the loop as well. Only
    # SQLite and light test sessions take this path; PostgreSQL serializes across workers below.
    key = (id(asyncio.get_running_loop()), key_id)
    with _local_locks_guard:
        return _local_locks.setdefault(key, asyncio.Lock())


@asynccontextmanager
async def serialize_key_ownership(db: AsyncSession, key_id: str) -> AsyncIterator[None]:
    """Hold one key's ownership still while a caller decides on it and writes.

    PostgreSQL's transaction advisory lock stays held until the caller's commit or rollback, so it
    serializes every worker and host. SQLite and light fake sessions use an in-process lock that
    is only good for tests. A caller keeps the context open through its write and its commit.
    """
    if _dialect_name(db) == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _advisory_key(key_id)})
        yield
        return
    async with _local_lock(key_id):
        yield


async def release_key_locks(db: AsyncSession) -> None:
    """End the transaction holding serialize_key_ownership's PostgreSQL lock, before slow work.

    The lock lasts until the transaction ends, so a caller with seconds of work left after its
    write, such as a sync's inventory reads, would keep a rotation or a revoke of the same key
    waiting. The caller has nothing else pending in the session when it calls this.
    """
    if _dialect_name(db) == "postgresql":
        await db.commit()


async def transfer_owner(
    db: AsyncSession, organization_id: int, to_key_id: str
) -> Tuple[Optional[Organization], Optional[str]]:
    """Make `to_key_id` the sole owner of one organization. Returns it and its previous primary.

    The caller holds serialize_key_ownership(to_key_id) and the organization's owner lock, has decided
    the key may own it, and commits. The row is read FOR UPDATE, so a sync or a second transfer of the
    same organization waits for this one, and the primary it reports is the one this write replaced. A
    transfer replaces the whole owner set (organization_owners.replace_owners), so every co-owner leaves
    with the old primary; an organization the key already solely owns is left unchanged.
    """
    # note: populate_existing, because the caller may already hold this row from an earlier read in
    # the same session, and the owner reported has to be the one the lock saw.
    org = (await db.execute(
        select(Organization)
        .where(Organization.id == organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if org is None:
        return None, None
    previous = org.api_key_id
    await replace_owners(db, org, to_key_id, via="transfer", by=None)
    return org, previous
