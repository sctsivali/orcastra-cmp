"""How the background collector's workers share one sweep: a held lease and a relay.

The collector runs in every uvicorn worker. One of them leads: it holds the lease, sweeps the
fleet for the monitoring dashboard and pushes it to its WebSocket clients. The others relay the
dashboard the leader stored to their own clients and never sweep.

important: the lease is held, not taken per cycle. A per-cycle lease that expired after 30 s
elected nobody for the rest of a cycle that lasted the sweep plus 30 s, so another worker took it
mid-cycle and swept again. The leader renews at the start of every cycle and after its sweep; the
lease lapses only when it stops renewing (a crash, or a sweep longer than the lease), and the next
worker to look then takes over.
"""

import logging
import os
import secrets
import socket
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Take the lease, or extend it when this worker already holds it.
_CLAIM = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 1 end
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class CollectorLease:
    """A Redis lease one worker holds while it keeps renewing it."""

    def __init__(self, key: str, seconds: int, redis: Callable[[], Any]) -> None:
        self.key = key
        self.seconds = seconds
        self._redis = redis
        # note: unique per process and per start, so a restarted worker that happens to get the
        # same pid never mistakes a dead predecessor's lease for its own.
        self.token = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"

    def claim(self) -> bool:
        """True when this worker leads: it took the lease or renewed its own.

        True as well when Redis is unavailable, so a single-worker or Redis-down deployment
        keeps collecting rather than going silent: over-collecting beats electing nobody.
        """
        try:
            client = self._redis()
            if client is None:
                return True
            script = client.register_script(_CLAIM)
            return bool(script(keys=[self.key], args=[self.token, self.seconds]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Collector lease unavailable, collecting locally: %s", exc)
            return True

    def release(self) -> None:
        """Hand the lease back on shutdown so the next leader need not wait for it to lapse."""
        try:
            client = self._redis()
            if client is not None:
                client.register_script(_RELEASE)(keys=[self.key], args=[self.token])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Collector lease not released: %s", exc)


class DashboardRelay:
    """Sends dashboards to this worker's WebSocket clients, each collection once."""

    def __init__(self) -> None:
        self._last_pushed: Optional[str] = None

    async def relay(self) -> None:
        """Push the leader's latest stored dashboard, if this worker has clients and it is new."""
        try:
            from app.services.ws_broadcast import get_monitoring_broadcaster

            if get_monitoring_broadcaster().client_count == 0:
                return
            from app.routers.monitoring import cached_dashboard

            await self.push(cached_dashboard())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Monitoring relay skipped: %s", exc)

    async def push(self, result: Any) -> None:
        if result is None:
            return
        try:
            from app.services.ws_broadcast import get_monitoring_broadcaster

            broadcaster = get_monitoring_broadcaster()
            if broadcaster.client_count == 0:
                return
            # note: mode="json" so datetimes go out as ISO 8601. The broadcaster's
            # json.dumps(default=str) wrote "2026-10-07 12:00:00+00:00" with a space,
            # which not every browser's Date.parse accepts, and the page now reads it.
            data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            stamp = data.get("timestamp") if isinstance(data, dict) else None
            if stamp is not None and stamp == self._last_pushed:
                return
            await broadcaster.broadcast(data)
            self._last_pushed = stamp
            logger.debug("Pushed monitoring data to %d WS clients", broadcaster.client_count)
        except Exception as exc:  # noqa: BLE001
            logger.debug("WS broadcast skipped: %s", exc)
