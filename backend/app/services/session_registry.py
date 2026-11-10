"""
Cross-worker share for live console/terminal sessions.

A WebSocket pins to one uvicorn worker but the REST poll lands on any, so each
worker publishes its ``SessionTracker`` sessions to one Redis hash (a JSON snapshot
per field). Readers drop snapshots older than the stale window (``last_seen``), so a
failed delete or a killed worker cannot leave a session "connected"; HEXPIRE is only
cleanup. As in app/core/cache.py, a Redis answer is authoritative and the per-worker
dict is a Redis-down fallback. Snapshots are not encrypted (email, instance name, byte
counts), as session_audit already stores them.

important: Redis runs ``allkeys-lru`` (docker-compose*.yml) and evicts any key under
pressure, TTL or not. One hot hash rewritten every few seconds is never the LRU victim;
per-session keys would be candidates, and would need a ``SCAN`` per poll.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# One hash for every live session across every worker, named orcastra:<domain>:<thing>
# like the app's other Redis keys.
SESSIONS_HASH_KEY = "orcastra:sessions:active"

# Set on a response whose session list came from one worker only, because Redis did
# not answer. Without it a short list looks like a correct one.
SESSION_REGISTRY_DEGRADED_HEADER = "X-Session-Registry-Degraded"

# A Redis fault must not make a 5s-polled endpoint hang: one attempt, then serve the
# local view and try again shortly. This is not the LXD breaker in
# services/lxd/health.py, which is keyed by cluster_id; tripping that for a Redis
# fault would report healthy clusters as offline.
_BREAKER_OPEN_SECONDS = 5.0
_CONNECT_TIMEOUT = 1.0
_SOCKET_TIMEOUT = 1.0


class SessionRegistry:
    """Shared view of live sessions. Degrades to local-only when Redis is away."""

    def __init__(self) -> None:
        self._client: Any = None
        self._breaker_until: float = 0.0
        self._fault_logged: bool = False
        # Which worker published a snapshot; explains duplicate rows when the hash is
        # read by hand.
        self.worker_id: str = uuid.uuid4().hex[:8]

    # ---- lifecycle -------------------------------------------------------

    async def connect(self, redis_url: str) -> None:
        """
        Build the async client. Call from lifespan startup only.

        important: redis.asyncio binds its connection pool to the running loop
        lazily. A client built at import time attaches to whatever loop exists and
        then fails intermittently with cross-loop errors.
        """
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.warning("redis.asyncio unavailable - session registry stays local-only")
            return

        try:
            self._client = aioredis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=_CONNECT_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
                # a retry here silently doubles the latency budget of an endpoint
                # that is polled every 5 seconds; the breaker is the retry.
                retry_on_timeout=False,
            )
            await self._client.ping()
            logger.info("Session registry connected to Redis (worker %s)", self.worker_id)
        except Exception as exc:
            logger.warning("Session registry could not reach Redis (%s) - local-only", exc)
            self._client = None

    async def aclose(self) -> None:
        """Release the pool. redis-py >=5 deprecated close() in favour of aclose()."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ---- breaker ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _usable(self) -> bool:
        return self._client is not None and time.time() >= self._breaker_until

    def _trip(self, exc: Exception) -> None:
        self._breaker_until = time.time() + _BREAKER_OPEN_SECONDS
        if not self._fault_logged:
            # once per trip, not once per request: a Redis outage under a 5s poll
            # would otherwise write a log line per operator per cycle.
            logger.warning("Session registry degraded, serving local view only: %s", exc)
            self._fault_logged = True

    def _recovered(self) -> None:
        if self._fault_logged:
            logger.info("Session registry recovered")
            self._fault_logged = False

    # ---- writes ----------------------------------------------------------

    async def publish(self, snapshots: List[Dict[str, Any]], ttl_seconds: int) -> None:
        """Write this worker's live sessions. No-op when there is nothing to say."""
        if not snapshots or not self._usable():
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            fields = {s["session_id"]: json.dumps(s) for s in snapshots}
            pipe.hset(SESSIONS_HASH_KEY, mapping=fields)
            # Per-field expiry cleans up after a worker that dies without removing
            # its rows. The guard covers a redis-py without hexpire (HEXPIRE needs
            # Redis 7.4); the reader's staleness filter carries correctness.
            try:
                pipe.hexpire(SESSIONS_HASH_KEY, ttl_seconds, *fields.keys())
            except AttributeError:
                pass
            # keep the hash itself from outliving the backend that wrote it
            pipe.expire(SESSIONS_HASH_KEY, max(ttl_seconds * 6, 60))
            await pipe.execute()
            self._recovered()
        except Exception as exc:
            self._trip(exc)

    async def remove(self, session_id: str) -> None:
        """Drop a session the moment it ends, rather than waiting out its TTL."""
        if not session_id or not self._usable():
            return
        try:
            await self._client.hdel(SESSIONS_HASH_KEY, session_id)
            self._recovered()
        except Exception as exc:
            self._trip(exc)

    # ---- reads -----------------------------------------------------------

    async def fetch(self, stale_seconds: int) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Every worker's live sessions, minus anything that stopped beating.

        Returns (snapshots, degraded). ``degraded`` true means Redis did not answer
        and the caller holds only its own worker's view, which the UI is then told.
        """
        if not self._usable():
            return [], True
        try:
            raw = await self._client.hgetall(SESSIONS_HASH_KEY)
            self._recovered()
        except Exception as exc:
            self._trip(exc)
            return [], True

        cutoff = time.time() - stale_seconds
        snapshots: List[Dict[str, Any]] = []
        for value in (raw or {}).values():
            try:
                snap = json.loads(value)
            except (ValueError, TypeError):
                continue
            # a publisher that stopped beating is gone, whether or not HEXPIRE ran
            if float(snap.get("last_seen") or 0) < cutoff:
                continue
            snapshots.append(snap)
        return snapshots, False


_registry: Optional[SessionRegistry] = None


def get_session_registry() -> SessionRegistry:
    """Process-wide registry handle. The Redis client inside it is set in lifespan."""
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry
