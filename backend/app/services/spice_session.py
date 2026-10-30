"""One LXD console operation for every channel of one SPICE display, across workers.

spice-html5 opens a WebSocket per SPICE channel (main, display, inputs, cursor), and each one
used to create its own LXD console operation: four operations, four control and four data
sockets, four tracked sessions and four rounds of cluster setup per console. LXD serves a VGA
operation's data secret to as many sockets as connect with it (that is how LXD-UI runs
spice-html5 on one operation), so one operation is enough.

The channels of one display reach different uvicorn workers, so they meet here, in Redis:

- The VGA ticket mint stamps all of a display's tickets with one random group id.
- The first channel to redeem (the main channel, which spice-html5 opens alone) claims the group,
  creates the operation, keeps its control socket, and publishes the operation's data URL.
- Every later channel waits for that record and opens only a data socket.
- When the owner leaves it retracts the record and closes the control socket; LXD then ends the
  operation and with it the other channels.
- Members count their bytes under the group so the owner's one tracked session reports the
  whole display.

important: the record carries the data URL with the operation's secret, a bearer credential for
the guest's screen and keyboard. It is stored only Fernet-encrypted under a key derived from
SECRET_KEY, keyed by a digest of the group id, and lives no longer than the owner keeps renewing
it. The group id itself only ever travels inside signed tickets.

important: Fernet authenticates the bytes, not where they sit. A sealed record copied under
another group's key would decrypt there and hand a stranger's channels the victim's display, so
the record carries its own key and is refused anywhere else, and a joining channel also checks
the record's instance and operation URL against its own (console_proxy).

note: anything that fails here falls back to the old behaviour, an operation per channel, rather
than refusing the console.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

KEY_PREFIX = "orcastra:spice"
RECORD_TTL_SECONDS = 60
RENEW_EVERY_SECONDS = 15.0
ATTACH_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.05
# A channel joins within its ticket's 30 s; a record older than this is never what it wants.
_SEAL_TTL_SECONDS = 120
_KEY_CONTEXT = b"orcastra:spice-session:v1"
_CONNECT_TIMEOUT = 2.0
_SOCKET_TIMEOUT = 2.0


def new_group() -> str:
    return secrets.token_urlsafe(18)


def _keys(group: str) -> Tuple[str, str, str]:
    digest = hashlib.sha256(group.encode("utf-8")).hexdigest()[:40]
    base = f"{KEY_PREFIX}:{digest}"
    return f"{base}:owner", f"{base}:op", f"{base}:bytes"


class SpiceSessionStore:
    """Where the channels of one display find its operation. Redis when it answers, else local."""

    def __init__(self) -> None:
        self._client: Any = None
        self._fernet: Any = None
        self._local: Dict[str, Tuple[float, Any]] = {}

    async def connect(self, redis_url: str) -> None:
        """Lifespan only: redis.asyncio binds its pool to the running loop."""
        try:
            import redis.asyncio as aioredis

            client = aioredis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=_CONNECT_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
                retry_on_timeout=False,
            )
            await client.ping()
            self._client = client
        except Exception as exc:  # noqa: BLE001 - one process can still share locally
            logger.warning("SPICE sessions are shared within this worker only (Redis not reachable): %s", exc)
            self._client = None

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    # ── encryption ───────────────────────────────────────────────────────────────────────────

    def _cipher(self) -> Any:
        if self._fernet is None:
            from cryptography.fernet import Fernet

            from app.core.config import get_settings

            secret = get_settings().secret_key or ""
            if not secret:
                raise RuntimeError("SECRET_KEY is not set")
            raw = hmac.new(secret.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(raw))
        return self._fernet

    def _seal(self, op_key: str, record: Dict[str, Any]) -> str:
        body = json.dumps({"k": op_key, **record}, separators=(",", ":"))
        return self._cipher().encrypt(body.encode("utf-8")).decode("ascii")

    def _open(self, sealed: Optional[str], op_key: str) -> Optional[Dict[str, Any]]:
        if not sealed:
            return None
        try:
            from cryptography.fernet import InvalidToken

            try:
                record = json.loads(self._cipher().decrypt(sealed.encode("ascii"), ttl=_SEAL_TTL_SECONDS))
            except InvalidToken:
                logger.warning("A SPICE session record failed its integrity check and was ignored")
                return None
        except Exception as exc:  # noqa: BLE001 - an unreadable record attaches nothing
            logger.warning("A SPICE session record could not be read: %s", type(exc).__name__)
            return None
        if not isinstance(record, dict) or record.pop("k", None) != op_key:
            logger.warning("A SPICE session record sealed for another display was ignored")
            return None
        return record

    # ── local fallback (a backend run without Redis is one process) ──────────────────────────

    def _local_get(self, key: str) -> Any:
        entry = self._local.get(key)
        if entry is None or entry[0] <= time.monotonic():
            self._local.pop(key, None)
            return None
        return entry[1]

    def _local_set(self, key: str, value: Any, ttl: float) -> None:
        now = time.monotonic()
        # Records nobody reads again (a failure, a member's last bytes) expire here too.
        for stale in [k for k, (expires, _) in self._local.items() if expires <= now]:
            del self._local[stale]
        self._local[key] = (now + ttl, value)

    # ── the protocol ─────────────────────────────────────────────────────────────────────────

    async def claim(self, group: str) -> bool:
        """Whether this channel owns the group (creates the operation). Raises on store failure."""
        owner_key, _, _ = _keys(group)
        token = f"{os.getpid()}:{secrets.token_hex(4)}"
        if self._client is not None:
            return bool(await self._client.set(owner_key, token, nx=True, ex=RECORD_TTL_SECONDS))
        if self._local_get(owner_key) is not None:
            return False
        self._local_set(owner_key, token, RECORD_TTL_SECONDS)
        return True

    async def publish(self, group: str, record: Dict[str, Any]) -> None:
        _, op_key, _ = _keys(group)
        sealed = self._seal(op_key, record)
        if self._client is not None:
            await self._client.set(op_key, sealed, ex=RECORD_TTL_SECONDS)
        else:
            self._local_set(op_key, sealed, RECORD_TTL_SECONDS)

    async def fail(self, group: str, message: str) -> None:
        """Tell waiting channels the operation will not come, so they stop waiting at once."""
        try:
            await self.publish(group, {"failed": message[:200]})
        except Exception as exc:  # noqa: BLE001 - waiters time out on their own
            logger.debug("Could not publish a SPICE session failure: %s", type(exc).__name__)

    async def wait_for(self, group: str, timeout: float = ATTACH_WAIT_SECONDS) -> Optional[Dict[str, Any]]:
        """The owner's record, or None when it did not appear within ``timeout`` or was refused.

        note: a record that is there but refused (another display's, a different SECRET_KEY, one
        sealed by an older build during a deploy) ends the wait at once and is logged once; the
        channel then opens its own operation instead of polling it for the whole window.
        """
        _, op_key, _ = _keys(group)
        deadline = time.monotonic() + timeout
        while True:
            if self._client is not None:
                sealed = await self._client.get(op_key)
            else:
                sealed = self._local_get(op_key)
            if sealed:
                return self._open(sealed, op_key)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_POLL_SECONDS)

    async def renew(self, group: str) -> None:
        owner_key, op_key, bytes_key = _keys(group)
        if self._client is not None:
            pipe = self._client.pipeline(transaction=False)
            pipe.expire(owner_key, RECORD_TTL_SECONDS)
            pipe.expire(op_key, RECORD_TTL_SECONDS)
            pipe.expire(bytes_key, RECORD_TTL_SECONDS)
            await pipe.execute()
            return
        for key in (owner_key, op_key, bytes_key):
            value = self._local_get(key)
            if value is not None:
                self._local_set(key, value, RECORD_TTL_SECONDS)

    async def add_bytes(self, group: str, downloaded: int, uploaded: int) -> None:
        if not downloaded and not uploaded:
            return
        _, _, bytes_key = _keys(group)
        if self._client is not None:
            pipe = self._client.pipeline(transaction=False)
            pipe.hincrby(bytes_key, "down", int(downloaded))
            pipe.hincrby(bytes_key, "up", int(uploaded))
            pipe.expire(bytes_key, RECORD_TTL_SECONDS)
            await pipe.execute()
            return
        current = self._local_get(bytes_key) or {"down": 0, "up": 0}
        current = {"down": current["down"] + int(downloaded), "up": current["up"] + int(uploaded)}
        self._local_set(bytes_key, current, RECORD_TTL_SECONDS)

    async def member_bytes(self, group: str) -> Tuple[int, int]:
        """Total bytes the other channels of the display moved so far."""
        _, _, bytes_key = _keys(group)
        if self._client is not None:
            values = await self._client.hgetall(bytes_key)
        else:
            values = self._local_get(bytes_key) or {}
        return int(values.get("down", 0) or 0), int(values.get("up", 0) or 0)

    async def retract(self, group: str) -> None:
        keys = _keys(group)
        if self._client is not None:
            await self._client.delete(*keys)
        else:
            for key in keys:
                self._local.pop(key, None)


_store: Optional[SpiceSessionStore] = None


def get_spice_session_store() -> SpiceSessionStore:
    global _store
    if _store is None:
        _store = SpiceSessionStore()
    return _store


def set_spice_session_store(store: Optional[SpiceSessionStore]) -> None:
    """Replace the process's store. For tests."""
    global _store
    _store = store
