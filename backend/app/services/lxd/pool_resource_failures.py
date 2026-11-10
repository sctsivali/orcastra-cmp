"""Remember, for a minute, a storage pool whose resources read fails.

A broken pool does not fail fast: a ZFS pool whose dataset is gone makes LXD answer
`GET /1.0/storage-pools/<pool>/resources` with an error after about 3 s, every time, and the
fleet readers (node status, the regions overview, the pool resources list) ask for it on every
build.

A failed read is remembered per worker for FAILURE_TTL_S, and the next read of the same pool
fails at once with the same consequence a real failure has (the pool is left out and the reading
is marked degraded), so each reader keeps its own handling. A successful read is never cached:
pool usage stays current, and any success clears the memory.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Tuple, TypeVar

T = TypeVar("T")

FAILURE_TTL_S = 60.0
_MAX_ENTRIES = 512

_lock = threading.Lock()
# (cluster_id, pool_name) -> (failed_at monotonic, exception class name)
_failed: Dict[Tuple[str, str], Tuple[float, str]] = {}


class PoolResourcesUnavailable(Exception):
    """The pool's resources read failed within FAILURE_TTL_S; not asked again yet."""


def read(cluster_id: str, pool_name: str, fn: Callable[[], T]) -> T:
    """`fn()`, unless the same pool failed within FAILURE_TTL_S. Raises what `fn` raises."""
    key = (cluster_id, pool_name)
    now = time.monotonic()
    with _lock:
        failure = _failed.get(key)
        if failure is not None and now - failure[0] < FAILURE_TTL_S:
            raise PoolResourcesUnavailable(
                f"resources of pool {pool_name} failed {round(now - failure[0])} s ago ({failure[1]})"
            )
    try:
        result = fn()
    except Exception as exc:
        with _lock:
            if len(_failed) >= _MAX_ENTRIES:
                _failed.pop(next(iter(_failed)))
            _failed[key] = (time.monotonic(), type(exc).__name__)
        raise
    with _lock:
        _failed.pop(key, None)
    return result


def forget(cluster_id: str) -> None:
    """Drop what is remembered for a cluster, after a recovery or a change to its pools."""
    with _lock:
        for key in [k for k in _failed if k[0] == cluster_id]:
            _failed.pop(key, None)
