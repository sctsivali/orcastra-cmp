"""
Cross-worker share for live console/terminal sessions.

Why this exists: ``SessionTracker`` keeps sessions in a module-global dict, which is
per PROCESS. The backend runs several uvicorn workers (WEB_CONCURRENCY, default 4),
a WebSocket pins to exactly one of them, and the REST poll that lists sessions is
spread across all of them. Most polls therefore landed on a worker that had never
heard of the session and answered "none", so the Sessions page flapped and the
console resource bar froze on its last good value.

Shape: ONE Redis hash, field per session, value a JSON snapshot. Not one key per
session, for two reasons.

1. ``SCAN MATCH`` walks the whole keyspace server-side, and this Redis also holds
   the response cache. Paying O(keyspace) every poll to find a handful of keys is
   the wrong trade.
2. important: Redis runs ``--maxmemory 256mb --maxmemory-policy allkeys-lru``
   (docker-compose*.yml). allkeys-lru evicts ANY key under pressure, TTL or not, so
   "it has a TTL" is not protection and a separate DB index would not help either.
   N per-session keys are N independent eviction candidates, and losing one brings
   the flapping back while looking like a Redis fault. One hot key rewritten every
   couple of seconds is never the LRU victim. Lowering maxmemory now has a
   non-obvious blast radius here.

Freshness is reader-side. Each snapshot carries ``last_seen``; a reader drops
anything older than the stale window. Per-field TTL (HEXPIRE) is garbage collection
on top, so correctness does not depend on it existing, and a delete that fails
during a Redis blip cannot strand a session as permanently "connected". That is
also what fixes the pre-existing leak where a SIGKILLed worker's sessions stayed
"connected" forever, because the old reaper only ever removed rows already marked
disconnected.

Invariant, mirrored deliberately from app/core/cache.py:277-284 so the rule reads
the same everywhere: Redis answering AT ALL is authoritative. The per-worker dict is
a Redis-is-down fallback only, never a miss path.

Snapshots are not encrypted. cache.py would not encrypt this either: its sensitive
namespaces are vault/credentials only. The payload is an email, an instance name and
byte counts, the durable copy of which already sits unencrypted in Postgres via
session_audit, and encrypting would double the payload, put a cipher op on the event
loop every beat, and make redis-cli debugging impossible.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# One hash for every live session across every worker. Namespace convention is
# orcastra:<domain>:<thing>, shared with the rest of the app's Redis keys.
SESSIONS_HASH_KEY = "orcastra:sessions:active"

# Set on a response whose session list came from one worker only, because Redis did
# not answer. A short list is otherwise indistinguishable from a correct one, which is
# exactly how the original bug stayed invisible for so long.
SESSION_REGISTRY_DEGRADED_HEADER = "X-Session-Registry-Degraded"

# A Redis fault must not make a 5s-polled endpoint hang. One attempt, then serve the
# local view and try again shortly. Deliberately NOT the LXD breaker in
# services/lxd/health.py: that one is keyed by cluster_id, and tripping it for a
# Redis fault would report healthy clusters as offline across the whole UI.
_BREAKER_OPEN_SECONDS = 5.0
_CONNECT_TIMEOUT = 1.0
_SOCKET_TIMEOUT = 1.0


class SessionRegistry:
    """Shared view of live sessions. Degrades to local-only when Redis is away."""

    def __init__(self) -> None:
        self._client: Any = None
        self._breaker_until: float = 0.0
        self._fault_logged: bool = False
        # identifies which worker published a snapshot; useful when reading the hash
        # by hand during an incident, and it is what makes duplicate rows explicable.
        self.worker_id: str = uuid.uuid4().hex[:8]

    # ---- lifecycle -------------------------------------------------------

    async def connect(self, redis_url: str) -> None:
        """
        Build the async client. Lifespan startup ONLY.

        important: redis.asyncio binds its connection pool to the running loop
        lazily. A client built at import time gets attached to whatever loop happens
        to exist and then fails intermittently with cross-loop errors, so this must
        never be called from module scope.
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
            # per-field expiry is the backstop for a worker that dies without
            # cleaning up. Missing on Redis < 7.4, hence the guarded call: the
            # reader's staleness filter is what actually carries correctness.
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
        and the caller is holding only its own worker's view, which is the bug this
        module exists to fix - so the UI is told rather than silently misled.
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
