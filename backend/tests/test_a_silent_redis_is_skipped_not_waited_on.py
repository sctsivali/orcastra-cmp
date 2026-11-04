"""A Redis that stops answering costs one short timeout, then is left alone for a while.

During a two-minute `docker pause` of the dev Redis (2026-11-04) every request on every worker
waited until it came back: each command held its caller for 10 s (a 5 s socket timeout, retried
once) and most callers run on the event loop, so `/networks` took 123 s and `/tasks` 86 s. The
client now times out after 1 s without a retry, and core/redis_guard.py skips Redis for 15 s
after a failure: commands raise RedisSkipped at once and callers take their existing fallbacks.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, List

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core import cache as cache_module
from app.core import redis_guard
from app.core.cache import CacheInvalidationError, CacheManager
from app.core.redis_guard import GuardedRedis, RedisSkipped
from app.services.lxd import breaker_sync


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Client:
    """Records every command; raises whatever `fail` holds."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.fail: Any = None

    def _run(self, name: str, value: Any = None) -> Any:
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail
        return value

    def get(self, key):
        return self._run("get")

    def setex(self, key, ttl, value):
        return self._run("setex", True)

    def delete(self, *keys):
        return self._run("delete", len(keys))

    def ping(self):
        return self._run("ping", True)

    def scan_iter(self, **kwargs):
        self.calls.append("scan_iter")
        yield "orcastra:x:1"
        if self.fail is not None:
            raise self.fail

    def pipeline(self, transaction=True):
        return _Pipeline(self)

    def register_script(self, body):
        return _Script(self)


class _Pipeline:
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.queued: List[str] = []

    def hget(self, key, field):
        self.queued.append("hget")
        return self

    def evalsha(self, *args):
        self.queued.append("evalsha")
        return self

    def execute(self):
        return self.client._run("execute", list(self.queued))

    def __len__(self):
        return len(self.queued)


class _Script:
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.seen_clients: List[Any] = []

    def __call__(self, keys=None, args=None, client=None):
        self.seen_clients.append(client)
        target = client if client is not None else self.client
        if isinstance(target, _Pipeline):
            return target.evalsha(keys, args)
        return self.client._run("evalsha", 1)


@pytest.fixture
def setup():
    clock = _Clock()
    client = _Client()
    return GuardedRedis(client, skip_seconds=15.0, clock=clock), client, clock


# --- the guard --------------------------------------------------------------------------------


def test_a_command_that_times_out_skips_redis_for_the_window(setup, caplog):
    guard, client, clock = setup
    client.fail = RedisTimeoutError("Timeout reading from socket")
    with caplog.at_level(logging.WARNING, logger="app.core.redis_guard"):
        with pytest.raises(RedisTimeoutError):
            guard.get("a")
        for _ in range(5):
            with pytest.raises(RedisSkipped):
                guard.get("a")
    assert client.calls == ["get"]
    assert guard.available() is False
    assert [r.message for r in caplog.records].count(
        "Redis did not answer (TimeoutError); skipping it for 15 s, local fallbacks serve meanwhile"
    ) == 1


def test_a_refused_connection_trips_it_too(setup):
    guard, client, _clock = setup
    client.fail = RedisConnectionError("Error 111 connecting")
    with pytest.raises(RedisConnectionError):
        guard.setex("a", 5, "v")
    with pytest.raises(RedisSkipped):
        guard.delete("a")
    assert client.calls == ["setex"]


def test_a_skipped_command_is_a_connection_error_to_every_existing_handler(setup):
    guard, client, _clock = setup
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.get("a")
    with pytest.raises(redis.ConnectionError):
        guard.get("a")


def test_after_the_window_one_command_probes_and_an_answer_closes_it(setup, caplog):
    guard, client, clock = setup
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.get("a")
    clock.now += 15.0
    assert guard.available() is True
    client.fail = None
    with caplog.at_level(logging.INFO, logger="app.core.redis_guard"):
        assert guard.ping() is True
    assert guard.get("b") is None
    assert client.calls == ["get", "ping", "get"]
    assert "Redis answers again; using it" in [r.message for r in caplog.records]


def test_while_one_command_probes_the_others_keep_skipping(setup):
    guard, client, clock = setup
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.get("a")
    clock.now += 15.0
    guard._admit()  # the probe is let through and is still out
    with pytest.raises(RedisSkipped):
        guard.get("b")
    assert client.calls == ["get"]


def test_a_failed_probe_opens_another_full_window(setup):
    guard, client, clock = setup
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.get("a")
    clock.now += 15.0
    with pytest.raises(RedisTimeoutError):
        guard.get("probe")
    clock.now += 14.0
    with pytest.raises(RedisSkipped):
        guard.get("c")
    assert client.calls == ["get", "get"]


def test_closing_the_client_is_never_refused(setup):
    guard, client, _clock = setup
    client.close = lambda: "closed"
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.get("a")
    assert guard.close() == "closed"


def test_a_command_error_from_a_healthy_redis_does_not_trip_it(setup):
    guard, client, _clock = setup
    client.fail = ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")
    with pytest.raises(ResponseError):
        guard.get("a")
    client.fail = None
    assert guard.get("a") is None
    assert guard.available() is True


def test_a_scan_that_fails_mid_iteration_trips_it(setup):
    guard, client, _clock = setup
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        list(guard.scan_iter(match="orcastra:x:*"))
    with pytest.raises(RedisSkipped):
        list(guard.scan_iter(match="orcastra:x:*"))


def test_a_pipeline_queues_freely_and_its_execute_is_guarded(setup):
    guard, client, _clock = setup
    pipe = guard.pipeline(transaction=False)
    assert pipe.hget("k", "v") is pipe  # chaining keeps the wrapper
    assert bool(pipe) is True and len(pipe) == 1
    assert pipe.execute() == ["hget"]
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.pipeline().execute()
    with pytest.raises(RedisSkipped):
        guard.pipeline().execute()


def test_a_script_run_in_a_pipeline_is_handed_the_real_pipeline(setup):
    guard, client, _clock = setup
    script = guard.register_script("return 1")
    pipe = guard.pipeline(transaction=False)
    script(keys=["k"], args=[1], client=pipe)
    assert isinstance(script._inner.seen_clients[-1], _Pipeline)
    assert pipe.execute() == ["evalsha"]
    assert script(keys=["k"], args=[1]) == 1


def test_a_script_call_is_guarded(setup):
    guard, client, _clock = setup
    script = guard.register_script("return 1")
    client.fail = RedisConnectionError("refused")
    with pytest.raises(RedisConnectionError):
        script(keys=["k"], args=[])
    with pytest.raises(RedisSkipped):
        script(keys=["k"], args=[])


# --- the client it builds ---------------------------------------------------------------------


def test_the_shared_client_waits_one_second_and_never_retries():
    guard = redis_guard.connect("redis://127.0.0.1:1/0")
    kwargs = guard.raw.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1 and kwargs["socket_connect_timeout"] == 1
    connection = guard.raw.connection_pool.make_connection()
    assert connection.retry.get_retries() == 0


@pytest.fixture
def silent_server():
    """A TCP listener that accepts connections and never answers: a paused Redis."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: List[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    server.close()


def test_a_paused_redis_costs_one_second_then_nothing(silent_server):
    guard = redis_guard.connect(f"redis://127.0.0.1:{silent_server}/0")
    started = time.monotonic()
    with pytest.raises((RedisTimeoutError, RedisConnectionError)):
        guard.get("orcastra:any")
    first = time.monotonic() - started
    started = time.monotonic()
    for _ in range(20):
        with pytest.raises(RedisSkipped):
            guard.get("orcastra:any")
    rest = time.monotonic() - started
    # Before: 10 s for the first command and 10 s for each of the twenty after it.
    assert first < 2.5
    assert rest < 0.1


# --- what the callers see ---------------------------------------------------------------------


@pytest.fixture
def manager(setup):
    guard, client, clock = setup
    manager = CacheManager(redis_url=None)
    manager.redis_client = guard
    manager._redis_configured = True
    return manager, client, clock


def test_the_cache_serves_memory_while_redis_is_skipped_and_says_so_once(manager, caplog):
    cache, client, _clock = manager
    cache.set("lists", "a", {"rows": 1}, ttl=60)
    client.fail = RedisTimeoutError("t")
    with caplog.at_level(logging.WARNING):
        assert cache.get("lists", "a") == {"rows": 1}
        for _ in range(10):
            assert cache.get("lists", "a") == {"rows": 1}
    # One timed-out GET reached Redis; the reads after it were skipped without a log line.
    assert client.calls == ["setex", "get"]
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2, warnings  # the timeout itself, and the guard opening


def test_writes_and_invalidations_are_sent_while_reads_are_skipped(manager):
    """Security review 2026-11-04: an invalidation is how other workers learn an entry is stale.

    Revoking an API key writes Vault, deletes the shared `vault` copy of its metadata and publishes
    a tombstone. Skipped while this worker's guard was open, both were lost silently and every
    other worker went on reading the pre-revoke metadata from Redis for its whole TTL.
    """
    cache, client, _clock = manager
    client.fail = RedisTimeoutError("t")
    cache.get("lists", "a")  # opens the guard
    client.fail = None
    cache.delete("vault", "secret/data/integrations/api_keys/oak_1/metadata")
    cache.set("apikey_revoked", "oak_1", True, ttl=60)
    cache.delete_pattern("lists", "*")
    assert client.calls == ["get", "delete", "setex", "scan_iter", "delete"]
    # And their answer closed the guard: the next read reaches Redis again.
    cache.get("lists", "a")
    assert client.calls[-1] == "get"


def test_a_failed_invalidation_while_skipped_still_fails_when_it_must(manager):
    cache, client, _clock = manager
    client.fail = RedisTimeoutError("t")
    cache.get("lists", "a")
    with pytest.raises(CacheInvalidationError):
        cache.delete("lists", "a", require_shared=True)
    assert client.calls == ["get", "delete"]


def test_vault_entries_are_never_answered_from_memory_while_redis_is_not(manager):
    """A memory copy of an API key's metadata can predate its revocation on another worker."""
    cache, client, _clock = manager
    cache.sensitive_namespaces = []  # encryption is not what this test is about
    cache.set("vault", "secret/data/integrations/api_keys/oak_1/metadata", {"is_revoked": False}, ttl=600)
    cache.set("lists", "a", {"rows": 1}, ttl=60)
    client.fail = RedisTimeoutError("t")
    assert cache.get("vault", "secret/data/integrations/api_keys/oak_1/metadata") is None
    assert cache.get("lists", "a") == {"rows": 1}


def test_a_memory_only_deployment_still_reads_vault_entries_from_memory():
    cache = CacheManager(redis_url=None)
    cache.sensitive_namespaces = []
    cache.set("vault", "secret/data/clusters/nuc2/metadata", {"endpoint": "x"}, ttl=600)
    assert cache.get("vault", "secret/data/clusters/nuc2/metadata") == {"endpoint": "x"}


def test_a_script_queued_into_a_pipeline_neither_waits_on_nor_closes_the_guard(setup):
    """Queuing sends nothing, so it must not be refused, and must not count as Redis answering."""
    guard, client, _clock = setup
    script = guard.register_script("return 1")
    client.fail = RedisTimeoutError("t")
    with pytest.raises(RedisTimeoutError):
        guard.get("a")
    client.fail = None
    pipe = guard.pipeline(transaction=False)
    script(keys=["k"], args=[1], client=pipe)
    assert guard.available() is False
    with pytest.raises(RedisSkipped):
        pipe.execute()


def test_an_entry_read_while_skipped_is_not_authoritative(manager):
    cache, client, _clock = manager
    cache.set("lists", "a", {"rows": 1}, ttl=60)
    client.fail = RedisTimeoutError("t")
    cache.get("lists", "a")
    _value, authoritative = cache.get_entry("lists", "a")
    assert authoritative is False


def test_dead_cluster_verdicts_stop_being_shared_while_redis_is_skipped(manager, monkeypatch):
    cache, client, _clock = manager
    monkeypatch.setattr(cache_module, "_global_cache", cache)
    monkeypatch.delenv("ORCASTRA_FLEET_VERDICT_SHARED", raising=False)
    assert breaker_sync.shared_mode() is True
    client.fail = RedisTimeoutError("t")
    cache.get("lists", "a")
    assert breaker_sync.shared_mode() is False
    assert breaker_sync.adopt_shared("pandora") is False


def test_a_plain_client_without_a_guard_still_counts_as_answering():
    assert redis_guard.answering(None) is False
    assert redis_guard.answering(_Client()) is True
