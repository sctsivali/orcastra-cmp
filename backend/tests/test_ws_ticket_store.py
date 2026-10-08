"""The ticket store: single use, 30 seconds, a per-caller budget, digests only, fails closed.

A ticket replaces an access token in a WebSocket URL, so it must be worth less than one: it
opens one socket within 30 seconds, is gone once used, and the store never holds it in a form a
key listing or a log line could replay. With Redis enabled and down, nothing is minted.
"""

import asyncio
import hashlib

import pytest

from app.core import ws_ticket_store
from app.core.ws_ticket_store import (
    BUDGET_PER_WINDOW,
    TICKET_PREFIX,
    TicketBudgetExceeded,
    TicketStoreUnavailable,
    WsTicketStore,
)


KEY = b"test-signing-key"


def _run(coro):
    return asyncio.run(coro)


def test_a_ticket_opens_once():
    store = WsTicketStore(mac_key=KEY)
    ticket = _run(store.mint("alice", '{"kind":"tasks"}'))

    assert _run(store.redeem(ticket)) == '{"kind":"tasks"}'
    assert _run(store.redeem(ticket)) is None, "spent"


def test_a_ticket_expires_after_thirty_seconds(monkeypatch):
    store = WsTicketStore(mac_key=KEY)
    clock = [1000.0]
    monkeypatch.setattr(ws_ticket_store.time, "monotonic", lambda: clock[0])
    ticket = _run(store.mint("alice", "{}"))

    clock[0] += 31

    assert _run(store.redeem(ticket)) is None


def test_the_budget_refuses_past_three_hundred_a_minute(monkeypatch):
    store = WsTicketStore(mac_key=KEY)
    clock = [1000.0]
    monkeypatch.setattr(ws_ticket_store.time, "monotonic", lambda: clock[0])
    for _ in range(BUDGET_PER_WINDOW):
        _run(store.mint("alice", "{}"))

    with pytest.raises(TicketBudgetExceeded):
        _run(store.mint("alice", "{}"))
    _run(store.mint("bob", "{}"))
    clock[0] += 61
    _run(store.mint("alice", "{}"))


@pytest.mark.parametrize("junk", ["", "x" * 200])
def test_an_unusable_ticket_reads_nothing(junk):
    assert _run(WsTicketStore(mac_key=KEY).redeem(junk)) is None


class _FakeAsyncRedis:
    def __init__(self, fail=None, granted=1):
        self.values = {}
        self.evals = []
        self.fail = fail
        self.granted = granted

    async def ping(self):
        if self.fail:
            raise self.fail
        return True

    async def eval(self, script, numkeys, *args):
        if self.fail:
            raise self.fail
        keys, argv = args[:numkeys], args[numkeys:]
        self.evals.append((keys, argv))
        if self.granted:
            self.values[keys[0]] = argv[0]
        return self.granted

    async def getdel(self, key):
        if self.fail:
            raise self.fail
        return self.values.pop(key, None)

    async def aclose(self):
        return None


def _redis_store(fake):
    store = WsTicketStore(mac_key=KEY)
    store._redis_expected = True
    store._client = fake
    return store


def test_redis_keeps_only_the_digest_and_a_hashed_budget_key():
    fake = _FakeAsyncRedis()
    store = _redis_store(fake)

    ticket = _run(store.mint("alice@example.test", '{"kind":"tasks"}'))

    (keys, argv), = fake.evals
    assert keys[0] == f"{TICKET_PREFIX}:{hashlib.sha256(ticket.encode()).hexdigest()}"
    assert ticket not in "".join(keys) and "alice" not in keys[1]
    assert argv[1:] == (BUDGET_PER_WINDOW, 60, 30)
    assert ticket not in argv[0] and argv[0].endswith('{"kind":"tasks"}'), "signed claims, no ticket"
    assert _run(store.redeem(ticket)) == '{"kind":"tasks"}'
    assert _run(store.redeem(ticket)) is None


def test_redis_refusing_the_budget_mints_nothing():
    store = _redis_store(_FakeAsyncRedis(granted=0))

    with pytest.raises(TicketBudgetExceeded):
        _run(store.mint("alice", "{}"))


def test_a_failing_redis_fails_closed():
    store = _redis_store(_FakeAsyncRedis(fail=ConnectionError("refused")))

    with pytest.raises(TicketStoreUnavailable):
        _run(store.mint("alice", "{}"))
    assert _run(store.redeem("anything")) is None


def test_redis_that_never_connected_is_not_replaced_by_the_local_store(monkeypatch):
    import redis.asyncio as aioredis

    class _Down:
        async def ping(self):
            raise ConnectionError("no route")

    monkeypatch.setattr(aioredis.Redis, "from_url", staticmethod(lambda *a, **k: _Down()))
    store = WsTicketStore(mac_key=KEY)
    _run(store.connect("redis://redis.invalid:6379/0", required=True))

    with pytest.raises(TicketStoreUnavailable):
        _run(store.mint("alice", "{}"))

    optional = WsTicketStore(mac_key=KEY)
    _run(optional.connect("redis://redis.invalid:6379/0", required=False))
    ticket = _run(optional.mint("alice", "{}"))
    assert _run(optional.redeem(ticket)) == "{}", "without REDIS_ENABLED the process store serves"


def test_claims_written_to_redis_without_the_signature_open_nothing():
    fake = _FakeAsyncRedis()
    store = _redis_store(fake)
    ticket = "forged-ticket-value"
    key = f"{TICKET_PREFIX}:{hashlib.sha256(ticket.encode()).hexdigest()}"
    forged = '{"kind":"terminal","user_id":"victim@example.test","groups":["role_admin"]}'

    fake.values[key] = forged
    assert _run(store.redeem(ticket)) is None, "no signature"

    fake.values[key] = "0" * 64 + "." + forged
    assert _run(store.redeem(ticket)) is None, "a made-up signature"

    other = WsTicketStore(mac_key=b"another deployment's key")
    fake.values[key] = other._sign(key, forged)
    assert _run(store.redeem(ticket)) is None, "signed with another key"


def test_a_signed_entry_moved_to_another_ticket_opens_nothing():
    fake = _FakeAsyncRedis()
    store = _redis_store(fake)
    genuine = _run(store.mint("alice", '{"kind":"tasks"}'))
    genuine_key = f"{TICKET_PREFIX}:{hashlib.sha256(genuine.encode()).hexdigest()}"
    stolen = fake.values.pop(genuine_key)

    mine = "attacker-chosen-ticket"
    fake.values[f"{TICKET_PREFIX}:{hashlib.sha256(mine.encode()).hexdigest()}"] = stolen

    assert _run(store.redeem(mine)) is None


def test_without_a_signing_key_nothing_is_minted(monkeypatch):
    from app.core import ws_ticket_store as module

    monkeypatch.setattr(module, "_settings_mac_key", lambda: None)
    store = WsTicketStore()

    with pytest.raises(TicketStoreUnavailable):
        _run(store.mint("alice", "{}"))
    assert _run(store.redeem("anything")) is None
