"""Who holds a cluster, decided by party rather than by how many organizations are bound to it.

A cluster carries one binding per organization. Counting bindings calls every partner cluster
with a sale "shared", because provisioning binds each buyer organization to the partner's
cluster. The questions "may this partner grant, manage or reach the whole of this cluster" need
the other answer: is any binding held by someone who is not on the caller's side. A buyer
organization belongs to the side of whoever minted the integration key that created it, and a
key's minter belongs to the side of everyone in their organizations.
"""

import logging
from typing import Dict, Iterable, Set

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.org_scope import get_org_ids_for_email
from app.services.lxd.proxy import VALIDATE_KEY_TIMEOUT, auth_executor

logger = logging.getLogger(__name__)

API_KEYS_VAULT_PATH = "secret/data/integrations/api_keys"


def read_key_creators_blocking(key_ids: Set[str]) -> Dict[str, str]:
    """created_by of each key id. Runs on auth_executor. An unreadable key maps to ''."""
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
        except Exception:
            metadata = {}
        creators[key_id] = str((metadata or {}).get("created_by") or "")
    return creators


async def side_of(db: AsyncSession, email: str) -> Set[str]:
    """Lowercased emails on the caller's side: themselves, and everyone in or creating their orgs."""
    from app.models.db_models import Organization, OrganizationMember

    side = {email.lower()} if email else set()
    own = await get_org_ids_for_email(email, db)
    if not own:
        return side
    members = (await db.execute(
        select(OrganizationMember.user_email).where(OrganizationMember.organization_id.in_(own))
    )).scalars().all()
    creators = (await db.execute(
        select(Organization.created_by).where(Organization.id.in_(own))
    )).scalars().all()
    return side | {str(e).lower() for e in [*members, *creators] if e}


async def clusters_held_by_another_party(
    db: AsyncSession, email: str, cluster_ids: Iterable[str]
) -> Set[str]:
    """Clusters on which some holder is neither on the caller's side nor theirs by key.

    An unbound row registered by someone else counts as another holder, as it does in
    org_scope._clusters_with_several_orgs. A key whose creator cannot be read counts as another
    holder too, so a Vault hiccup narrows, never widens.
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
    own_orgs = await get_org_ids_for_email(email, db)
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
            if (creators.get(key_id) or "").lower() not in side:
                held_elsewhere |= clusters
    return held_elsewhere
