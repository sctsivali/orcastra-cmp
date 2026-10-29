"""Where the last reads are kept: one Redis record per CVE, shared by every worker, or this process
when Redis is down.

A record says when Ubuntu was last tried and last read for that CVE, why the last try failed, and
the last record that parsed. Only the kept fields are stored (record.to_dict), never the raw body.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "orcastra:kernel_cve_feed:"
COOLDOWN_KEY = f"{KEY_PREFIX}check_now"
# Longer than a record is believed (3 days), so a stale one still says when it was read.
STATE_TTL_SECONDS = 30 * 86400


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


def state_key(cve_id: str) -> str:
    return f"{KEY_PREFIX}{cve_id}"


class KernelCveFeedStore:
    def __init__(self, redis: Callable[[], Any] = _shared_redis) -> None:
        self._redis = redis
        self._local: Dict[str, Dict[str, Any]] = {}
        self._local_cooldown_until = 0.0
        self._lock = threading.Lock()

    def _client(self) -> Any:
        try:
            return self._redis()
        except Exception:  # noqa: BLE001
            return None

    def read_many(self, cve_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        ids = list(cve_ids)
        client = self._client()
        if client is not None and ids:
            try:
                raws = client.mget([state_key(cve_id) for cve_id in ids])
                states = {}
                for cve_id, raw in zip(ids, raws):
                    state = json.loads(raw) if raw else {}
                    states[cve_id] = state if isinstance(state, dict) else {}
                return states
            except Exception as exc:  # noqa: BLE001 - fall back to this process's copy
                logger.debug("Kernel CVE feed state unavailable in Redis: %s", type(exc).__name__)
        with self._lock:
            return {cve_id: dict(self._local.get(cve_id) or {}) for cve_id in ids}

    def read(self, cve_id: str) -> Dict[str, Any]:
        return self.read_many([cve_id])[cve_id]

    def write(self, cve_id: str, state: Dict[str, Any]) -> None:
        with self._lock:
            self._local[cve_id] = dict(state)
        client = self._client()
        if client is None:
            return
        try:
            client.set(state_key(cve_id), json.dumps(state), ex=STATE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Kernel CVE feed state not shared: %s", type(exc).__name__)

    def take_cooldown(self, seconds: int) -> Optional[int]:
        """None when a manual check may run now; otherwise the seconds left until it may."""
        client = self._client()
        if client is not None:
            try:
                if client.set(COOLDOWN_KEY, "1", nx=True, ex=seconds):
                    return None
                return max(1, int(client.ttl(COOLDOWN_KEY)))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            now = time.monotonic()
            if now >= self._local_cooldown_until:
                self._local_cooldown_until = now + seconds
                return None
            return max(1, int(self._local_cooldown_until - now))


_store: Optional[KernelCveFeedStore] = None


def get_store() -> KernelCveFeedStore:
    global _store
    if _store is None:
        _store = KernelCveFeedStore()
    return _store
