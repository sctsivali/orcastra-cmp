"""Where the last check is kept: Redis, shared by every worker, or this process when Redis is down.

One record: when the feed was last tried and last read, from where, why the last try failed, and
the last manifest that parsed. The manifest is stored as the parsed releases, never the raw body.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional

from app.services.release_check.manifest import Manifest, Release

logger = logging.getLogger(__name__)

STATE_KEY = "orcastra:release_check:state"
COOLDOWN_KEY = "orcastra:release_check:check_now"
# Longer than the last good manifest is honoured, so a stale record still says when it was read.
STATE_TTL_SECONDS = 30 * 86400


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


def manifest_to_dict(manifest: Manifest) -> Dict[str, Any]:
    return {"generated_at": manifest.generated_at, "releases": [asdict(r) for r in manifest.releases]}


def manifest_from_dict(data: Optional[Dict[str, Any]]) -> Optional[Manifest]:
    if not data or not data.get("releases"):
        return None
    try:
        return Manifest(
            releases=tuple(Release(**release) for release in data["releases"]),
            generated_at=data.get("generated_at"),
        )
    except TypeError:
        return None


class ReleaseCheckStore:
    def __init__(self, redis: Callable[[], Any] = _shared_redis) -> None:
        self._redis = redis
        self._local: Dict[str, Any] = {}
        self._local_cooldown_until = 0.0
        self._lock = threading.Lock()

    def _client(self) -> Any:
        try:
            return self._redis()
        except Exception:  # noqa: BLE001
            return None

    def read(self) -> Dict[str, Any]:
        client = self._client()
        if client is not None:
            try:
                raw = client.get(STATE_KEY)
                if raw:
                    return json.loads(raw)
            except Exception as exc:  # noqa: BLE001 - fall back to this process's copy
                logger.debug("Release check state unavailable in Redis: %s", type(exc).__name__)
        with self._lock:
            return dict(self._local)

    def write(self, state: Dict[str, Any]) -> None:
        with self._lock:
            self._local = dict(state)
        client = self._client()
        if client is None:
            return
        try:
            client.set(STATE_KEY, json.dumps(state), ex=STATE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Release check state not shared: %s", type(exc).__name__)

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


_store: Optional[ReleaseCheckStore] = None


def get_store() -> ReleaseCheckStore:
    global _store
    if _store is None:
        _store = ReleaseCheckStore()
    return _store
