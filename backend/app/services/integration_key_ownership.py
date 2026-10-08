"""Which integration key owns an OrcaHub organization, and how an Administrator moves it.

`organizations.api_key_id` names the integration key that owns a synced organization. Only the
owner may sync it (`_upsert_synced_organizations`), write its members and tenant access policies
(`_upsert_synced_members`, `sync_policies_from_external`) and provision tenant access for it
(`provision_tenant_access`). An organization whose owner is NULL is claimable by any key that
pushes or provisions it. That binding is what stops one integration rewriting another's
organizations, and it holds only while the owner key keeps working.

The writers of `api_key_id` are the three claim paths (the sync upsert, provisioning and the
dashboard's refresh from a key's callback), the Administrator transfer below, and the carry an
add-on credential's rotation makes to its new key (carry_owned_organizations). There is no
release: an owner is replaced by another key, never set back to NULL, because a NULL owner is
open to whichever key names the organization next.

An add-on credential may hold org.sync, so it can own organizations too. Its rotation carries
them to the new credential in the same commit as the installation row, uninstalling it is refused
while it owns any (suspending the add-on is the way to stop it at once), and the reconcile sweep,
which still revokes a credential that must stop, records the organizations that revocation
leaves frozen.

Nothing used to move an owner. Revoking or deleting the owner key left its organizations bound to
a key that can never authenticate again, so their sync, member updates and provisioning stopped
for good, and the only way out was SQL against production. `transfer_owner` is that way out, as
an audited Administrator action, and `refuse_if_key_owns_organizations` stops the key routes from
creating the state in the first place: a key that owns an active OrcaHub organization is not
revoked, deleted or stripped of org.sync, for any caller, an Administrator included. Rotating its
secret keeps the key id, so it is the way to stop a leaked secret without freezing anything.

Concurrency: a decision about one key's ownership runs inside `serialize_key_ownership` for that
key, and the key is read and judged inside it, so two decisions about the same key never
interleave and a check cannot go stale before the write it allowed.
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

from app.core.api_key_capabilities import has_capability
from app.core.api_key_expiry import classify_key, expiry_state
from app.models.db_models import Organization, OrganizationCluster
from app.services.add_on_credential_edits import is_add_on_credential

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
    "capabilities": "Withdrawing its org.sync capability",
    "uninstall": "Uninstalling the add-on",
}

_local_locks_guard = threading.Lock()
_local_locks: Dict[Tuple[int, str], asyncio.Lock] = {}


def _owned_filter(key_ids: Iterable[str]) -> List[Any]:
    return [
        Organization.source == "orcahub",
        Organization.is_active.is_(True),
        Organization.api_key_id.in_(sorted(set(key_ids))),
    ]


async def owned_organizations(db: AsyncSession, key_id: str) -> List[Organization]:
    """The active OrcaHub organizations this key owns, by slug.

    note: not `claimed_organization_count` in integration_cluster_rebind. That one counts every
    organization carrying the key, inactive and of any source, because it decides whether a push
    may rebind a cluster, and an inactive organization is still a party the key serves there. This
    one answers what stops working when the key ends: only an active OrcaHub organization is synced
    and provisioned, so only one of those is frozen by losing its owner.
    """
    if not key_id:
        return []
    rows = await db.execute(select(Organization).where(*_owned_filter([key_id])).order_by(Organization.slug))
    return list(rows.scalars().all())


async def owned_by_keys(db: AsyncSession, key_ids: Iterable[str]) -> Dict[str, List[Tuple[int, str]]]:
    """For each key, the (id, slug) of every active OrcaHub organization it owns, by slug. One query."""
    wanted = {key_id for key_id in key_ids if key_id}
    if not wanted:
        return {}
    rows = await db.execute(
        select(Organization.api_key_id, Organization.id, Organization.slug)
        .where(*_owned_filter(wanted))
        .order_by(Organization.api_key_id, Organization.slug)
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


def _named(slugs: List[str], hidden: int) -> str:
    shown = ", ".join(slugs[:MAX_NAMED_SLUGS])
    rest = []
    if len(slugs) > MAX_NAMED_SLUGS:
        rest.append(f"{len(slugs) - MAX_NAMED_SLUGS} more")
    if hidden:
        rest.append(f"{hidden} you cannot see")
    if not rest:
        return shown
    return f"{shown} and {' and '.join(rest)}" if shown else " and ".join(rest)


def _owned_message(action: str, total: int, slugs: List[str], hidden: int, *, admin: bool, add_on: bool) -> str:
    one = total == 1
    them, their = ("it", "its") if one else ("them", "their")
    noun = "organization" if one else "organizations"
    if action == "capabilities":
        effect = (
            f"{_ENDING[action]} would leave {them} with an owner that can no longer sync {them}, so "
            f"{their} sync and member updates would stop."
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
    parts = [f"This key owns {total} OrcaHub {noun} ({_named(slugs, hidden)}).", effect, remedy]
    if add_on:
        parts.append("Suspend the add-on to stop it now.")
    elif action in ("revoke", "delete"):
        parts.append(
            "To stop a leaked secret now, rotate the secret instead: the key ID and its organizations stay."
        )
    return " ".join(parts)


async def refuse_if_key_owns_organizations(
    db: AsyncSession, key_id: str, *, action: str, user: Any, metadata: Mapping[str, Any]
) -> None:
    """409 when the key owns an active OrcaHub organization, 503 when that cannot be read.

    The caller has already authorized the key (so an id outside the caller's reach is still 404),
    holds serialize_key_ownership(key_id), and has written nothing yet. There is no Administrator
    bypass: the remedy is to transfer the organizations or delete them, which an Administrator can.

    note: a caller who is not an Administrator sees only the slugs of organizations they belong to;
    the rest are counted. The audit record names them all, up to the cap.

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
    add_on = is_add_on_credential(metadata)
    if add_on:
        remedies = ["transfer_owner", "delete_organization", "suspend_add_on"]
    elif action in ("revoke", "delete"):
        remedies = ["transfer_owner", "delete_organization", "rotate_secret"]
    else:
        remedies = ["transfer_owner", "delete_organization"]
    detail = {
        "error": "key_owns_organizations",
        "action": action,
        "message": _owned_message(action, len(owned), slugs, hidden, admin=admin, add_on=add_on),
        "api_key_id": key_id,
        "organization_count": len(owned),
        "organization_slugs": slugs[:MAX_LISTED_SLUGS],
        "hidden_organization_count": hidden,
        "remedies": remedies,
    }
    audit = {
        "owned_organization_slugs": [org.slug for org in owned][:MAX_LISTED_SLUGS],
        "owned_organization_count": len(owned),
    }
    raise KeyOwnsOrganizations(detail, audit)


async def carry_owned_organizations(
    db: AsyncSession, *, from_key_id: str, to_key_id: str
) -> List[Tuple[int, str, Optional[str]]]:
    """Move every organization one key owns to another. The caller holds the lock and commits.

    For an add-on credential's rotation: the new key takes the old one's organizations in the same
    commit as the installation row, so the add-on keeps syncing them. Every organization carrying
    the old key moves, active or not, because the old key is being retired and one left on it could
    never be synced again. Returns (id, slug, external_id) of each one moved.

    important: an ORM assignment, like transfer_owner. The claim-path ratchet finds every writer of
    organizations.api_key_id by that shape.
    """
    if not from_key_id or not to_key_id or from_key_id == to_key_id:
        return []
    rows = await db.execute(
        select(Organization)
        .where(Organization.api_key_id == from_key_id)
        .order_by(Organization.slug)
        .with_for_update()
    )
    carried: List[Tuple[int, str, Optional[str]]] = []
    for org in rows.scalars().all():
        org.api_key_id = to_key_id
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
    OrcaHub key holding org.sync (a legacy key without a capability list holds every one), and not
    an add-on credential, which its installation owns and rotates.

    note: an add-on credential is named before its state. Its app_type is automation, so the
    later test would call it "not an OrcaHub connection", which is true and says less.
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
    if not has_capability(dict(metadata), "org.sync"):
        return 409, "target_key_cannot_sync", (
            "This API key does not have the org.sync capability, so it could not sync the organization."
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
    is only good for tests, the same split as shared_cluster_audit.serialize_cluster_configuration.
    A caller keeps the context open through its write and its commit.
    """
    if _dialect_name(db) == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _advisory_key(key_id)})
        yield
        return
    async with _local_lock(key_id):
        yield


async def transfer_owner(
    db: AsyncSession, organization_id: int, to_key_id: str
) -> Tuple[Optional[Organization], Optional[str]]:
    """Make `to_key_id` the owner of one organization. Returns it and its previous owner.

    The caller holds serialize_key_ownership(to_key_id), has decided the key may own it, and
    commits. The row is read FOR UPDATE, so a sync or a second transfer of the same organization
    waits for this one, and the owner it reports is the one this write replaced. An organization
    the key already owns is returned unchanged, so the caller can answer without writing.

    important: an ORM assignment, never an UPDATE statement. The claim-path ratchet in
    tests/test_the_one_org_rule_holds_for_orcahubs_bodies_and_every_claim_path.py finds every
    writer of organizations.api_key_id by that shape.
    """
    org = (await db.execute(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )).scalar_one_or_none()
    if org is None:
        return None, None
    previous = org.api_key_id
    if previous != to_key_id:
        org.api_key_id = to_key_id
    return org, previous
