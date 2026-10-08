"""Each storage pool's driver on a cluster, read once and shared by every worker.

The instance dialog needs a container's root pool driver to know whether its disk I/O can be
measured at all. One storage-pools?recursion=1 read answers every pool of the cluster, and a
pool keeps its driver for life, so the answer is kept for ten minutes; a failed read is kept
for one, so a cluster that cannot answer is not asked again on every dialog poll.
"""

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "lxd_resources"
DRIVERS_TTL_SECONDS = 600
FAILURE_TTL_SECONDS = 60
MIN_BUDGET_SECONDS = 2.0


def _key(cluster: str) -> str:
    return f"pool_drivers:v1:{quote(cluster, safe='')}"


def _cache() -> Any:
    from app.core.cache import get_cache_manager

    return get_cache_manager()


def pool_driver(client: Any, cluster: str, pool: str, *, deadline: Optional[float]) -> Optional[str]:
    """The driver of `pool` on `cluster`, or None when it is unknown or cannot be read in time.

    `deadline` is a time.monotonic() value; without one the cache is the only source.
    """
    cache = _cache()
    try:
        entry = cache.get(CACHE_NAMESPACE, _key(cluster))
    except Exception:  # noqa: BLE001 - a cache miss costs one read
        entry = None
    if isinstance(entry, dict):
        drivers = entry.get("drivers")
        return drivers.get(pool) if isinstance(drivers, dict) else None
    if deadline is None or deadline - time.monotonic() < MIN_BUDGET_SECONDS:
        return None
    read_timeout = max(1.0, min(4.0, deadline - time.monotonic() - 1.0))
    try:
        rows = client.api["storage-pools"].get(params={"recursion": 1}, timeout=(2, read_timeout)).json()
        drivers: Dict[str, str] = {
            str(row["name"]): str(row.get("driver") or "")
            for row in rows.get("metadata") or []
            if isinstance(row, dict) and row.get("name")
        }
    except Exception as exc:  # noqa: BLE001 - disk I/O then reports the driver as unknown
        logger.debug("Storage pool drivers unreadable on %s: %s", cluster, str(exc)[:200])
        _store(cache, cluster, {"failed": True}, FAILURE_TTL_SECONDS)
        return None
    _store(cache, cluster, {"drivers": drivers}, DRIVERS_TTL_SECONDS)
    return drivers.get(pool)


def _store(cache: Any, cluster: str, value: Dict[str, Any], ttl: int) -> None:
    try:
        cache.set(CACHE_NAMESPACE, _key(cluster), value, ttl=ttl)
    except Exception:  # noqa: BLE001 - without the entry the next poll reads again
        pass
