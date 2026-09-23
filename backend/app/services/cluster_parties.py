"""Who holds a cluster, decided by party rather than by how many organizations are bound to it.

A cluster carries one binding per organization. Counting bindings calls every partner cluster
with a sale "shared", because provisioning binds each buyer organization to the partner's
cluster. The questions "may this partner grant, manage or reach the whole of this cluster" need
the other answer: is any binding held by someone outside the caller's team.

The team is the caller and everyone who runs one of the caller's organizations (a member above
tenant). An organization's creator never joins it: synced organizations carry "system" and an
admin may have created organizations for several partners, so a creator would join unrelated
partners into one team. A buyer organization belongs to the team of whoever minted the key that
created it. A key that no longer exists holds nothing.
"""

import logging
from typing import Dict, Iterable, Set

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.services.lxd.proxy import VALIDATE_KEY_TIMEOUT, auth_executor

logger = logging.getLogger(__name__)

API_KEYS_VAULT_PATH = "secret/data/integrations/api_keys"
# A key Vault answered 404 for. Its organizations hold nothing, while an unreadable key still does.
DELETED_KEY = "\0deleted"


def _is_not_found(exc: BaseException) -> bool:
    seen = exc
    while seen is not None:
        if getattr(getattr(seen, "response", None), "status_code", None) == 404:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def read_key_creators_blocking(key_ids: Set[str]) -> Dict[str, str]:
    """created_by of each key id. Runs on auth_executor.

    A key Vault no longer has maps to DELETED_KEY, and one it could not read maps to ''.
    """
    from app.core.config import get_settings
    from app.core.vault_client import get_vault_client

    settings = get_settings()
    creators: Dict[str, str] = {key_id: "" for key_id in key_ids}
    if not settings.vault_enabled or not settings.vault_token:
        return creators
    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
    for key_id in sorted(key_ids):
        try:
            metadata = vault_client.vault_read(f"{API_KEYS_VAULT_PATH}/{key_id}/metadata")
        except Exception as exc:
            creators[key_id] = DELETED_KEY if _is_not_found(exc) else ""
            continue
        creators[key_id] = str((metadata or {}).get("created_by") or "")
    return creators


async def _run_orgs(db: AsyncSession, email: str) -> Set[int]:
    """Organizations this user runs: created, or joined in a role above tenant.

    important: not get_org_ids_for_email, which counts any membership. A partner who also buys from
    another partner is a tenant of that seller's buyer organization, and counting it would put the
    seller, and the seller's keys and clusters, on the buyer's side.
    """
    from app.models.db_models import Organization, OrganizationMember

    if not email:
        return set()
    rows = (await db.execute(
        select(Organization.id)
        .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy column comparison
            or_(
                Organization.created_by == email,
                (OrganizationMember.user_email == email) & (OrganizationMember.role != "tenant"),
            ),
        )
    )).scalars().all()
    return set(rows)


async def side_of(db: AsyncSession, email: str) -> Set[str]:
    """Lowercased emails on the caller's team: themselves and the non-tenant members of their orgs."""
    from app.models.db_models import OrganizationMember

    side = {email.lower()} if email else set()
    own = await _run_orgs(db, email)
    if not own:
        return side
    # note: tenants of an organization the caller runs are its customers, not its team, and an
    # organization's creator is never counted (see the module docstring).
    members = (await db.execute(
        select(OrganizationMember.user_email).where(
            OrganizationMember.organization_id.in_(own), OrganizationMember.role != "tenant"
        )
    )).scalars().all()
    return side | {str(e).lower() for e in members if e}


async def clusters_held_by_another_party(
    db: AsyncSession, email: str, cluster_ids: Iterable[str]
) -> Set[str]:
    """Clusters on which some holder is outside the caller's team.

    An unbound row registered by someone else counts as another holder, as it does in
    org_scope._clusters_with_several_orgs. A key whose creator cannot be read counts as another
    holder too, so a Vault hiccup narrows, never widens. A key Vault no longer has holds nothing.
    """
    from app.models.db_models import Organization, OrganizationCluster

    wanted = sorted(set(cluster_ids))
    if not wanted:
        return set()
    rows = (await db.execute(
        select(
            OrganizationCluster.cluster_id,
            OrganizationCluster.organization_id,
            OrganizationCluster.registered_by,
            Organization.api_key_id,
        )
        .outerjoin(Organization, OrganizationCluster.organization_id == Organization.id)
        .where(OrganizationCluster.cluster_id.in_(wanted))
    )).all()
    own_orgs = await _run_orgs(db, email)
    held_elsewhere: Set[str] = set()
    by_key: Dict[str, Set[str]] = {}
    for cluster_id, org_id, registered_by, org_key in rows:
        if org_id is None:
            if (registered_by or "").lower() != (email or "").lower():
                held_elsewhere.add(cluster_id)
        elif org_id in own_orgs:
            continue
        elif org_key:
            by_key.setdefault(org_key, set()).add(cluster_id)
        else:
            held_elsewhere.add(cluster_id)
    if by_key:
        try:
            creators = await run_blocking(
                read_key_creators_blocking, set(by_key),
                timeout=VALIDATE_KEY_TIMEOUT, label="api_key_creators", executor=auth_executor,
            )
        except (BlockingTimeout, BlockingPoolFull):
            raise HTTPException(
                status_code=503, detail="Could not confirm who holds these clusters. Try again."
            ) from None
        side = await side_of(db, email)
        for key_id, clusters in by_key.items():
            creator = creators.get(key_id) or ""
            if creator == DELETED_KEY or creator.lower() in side:
                continue
            held_elsewhere |= clusters
    return held_elsewhere


async def held_with_confirmation(
    db: AsyncSession, email: str, bounded: Iterable[str]
) -> "tuple[Set[str], bool]":
    """clusters_held_by_another_party for a read path, which narrows rather than failing.

    Returns (held, confirmed). When who holds a cluster cannot be confirmed (Vault slow), every
    bounded cluster counts as held, the count-based bound production has always applied, and
    confirmed is False so a listing can say so instead of answering 503.
    """
    wanted = set(bounded)
    try:
        return await clusters_held_by_another_party(db, email, wanted), True
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        logger.warning("Could not confirm who holds %d cluster(s), keeping them bounded", len(wanted))
        return wanted, False


async def held_or_every_bounded(db: AsyncSession, email: str, bounded: Iterable[str]) -> Set[str]:
    """held_with_confirmation without the flag, for a path that only needs the set."""
    held, _confirmed = await held_with_confirmation(db, email, bounded)
    return held
