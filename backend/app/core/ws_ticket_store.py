"""Where WebSocket tickets wait between their mint and the socket that redeems them.

important: keyed by the ticket's SHA-256, never the ticket, so a key listing, a slow log or a
MONITOR session cannot replay one. A mint is one script that also counts the caller's budget
(300 a minute), and a redeem is GETDEL, so a ticket opens one socket however many race for it.

important: signed. A stored ticket is an HMAC over its key's digest and its claims, with a key
derived from SECRET_KEY, and a redeem checks it before reading any claim. Without it, anyone who
could write to Redis could write a ticket for any identity and open a shell as it, no token
needed. Moving a signed entry to another key fails the check, because the key is part of it.

important: fails closed. With Redis required and not answering, a mint is refused and a redeem
returns nothing; a per-process store would let a ticket minted on one worker fail on another,
and silently degrading to it would hide the outage. The local store serves only a backend run
without Redis, which is one process. Without SECRET_KEY nothing is minted.
"""

import hashlib
import hmac
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
_MAC_CONTEXT = b"orcastra:ws-ticket:v1"

# KEYS[1] ticket key, KEYS[2] budget key. ARGV: signed claims, budget, window seconds, ticket ttl.
_MINT = """
local spent = redis.call('INCR', KEYS[2])
if spent == 1 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
if spent > tonumber(ARGV[2]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
return 1
"""


class TicketStoreUnavailable(Exception):
    """Redis is required and did not answer, or no signing key is configured."""


class TicketBudgetExceeded(Exception):
    """The caller minted more tickets in the window than any page needs."""


def _budget_key(user_key: str, prefix: str = BUDGET_PREFIX) -> str:
    return f"{prefix}:{hashlib.sha256(user_key.encode('utf-8')).hexdigest()}"


def _settings_mac_key(context: bytes = _MAC_CONTEXT) -> Optional[bytes]:
    from app.core.config import get_settings

    secret = get_settings().secret_key or ""
    if not secret:
        return None
    return hmac.new(secret.encode("utf-8"), context, hashlib.sha256).digest()


class WsTicketStore:
    """Single-use signed tickets. Its defaults are the WebSocket ones.

    note: another kind of ticket is another store with its own prefix and MAC context, as
    core/file_ticket.py does, so a ticket of one kind cannot be spent on the other's door.
    """

    def __init__(
        self,
        mac_key: Optional[bytes] = None,
        *,
        ticket_prefix: str = TICKET_PREFIX,
        budget_prefix: str = BUDGET_PREFIX,
        mac_context: bytes = _MAC_CONTEXT,
        budget: int = BUDGET_PER_WINDOW,
        ttl_seconds: int = TICKET_TTL_SECONDS,
    ) -> None:
        self._ticket_prefix = ticket_prefix
        self._budget_prefix = budget_prefix
        self._mac_context = mac_context
        self._budget = budget
        self._ttl_seconds = ttl_seconds
        self._client: Any = None
        self._redis_expected = False
        self._mac_key = mac_key
        self._lock = threading.Lock()
        self._local: Dict[str, Tuple[float, str]] = {}
        self._spent: Dict[str, Tuple[float, int]] = {}

    async def connect(self, redis_url: str, *, required: bool) -> None:
        """Use Redis when it answers. Lifespan only: redis.asyncio binds to the running loop.

        `required` (REDIS_ENABLED) makes an unreachable Redis a refusal rather than a fallback.
        """
        if required:
            self._redis_expected = True
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
        except Exception as exc:  # noqa: BLE001 - refused when required, local otherwise
            if required:
                logger.error("WebSocket ticket store could not reach Redis; sockets cannot open: %s", exc)
            else:
                logger.warning("WebSocket ticket store runs in this process only (Redis not reachable): %s", exc)
            return
        self._client = client
        self._redis_expected = True

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    def _key_bytes(self) -> bytes:
        if self._mac_key is None:
            self._mac_key = _settings_mac_key(self._mac_context)
        if not self._mac_key:
            logger.error("SECRET_KEY is not set; WebSocket tickets cannot be signed, so none are minted")
            raise TicketStoreUnavailable()
        return self._mac_key

    def _sign(self, key: str, claims_json: str) -> str:
        mac = hmac.new(self._key_bytes(), f"{key}\n{claims_json}".encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{mac}.{claims_json}"

    def _verified(self, key: str, stored: Optional[str]) -> Optional[str]:
        if not stored or "." not in stored:
            return None
        mac, claims_json = stored.split(".", 1)
        try:
            expected = hmac.new(self._key_bytes(), f"{key}\n{claims_json}".encode("utf-8"), hashlib.sha256).hexdigest()
        except TicketStoreUnavailable:
            return None
        if not hmac.compare_digest(mac, expected):
            logger.warning("WebSocket ticket refused: its stored claims do not carry a valid signature")
            return None
        return claims_json

    async def mint(self, user_key: str, claims_json: str) -> str:
        ticket = new_ticket()
        key = f"{self._ticket_prefix}:{ticket_digest(ticket)}"
        signed = self._sign(key, claims_json)
        if self._redis_expected:
            if self._client is None:
                raise TicketStoreUnavailable()
            try:
                granted = await self._client.eval(
                    _MINT, 2, key, _budget_key(user_key, self._budget_prefix),
                    signed, self._budget, BUDGET_WINDOW_SECONDS, self._ttl_seconds,
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
            if spent >= self._budget:
                raise TicketBudgetExceeded()
            self._spent[user_key] = (started, spent + 1)
            self._local = {k: v for k, v in self._local.items() if v[0] > now}
            self._local[key] = (now + self._ttl_seconds, signed)
        return ticket

    async def redeem(self, ticket: str) -> Optional[str]:
        """The ticket's claims, deleted as they are read; None when unknown, spent or unsigned."""
        if not ticket or len(ticket) > 128:
            return None
        key = f"{self._ticket_prefix}:{ticket_digest(ticket)}"
        if self._redis_expected:
            if self._client is None:
                return None
            try:
                return self._verified(key, await self._client.getdel(key))
            except Exception as exc:  # noqa: BLE001 - an unreadable ticket opens nothing
                logger.warning("WebSocket ticket redeem failed: %s", type(exc).__name__)
                return None
        with self._lock:
            entry = self._local.pop(key, None)
        if entry is None or entry[0] <= time.monotonic():
            return None
        return self._verified(key, entry[1])


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
