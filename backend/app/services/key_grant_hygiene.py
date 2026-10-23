"""Keep API key grants naming only clusters that are registered.

Grants are stored as typed. A cluster registered again under a new id (on dev `nuc-2` became
`nuc2`, `blitz01` became `blitz001`) left its old id in key grants, where it authorises nothing
but is reported as held, polled by the integration that owns the key, and looked up in Vault on
every poll. Deregistration strips the id (`routers/cluster_registration.py`), grant writes refuse
a new unregistered id (`routers/integrations.py`), and
`backend/scripts/repair_key_grant_cluster_ids.py` repairs what earlier deregistrations left.

important: never a remap. A cluster registered again is a new trust identity, possibly owned by
another organization; granting it is a decision for the key's owner, not a cleanup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

Stale = Callable[[str], bool]


@dataclass(frozen=True)
class GrantCleanup:
    key_id: str
    app_name: str
    drop: List[str]
    # An add-on installation owns its grant (services/add_on_credential_edits.py): reported only.
    add_on: bool
    # Dropping would leave the key with no grant, which hides it from every partner's key list:
    # reported only, so its owner decides.
    left_empty: bool

    @property
    def writable(self) -> bool:
        return bool(self.drop) and not self.add_on and not self.left_empty


def _grants(metadata: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [g for g in (metadata.get("cluster_permissions") or []) if isinstance(g, dict)]


def plan_cleanup(key_id: str, metadata: Mapping[str, Any], stale: Stale) -> Optional[GrantCleanup]:
    """What dropping the stale ids from one key's grant would change, or None for nothing."""
    from app.services.add_on_credential_edits import is_add_on_credential

    grants = _grants(metadata)
    drop = sorted({str(g["cluster_id"]) for g in grants if g.get("cluster_id") and stale(str(g["cluster_id"]))})
    if not drop:
        return None
    kept = [g for g in grants if g.get("cluster_id") not in drop]
    return GrantCleanup(
        key_id=key_id,
        app_name=str(metadata.get("app_name") or metadata.get("name") or ""),
        drop=drop,
        add_on=is_add_on_credential(metadata),
        left_empty=not kept,
    )


def list_key_ids_strict(vault_client: Any) -> List[str]:
    """Every API key id in Vault; raises instead of answering an empty list on a failed LIST."""
    from app.routers.integrations import VAULT_INTEGRATIONS_PATH

    response = vault_client.session.request(
        "LIST",
        f"{vault_client.vault_addr}/v1/secret/metadata/{VAULT_INTEGRATIONS_PATH}/api_keys",
        headers=vault_client.headers,
        timeout=(2, 10),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return [str(k).rstrip("/") for k in response.json().get("data", {}).get("keys", [])]


def read_key_metadata(vault_client: Any, key_id: str) -> Dict[str, Any]:
    """Uncached: every write here is built from Vault itself, never from the Redis mirror."""
    from app.routers.integrations import get_api_key_from_vault

    metadata, _ = get_api_key_from_vault(vault_client, key_id, use_cache=False)
    return metadata


async def apply_cleanup(db: Any, vault_client: Any, key_id: str, stale: Stale, reason: str) -> List[str]:
    """Drop the stale ids from one key under its ownership lock; returns the ids dropped."""
    from app.routers.integrations import VAULT_INTEGRATIONS_PATH, evict_api_key_cache
    from app.core.cache import get_cache_manager
    from app.services.activity_logger import get_activity_logger
    from app.services.integration_key_ownership import serialize_key_ownership

    async with serialize_key_ownership(db, key_id):
        metadata = await asyncio.to_thread(read_key_metadata, vault_client, key_id)
        cleanup = plan_cleanup(key_id, metadata, stale)
        if cleanup is None or not cleanup.writable:
            await db.commit()
            return []
        metadata["cluster_permissions"] = [g for g in _grants(metadata) if g.get("cluster_id") not in cleanup.drop]
        await asyncio.to_thread(
            vault_client.vault_kv_put,
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata",
            metadata,
        )
        await db.commit()
    evict_api_key_cache(key_id)
    get_cache_manager().delete_pattern("api_responses", "integrations:api_keys:list*")
    get_activity_logger().log_activity(
        action="api_key.grant.clusters_removed",
        target_type="api_key",
        target_id=key_id,
        result="success",
        details={"clusters": cleanup.drop, "reason": reason},
    )
    logger.info("Removed %s from the grant of API key %s (%s)", cleanup.drop, key_id, reason)
    return cleanup.drop


async def strip_cluster_from_key_grants(
    cluster_id: str, session_maker: Optional[Callable[[], Any]] = None
) -> int:
    """After a deregistration: drop the id from every key grant that can be rewritten.

    Returns how many keys changed. A failure is logged and counted out, never raised: the cluster
    is already deregistered, and the repair script covers whatever this could not reach. Each key
    is rewritten in a session of its own, so nothing here commits the caller's transaction.
    """
    from app.routers.integrations import get_vault_integrations_client

    if session_maker is None:
        from app.core.database import async_session_maker as session_maker
    try:
        vault_client = get_vault_integrations_client()
    except Exception:  # noqa: BLE001 - Vault not configured: there are no key grants to clean
        return 0
    try:
        key_ids = await asyncio.to_thread(list_key_ids_strict, vault_client)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("Could not list API keys to drop deregistered cluster %s: %s", cluster_id, exc)
        return 0
    changed = 0
    for key_id in key_ids:
        try:
            async with session_maker() as db:
                dropped = await apply_cleanup(
                    db, vault_client, key_id, lambda c: c == cluster_id, reason=f"cluster {cluster_id} deregistered"
                )
        except Exception as exc:  # noqa: BLE001 - one key must not stop the others
            logger.warning("Could not drop deregistered cluster %s from API key %s: %s", cluster_id, key_id, exc)
            continue
        changed += bool(dropped)
    return changed
