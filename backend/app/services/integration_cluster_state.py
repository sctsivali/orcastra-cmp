"""What an API key's cluster listing says about each cluster beyond its registration.

`GET /integrations/clusters`, and MCP `orcastra_list_clusters` which runs it, promised a live status
and returned none, so an agent could not tell a cluster worth calling from one that would only
time out. Each row now carries the dashboard's availability fields (`status`, `reason_code`,
`reason`, `checked_at`, the names `AvailableCluster` uses) and the access the presented key holds
on that cluster.

important: cache-only by design. The state comes from what the dashboard already paid for (the
shared availability cache, else a warm `/nodes` body) and from this worker's breaker, whichever is
newer. Nothing here probes a cluster, so a key holder cannot turn a listing into a fleet-wide
fan-out, and a cluster nothing has checked recently reads as null rather than as a guess.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.api_key_permissions import granted_level_name
from app.core.org_scope import unscoped_cache_key
from app.core.type_utils import normalize_cluster_type
from app.models.schemas import ClusterAvailability
from app.services.lxd import status as lxd_status

logger = logging.getLogger(__name__)

__all__ = ["server_type_name", "stamp_cluster_rows"]

_UNKNOWN: Dict[str, Any] = {"status": None, "reason_code": None, "reason": None, "checked_at": None}


def server_type_name(stored: Optional[str]) -> str:
    """The lowercase family name this surface has always sent, ex; `lxd`, `incus`, `microcloud`."""
    return normalize_cluster_type(stored).lower()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _newer(
    cached: Optional[ClusterAvailability], tripped: Optional[ClusterAvailability]
) -> Optional[ClusterAvailability]:
    # note: the breaker is per worker and the cache is shared. A cache row written after this
    # worker tripped means another worker reached the cluster since, so the newer one wins
    # either way rather than the breaker always overriding.
    if cached is None or tripped is None:
        return cached or tripped
    return tripped if _aware(tripped.checked_at) > _aware(cached.checked_at) else cached


def _access(api_key: Dict[str, Any], cluster_id: str) -> Optional[Dict[str, Any]]:
    for grant in api_key.get("cluster_permissions", []):
        if isinstance(grant, dict) and grant.get("cluster_id") == cluster_id:
            return {
                "level": granted_level_name(grant.get("permissions", ["read"])),
                # Absent means ["default"], the ClusterPermission default every check applies.
                "projects": list(grant.get("projects", ["default"])),
            }
    return None


def _known_rows() -> Dict[str, ClusterAvailability]:
    try:
        peeked = lxd_status.peek_availability(unscoped_cache_key)
    except Exception as exc:  # noqa: BLE001 - state is an enrichment, never the reason a read fails
        logger.warning("Availability cache unreadable for the key's cluster list: %s", exc)
        return {}
    return {row.name: row for row in peeked.clusters} if peeked else {}


def stamp_cluster_rows(api_key: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    """Add state and access to each row in place. Rows carry the cluster id under `id`."""
    known = _known_rows()
    for row in rows:
        cluster_id = row["id"]
        state = _newer(known.get(cluster_id), lxd_status.breaker_availability(cluster_id))
        if state is None:
            row.update(_UNKNOWN)
        else:
            row.update({
                "status": state.status.value,
                "reason_code": state.reason_code.value,
                "reason": state.reason,
                "checked_at": _aware(state.checked_at).isoformat(),
            })
            if state.node_type:
                # A live answer outranks the registration-time guess Vault stores.
                row["server_type"] = server_type_name(state.node_type)
        row["access"] = _access(api_key, cluster_id)
