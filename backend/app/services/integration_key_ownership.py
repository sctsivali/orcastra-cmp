"""Which integration keys own an OrcaHub organization, and how an Administrator moves it.

An organization is owned by a set of keys (app.services.organization_owners), with
`organizations.api_key_id` as the primary that OrcaHub callbacks use. Only an owner may sync it,
write its members and policies, and provision tenant access for it. `api_key_id` is written only by
the claim paths (sync upsert, provisioning, callback refresh), the Administrator transfer below and
an add-on credential's rotation. It is never set back to NULL: a NULL owner is claimable by
whichever key pushes or provisions the organization next.

An organization whose owner key stops working can no longer sync, update members or provision, so
a key that owns an active OrcaHub organization is not revoked, deleted or stripped of a capability
it syncs with, for any caller, an Administrator included. Rotating the secret keeps the key id, so
it stops a leaked secret without freezing anything. An add-on credential carries its organizations
to its new credential on rotation and cannot be uninstalled while it owns any; when the reconcile
sweep revokes one that must stop, it records the organizations left frozen.

Each decision about one key runs inside `serialize_key_ownership`, with the key read and judged
inside it, so a check cannot go stale before the write it allowed.
"""

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capabilities import has_capability
from app.core.api_key_expiry import classify_key, expiry_state
from app.models.db_models import Organization, OrganizationCluster, OrganizationOwnerKey
from app.services.add_on_credential_edits import is_add_on_credential
from app.services.organization_owners import (
    LastOwner,
    NoAbleOwner,
    OwnerRow,
    carry,
    drop_unable_primary,
    multi_owner_enabled,
    owned_by_clause,
    owners_of,
    remove_owner,
    replace_owners,
)

logger = logging.getLogger(__name__)

TRANSFER_ACTION = "integration.organization.owner.transfer"

LAST_OWNER_DETAIL = {
    "error": "last_owner",
    "message": (
        "Another request changed this organization's owner keys, and this key is now its only owner able "
        "to sync it. Nothing was changed. Transfer the organization to another key, or add an owner key first."
    ),
}

# The words an owner key's state is reported in. `unclaimed` is an organization with no owner,
# `missing` a key Vault says does not exist, `unknown` a key whose record could not be read.
OWNER_KEY_STATES = ("active", "expiring", "expired", "revoked", "inactive", "missing", "unknown", "unclaimed")

# What an owner syncs with. /sync admits a key holding inventory.read, and the organization and
# member half of the push needs org.sync too, so an owner missing either one freezes what it owns.
SYNC_CAPABILITIES = ("inventory.read", "org.sync")

# What a refusal lists. The message names a few, the answer up to the cap, the count all of them.
MAX_LISTED_SLUGS = 20
MAX_NAMED_SLUGS = 5

OWNERSHIP_UNKNOWN_DETAIL = (
    "The organizations this key owns could not be checked, so nothing was changed. Try again."
)

_ENDING = {
    "revoke": "Revoking it",
    "delete": "Deleting it",
    "uninstall": "Uninstalling the add-on",
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

    This answers what stops working when the key ends: only an active OrcaHub organization is
    synced and provisioned. `claimed_organization_count` in integration_cluster_rebind counts every
    organization carrying the key instead, since an inactive one is still a party the key serves
    when a push rebinds a cluster.
    """
    if not key_id:
        return []
    rows = await db.execute(select(Organization).where(*_owned_filter([key_id])).order_by(Organization.slug))
    return list(rows.scalars().all())


async def keys_whose_home_the_user_partners(db: AsyncSession, email: str, key_ids: Iterable[str]) -> Set[str]:
    """Of these keys, the ones whose home organization this user holds a current partner row in.

    The home is what cluster_parties.home_partner_keys proves by a sale: the oldest organization the
    key's sync created, holding a cluster its own partner registered that the key sold on. A key with
    no proven home, an unsold one included, is left to an Administrator. No grace before a first sale:
    "unsold" can only be read from provisioning bindings, which deregistering deletes, so a buyer
    could become the home. A buyer's partner rows and created_by never count either: OrcaHub makes
    every founder a PARTNER, and provisioning stamps each buyer with the key's minter as created_by,
    so either would let a minter who left the seller and bought from it act for the seller's key.
    """
    from app.services.cluster_parties import home_partner_keys

    wanted = {key_id for key_id in key_ids if key_id}
    if not wanted or not email:
        return set()
    return await home_partner_keys(db, email) & wanted


async def partners_the_home_organization(db: AsyncSession, key_id: str, email: str) -> Optional[bool]:
    """None when the key owns no active OrcaHub organization, else whether this user holds a current
    partner row in the key's home organization (keys_whose_home_the_user_partners).

    A secret rotation asks this of every caller but an Administrator, the key's creator included. A
    fresh secret for such a key is the sync, the member writes and the provisioning of every
    organization it owns, buyers included, so only the seller's current partners may take one.
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


def _capability_names(names: Sequence[str]) -> str:
    word = "capability" if len(names) == 1 else "capabilities"
    return f"{' and '.join(names)} {word}"


def sync_capabilities_missing(metadata: Mapping[str, Any]) -> List[str]:
    """The SYNC_CAPABILITIES a key does not hold. A legacy key, with no list, holds them all."""
    return [name for name in SYNC_CAPABILITIES if not has_capability(dict(metadata), name)]


def _owned_message(
    action: str,
    total: int,
    slugs: List[str],
    hidden: int,
    *,
    admin: bool,
    add_on: bool,
    withdrawn: Sequence[str] = (),
    co_owner: bool = False,
) -> str:
    one = total == 1
    them, their = ("it", "its") if one else ("them", "their")
    noun = "organization" if one else "organizations"
    if action == "capabilities":
        effect = (
            f"Withdrawing its {_capability_names(list(withdrawn) or ['org.sync'])} would leave {them} "
            f"with an owner that can no longer sync {them}, so {their} sync and member updates would stop."
        )
    else:
        effect = (
            f"{_ENDING.get(action, 'Ending it')} would leave {them} with an owner no other key can "
            f"replace, so {their} sync, member updates and provisioning would stop."
        )
    if admin:
        remedy = f"Transfer {them} to another active key first, or delete {them}."
    else:
        remedy = f"Ask an administrator to transfer {them} to another active key first, or to delete {them}."
    if co_owner:
        effect = effect.replace(
            "with an owner no other key can replace", "with no other owner key able to sync them"
        ) if total != 1 else effect.replace(
            "with an owner no other key can replace", "with no other owner key able to sync it"
        )
        remedy += (
            f" Or make another active key a co-owner of {them} first (OrcaHub's Make Co-owner, or an "
            "administrator), then end this one."
        )
    parts = [f"This key is the only able owner of {total} OrcaHub {noun} ({name_organizations(slugs, hidden)})."
             if co_owner else f"This key owns {total} OrcaHub {noun} ({name_organizations(slugs, hidden)}).",
             effect, remedy]
    if add_on:
        parts.append("Suspend the add-on to stop it now.")
    elif action in ("revoke", "delete"):
        parts.append(
            "To stop a leaked secret now, rotate the secret instead: the key ID and its organizations stay."
        )
    return " ".join(parts)


MetadataReader = Callable[[str], Awaitable[Tuple[Optional[Dict[str, Any]], Optional[str]]]]


@dataclass
class OwnerHandoff:
    """An organization a key leaves, or stops being able to serve, while another owner carries on.

    `able` holds, for every other owner key, whether it can sync the organization (True or False),
    as read under the locks; the handoff moves the primary along that same verdict.
    """

    org: Organization
    owners: List[OwnerRow]
    able: Dict[str, bool] = field(default_factory=dict)


async def refuse_if_key_owns_organizations(
    db: AsyncSession,
    key_id: str,
    *,
    action: str,
    user: Any,
    metadata: Mapping[str, Any],
    withdrawn: Sequence[str] = (),
    read_metadata: Optional[MetadataReader] = None,
) -> List[OwnerHandoff]:
    """409 when the key is the only able owner of an active OrcaHub organization, 503 when that cannot
    be read; otherwise the organizations it co-owns, for apply_owner_handoffs.

    `withdrawn` is the SYNC_CAPABILITIES a capability edit removes. `read_metadata(key_id)` returns
    another owner's uncached (metadata, failure); without it every other owner is unreadable.
    An organization another owner can still sync (active, holding SYNC_CAPABILITIES, an OrcaHub key)
    does not stop this key ending; one where every other owner is unreadable is a 503, since
    allowing it might freeze the organization. There is no Administrator bypass.

    important: the caller has already authorized the key (an id outside its reach is still 404),
    holds serialize_key_ownership(key_id) and serialize_org_owners over owned_org_ids_to_lock, and
    has written nothing yet. A non-Administrator sees only the slugs of their own organizations; the
    rest are counted. The 503 detail is fixed: a database error's text names the database, and this
    answer reaches the browser.
    """
    try:
        owned = await owned_organizations(db, key_id)
        owners = await owners_of(db, [org.id for org in owned]) if owned else {}
        visible = await visible_organization_ids(db, user) if owned else None
    except Exception as exc:  # noqa: BLE001 - unknown ownership refuses, it never allows
        logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
    if not owned:
        return []
    verdicts = await able_owners(
        {row.api_key_id for rows in owners.values() for row in rows if row.api_key_id != key_id}, read_metadata
    )
    sole: List[Organization] = []
    handoffs: List[OwnerHandoff] = []
    unknown = False
    for org in owned:
        others = [row.api_key_id for row in owners.get(org.id, []) if row.api_key_id != key_id]
        if any(verdicts.get(other) for other in others):
            handoffs.append(OwnerHandoff(org, owners.get(org.id, []), {k: bool(v) for k, v in verdicts.items() if v is not None}))
        elif any(verdicts.get(other) is None for other in others):
            unknown = True
        else:
            sole.append(org)
    if unknown and not sole:
        logger.warning("Could not read the other owner keys of an organization api key %s co-owns", key_id)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL)
    if not sole:
        return handoffs
    co_owner = multi_owner_enabled()
    admin = visible is None
    slugs = [org.slug for org in sole if admin or org.id in visible]
    hidden = len(sole) - len(slugs)
    add_on = is_add_on_credential(metadata)
    if add_on:
        remedies = ["transfer_owner", "delete_organization", "suspend_add_on"]
    elif action in ("revoke", "delete"):
        remedies = ["transfer_owner", "delete_organization", "rotate_secret"]
    else:
        remedies = ["transfer_owner", "delete_organization"]
    if co_owner:
        remedies.append("add_owner_key")
    detail = {
        "error": "key_owns_organizations",
        "action": action,
        "message": _owned_message(
            action, len(sole), slugs, hidden, admin=admin, add_on=add_on, withdrawn=withdrawn, co_owner=co_owner
        ),
        "api_key_id": key_id,
        "organization_count": len(sole),
        "organization_slugs": slugs[:MAX_LISTED_SLUGS],
        "hidden_organization_count": hidden,
        "remedies": remedies,
    }
    if co_owner:
        # Organizations another owner keeps serving; these would not have stopped this key.
        detail["co_owned_organization_count"] = len(handoffs)
    # important: the same slugs the caller was shown. A refusal's record is visible to the caller who
    # triggered it, so naming the hidden organizations there would undo hiding them in the answer.
    audit = {
        "owned_organization_slugs": slugs[:MAX_LISTED_SLUGS],
        "owned_organization_count": len(sole),
        "owned_organization_hidden_count": hidden,
    }
    raise KeyOwnsOrganizations(detail, audit)


async def able_owners(key_ids: Iterable[str], read_metadata: Optional[MetadataReader]) -> Dict[str, Optional[bool]]:
    """For each key, whether it could own an organization now (owner_key_refusal), None when unreadable."""
    verdicts: Dict[str, Optional[bool]] = {}
    for other in sorted(set(key_ids)):
        if read_metadata is None:
            verdicts[other] = None
            continue
        try:
            other_metadata, failure = await read_metadata(other)
        except Exception:  # noqa: BLE001 - an unreadable owner is unknown, never able
            verdicts[other] = None
            continue
        refused = owner_key_refusal(other_metadata, failure)
        verdicts[other] = None if refused is not None and refused[0] >= 500 else refused is None
    return verdicts


async def apply_owner_handoffs(
    db: AsyncSession, key_id: str, handoffs: Sequence[OwnerHandoff], *, remove: bool
) -> List[Tuple[Organization, List[str], Optional[str], Optional[str]]]:
    """Move the primary off a key that ends (and take it out of the sets when `remove`).

    For revoke and delete the key leaves every set it was in (`remove`); for a withdrawn capability it
    stays an owner but stops being the primary. The caller holds the locks and commits after its Vault
    write. Returns (org, owners before, primary before, primary after) for the audit records.
    """
    changed = []
    for handoff in handoffs:
        org = handoff.org
        before = [row.api_key_id for row in handoff.owners]
        primary_before = org.api_key_id

        def able(other: str, verdicts: Dict[str, bool] = handoff.able) -> Optional[bool]:
            return verdicts.get(other)

        # important: answer 409 or 503, never a 500. The refusal above was decided on the same locked
        # rows, so this is not expected; every caller runs it before its Vault write, so nothing is
        # written when it does happen.
        try:
            if remove:
                await remove_owner(db, org, key_id, able=able)
            else:
                await drop_unable_primary(db, org, key_id, able=able)
        except (LastOwner, NoAbleOwner):
            raise HTTPException(status_code=409, detail=LAST_OWNER_DETAIL) from None
        except LookupError:
            raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
        changed.append((org, before, primary_before, org.api_key_id))
    return changed


def record_owner_handoffs(
    changes: Sequence[Tuple[Organization, List[str], Optional[str], Optional[str]]], *, key_id: str, action: str,
    actor_email: Optional[str], remove: bool,
) -> None:
    """One audit record per organization a key left or stopped being primary of, after the commit."""
    from app.services.organization_owners import PRIMARY_ACTION, REMOVE_ACTION, record_owner_change

    for org, before, primary_before, primary_after in changes:
        after = [k for k in before if not (remove and k == key_id)]
        if not remove and primary_before == primary_after:
            continue
        record_owner_change(
            REMOVE_ACTION if remove else PRIMARY_ACTION, org,
            before=before, after=after, primary_before=primary_before, primary_after=primary_after,
            details={"owner_key_id": key_id, "trigger": action, "actor_email": actor_email},
        )


async def owned_org_ids_to_lock(db: AsyncSession, key_id: str) -> List[int]:
    """The organizations whose owner sets a key's end may change: every active OrcaHub one it owns.

    An unreadable database answers 503 with the fixed sentence, as refuse_if_key_owns_organizations does.
    """
    try:
        return [org.id for org in await owned_organizations(db, key_id)]
    except Exception as exc:  # noqa: BLE001 - unknown ownership refuses, it never allows
        logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None


async def carry_owned_organizations(
    db: AsyncSession, *, from_key_id: str, to_key_id: str
) -> List[Tuple[int, str, Optional[str]]]:
    """Move every organization one key owns to another. The caller holds the lock and commits.

    For an add-on credential's rotation: the new key takes the outgoing key's place in the owner set
    of every organization it is in, in the same commit as the installation row, so the add-on keeps
    syncing them, and keeps the primary where the outgoing key held it. Every organization moves,
    active or not, because one left on the outgoing key could never be synced again. Returns
    (id, slug, external_id) of each one moved. The writes are organization_owners.carry.
    """
    if not from_key_id or not to_key_id or from_key_id == to_key_id:
        return []
    rows = await db.execute(
        select(Organization)
        .where(owned_by_clause([from_key_id]))
        .order_by(Organization.slug)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    carried: List[Tuple[int, str, Optional[str]]] = []
    for org in rows.scalars().all():
        await carry(db, org, from_key_id=from_key_id, to_key_id=to_key_id)
        carried.append((org.id, org.slug, org.external_id))
    return carried


def record_carried_organizations(
    carried: List[Tuple[int, str, Optional[str]]], *, from_key_id: str, to_key_id: str, trigger: str
) -> None:
    """One transfer record per organization a carry moved, after the commit that moved them."""
    from app.services.activity_logger import get_activity_logger

    activity_logger = get_activity_logger()
    for org_id, slug, external_id in carried:
        try:
            activity_logger.log_activity(
                action=TRANSFER_ACTION,
                target_type="organization",
                target_id=str(org_id),
                target_organization_id=org_id,
                target_organization_slug=slug,
                details={
                    "organization_slug": slug,
                    "external_id": external_id,
                    "from_key_id": from_key_id,
                    "to_key_id": to_key_id,
                    "trigger": trigger,
                },
                before_state={"owner_key_id": from_key_id},
                after_state={"owner_key_id": to_key_id},
            )
        except Exception:  # pragma: no cover - a failed audit write must not fail the rotation
            logger.warning("Could not write the audit record for %s", TRANSFER_ACTION, exc_info=True)


def owner_key_refusal(
    metadata: Optional[Mapping[str, Any]], failure: Optional[str]
) -> Optional[Tuple[int, str, str]]:
    """Why a key cannot become an owner, as (status, error code, message), or None when it can.

    An owner has to be able to do what owning means: sync the organization. So it is an active
    OrcaHub key holding SYNC_CAPABILITIES (a legacy key without a capability list holds every one),
    and not an add-on credential, which its installation owns and rotates.

    An add-on credential is named before its state. Its app_type is automation, so the later test
    would call it "not an OrcaHub connection", which is true but says less.
    """
    if failure == "unknown":
        return 503, "target_key_unverifiable", (
            "The API key store could not be read, so the new owner key was not checked and "
            "nothing changed. Try again."
        )
    if failure == "missing" or not metadata:
        return 409, "target_key_missing", "No API key with this ID exists, so it cannot own an organization."
    if is_add_on_credential(metadata):
        return 409, "target_key_is_add_on", (
            "This API key is an add-on credential. Its installation owns it, so it cannot own an "
            "OrcaHub organization."
        )
    state = classify_key(dict(metadata))
    if state != "active":
        return 409, f"target_key_{state}", (
            f"This API key is {state}, so it could not sync the organization. Choose an active key."
        )
    missing = sync_capabilities_missing(metadata)
    if missing:
        return 409, "target_key_cannot_sync", (
            f"This API key does not have the {_capability_names(missing)}, so it could not sync the "
            "organization."
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
    # A grant for a registration that has ended covers nothing: the id may be someone else's now.
    from app.services import cluster_generations

    granted = cluster_generations.reachable(
        (metadata or {}).get("cluster_permissions"), await cluster_generations.current_or_none()
    )
    return sorted({cluster_id for cluster_id in bound.scalars().all() if cluster_id and cluster_id not in granted})


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
    # An asyncio lock belongs to one event loop, so it is keyed by the loop as well. Only SQLite
    # and light test sessions take this path; PostgreSQL serializes across workers below.
    key = (id(asyncio.get_running_loop()), key_id)
    with _local_locks_guard:
        return _local_locks.setdefault(key, asyncio.Lock())


@asynccontextmanager
async def serialize_key_ownership(db: AsyncSession, key_id: str) -> AsyncIterator[None]:
    """Hold one key's ownership still while a caller decides on it and writes.

    PostgreSQL's transaction advisory lock stays held until the caller's commit or rollback, so it
    serializes every worker and host. SQLite and light fake sessions use an in-process lock that
    is only good for tests, the same split as shared_cluster_audit.serialize_cluster_configuration.
    A caller keeps the context open through its write and its commit.
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

    The caller holds serialize_key_ownership(to_key_id), has decided the key may own it, and
    commits. The row is read FOR UPDATE, so a sync or a second transfer of the same organization
    waits for this one, and the primary it reports is the one this write replaced. A transfer replaces
    the whole owner set (organization_owners.replace_owners), so every co-owner leaves with the
    previous primary; an organization the key already solely owns is left unchanged.
    """
    # populate_existing: the caller may already hold this row from an earlier read in the same
    # session, and the owner reported has to be the one the lock saw.
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
