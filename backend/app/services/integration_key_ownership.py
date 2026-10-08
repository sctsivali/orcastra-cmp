"""Which integration key owns an OrcaHub organization, and how an Administrator moves it.

`organizations.api_key_id` names the integration key that owns a synced organization. Only the
owner may sync it (`_upsert_synced_organizations`), write its members and tenant access policies
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
an audited Administrator action.

Concurrency: a decision about one key's ownership runs inside `serialize_key_ownership` for that
key, and the key is read and judged inside it, so two decisions about the same key never
interleave and a check cannot go stale before the write it allowed.
"""

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional, Tuple

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
