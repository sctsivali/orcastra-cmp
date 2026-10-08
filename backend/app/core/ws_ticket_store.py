"""Where WebSocket tickets wait between their mint and the socket that redeems them.

important: keyed by the ticket's SHA-256, never the ticket, so a key listing, a slow log or a
MONITOR session cannot replay one. A mint is one script that also counts the caller's budget
(300 a minute), and a redeem is GETDEL, so a ticket opens one socket however many race for it.

important: fails closed. With Redis enabled and not answering, a mint is refused and a redeem
returns nothing; a per-process store would let a ticket minted on one worker fail on another,
and silently degrading to it would hide the outage. The local store serves only a backend run
without Redis, which is one process.
"""

import hashlib
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from app.core.ws_ticket import TICKET_TTL_SECONDS, new_ticket, ticket_digest

logger = logging.getLogger(__name__)

TICKET_PREFIX = "orcastra:ws_ticket"
BUDGET_PREFIX = "orcastra:ws_ticket_budget"
BUDGET_PER_WINDOW = 300
BUDGET_WINDOW_SECONDS = 60
_CONNECT_TIMEOUT = 2.0
_SOCKET_TIMEOUT = 2.0

# KEYS[1] ticket key, KEYS[2] budget key. ARGV: claims, budget, window seconds, ticket ttl.
_MINT = """
local spent = redis.call('INCR', KEYS[2])
if spent == 1 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
if spent > tonumber(ARGV[2]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
return 1
"""


class TicketStoreUnavailable(Exception):
    """Redis is enabled and did not answer; no ticket can be minted or redeemed."""


class TicketBudgetExceeded(Exception):
    """The caller minted more tickets in the window than any page needs."""


def _budget_key(user_key: str) -> str:
    return f"{BUDGET_PREFIX}:{hashlib.sha256(user_key.encode('utf-8')).hexdigest()}"


class WsTicketStore:
    def __init__(self) -> None:
        self._client: Any = None
        self._redis_expected = False
        self._lock = threading.Lock()
        self._local: Dict[str, Tuple[float, str]] = {}
        self._spent: Dict[str, Tuple[float, int]] = {}

    async def connect(self, redis_url: str) -> None:
        """Build the async client. Lifespan startup only: redis.asyncio binds to the running loop."""
        self._redis_expected = True
        try:
            import redis.asyncio as aioredis

            self._client = aioredis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=_CONNECT_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
                retry_on_timeout=False,
            )
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001 - the store then refuses rather than degrades
            logger.error("WebSocket ticket store could not reach Redis; sockets cannot open: %s", exc)
            self._client = None

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def mint(self, user_key: str, claims_json: str) -> str:
        ticket = new_ticket()
        key = f"{TICKET_PREFIX}:{ticket_digest(ticket)}"
        if self._redis_expected:
            if self._client is None:
                raise TicketStoreUnavailable()
            try:
                granted = await self._client.eval(
                    _MINT, 2, key, _budget_key(user_key),
                    claims_json, BUDGET_PER_WINDOW, BUDGET_WINDOW_SECONDS, TICKET_TTL_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - refused, never minted somewhere else
                logger.warning("WebSocket ticket mint failed: %s", type(exc).__name__)
                raise TicketStoreUnavailable() from exc
            if not granted:
                raise TicketBudgetExceeded()
            return ticket
        now = time.monotonic()
        with self._lock:
            started, spent = self._spent.get(user_key, (now, 0))
            if now - started >= BUDGET_WINDOW_SECONDS:
                started, spent = now, 0
            if spent >= BUDGET_PER_WINDOW:
                raise TicketBudgetExceeded()
            self._spent[user_key] = (started, spent + 1)
            self._local = {k: v for k, v in self._local.items() if v[0] > now}
            self._local[key] = (now + TICKET_TTL_SECONDS, claims_json)
        return ticket

    async def redeem(self, ticket: str) -> Optional[str]:
        """The ticket's claims, deleted as they are read; None when it is unknown or spent."""
        if not ticket or len(ticket) > 128:
            return None
        key = f"{TICKET_PREFIX}:{ticket_digest(ticket)}"
        if self._redis_expected:
            if self._client is None:
                return None
            try:
                return await self._client.getdel(key)
            except Exception as exc:  # noqa: BLE001 - an unreadable ticket opens nothing
                logger.warning("WebSocket ticket redeem failed: %s", type(exc).__name__)
                return None
        with self._lock:
            entry = self._local.pop(key, None)
        if entry is None or entry[0] <= time.monotonic():
            return None
        return entry[1]


_store: Optional[WsTicketStore] = None


def get_ws_ticket_store() -> WsTicketStore:
    global _store
    if _store is None:
        _store = WsTicketStore()
    return _store


def set_ws_ticket_store(store: Optional[WsTicketStore]) -> None:
    """Replace the process's store. For tests."""
    global _store
    _store = store
