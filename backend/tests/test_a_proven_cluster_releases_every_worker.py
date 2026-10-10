"""A cluster one worker proves reachable must stop answering 503 on the others within seconds.

The breaker is per process and prod runs four workers. After a cluster that refused every
certificate was repaired, only the worker serving Check Authentication or Verify reconnected; the
lab measured about 37s of 503 on the rest, until their trip expired and the healer came by.

What is pinned here:
  * a trusted connect (and only a trusted one) leaves a shared "proven at" marker;
  * a worker retries, once, a cluster it last saw fail before that marker, through
    ensure_connection, and a retry that fails is not repeated until a newer marker;
  * a worker with nothing failed does no cache I/O at all;
  * the reachability caches are dropped after a recovery and after a verified adopt, and check-auth
    uses the same helper;
  * every worker starts the loop.
"""

import ast
import asyncio
import inspect
import threading
import time
from pathlib import Path

import pytest

from app.core.blocking import BlockingPoolFull
from app.services.lxd import breaker_sync, health
from app.services.lxd.connection import LXDConnectionManager
from app.services.lxd.exceptions import (
    LXDAuthorizationError,
    LXDConnectionError,
    LXDLocalStorageError,
    LXDTransportPinError,
)

CLUSTER = "proven-under-test"
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_breaker(monkeypatch):
    monkeypatch.setattr(health, "_failed", {})
    monkeypatch.setattr(health, "_last_failed", {})


class _Cache:
    def __init__(self, proven=None, fail_set=False):
        self.proven = dict(proven or {})
        self.fail_set = fail_set
        self.sets = []
        self.gets = 0
        self.reachability = []

    def set(self, namespace, key, value, ttl=None):
        if self.fail_set:
            raise RuntimeError("redis went away")
        self.sets.append((namespace, key, value, ttl))
        self.proven[key] = value

    def get(self, namespace, key):
        self.gets += 1
        assert namespace == breaker_sync.PROVEN_NAMESPACE
        return self.proven.get(key)

    def invalidate_cluster_reachability(self, *cluster_ids):
        self.reachability.append(cluster_ids)


@pytest.fixture
def cache(monkeypatch):
    fake = _Cache()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: fake)
    return fake


# --- the breaker remembers a failure past its trip -------------------------------------------

def test_a_failure_is_remembered_past_the_trip_until_a_success_clears_it(monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(health.time, "time", lambda: clock[0])
    health.record_failure(CLUSTER, "certificate not trusted", kind=health.KIND_UNAUTHORIZED)
    clock[0] += health.DEFAULT_TTL + 1

    assert not health.is_tripped(CLUSTER), "the trip itself still expires on its TTL"
    assert health.failure_times() == {CLUSTER: 1_000.0}, (
        "the worker is still disconnected after the trip expires; the sync must see that window"
    )
    health.clear(CLUSTER)
    assert health.failure_times() == {}


# --- a trusted connect announces, nothing else does ------------------------------------------

class _Response:
    def __init__(self, auth, status_code=200):
        self.status_code = status_code
        self._auth = auth

    def json(self):
        return {"metadata": {"auth": self._auth}}


def _manager(monkeypatch, *, auth="trusted", status_code=200, raises=None):
    manager = LXDConnectionManager()
    monkeypatch.setattr("app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False)
    monkeypatch.setattr(manager, "_create_temp_cert_files", lambda cid, cert, key: ("/tmp/c.crt", "/tmp/c.key"))
    monkeypatch.setattr(manager, "_enforce_certificate_pin", lambda cid, endpoint: None)
    monkeypatch.setattr(manager, "_configure_client_session", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.activity_logger.get_activity_logger",
        lambda: type("L", (), {"log_activity": lambda *a, **k: None})(),
    )

    def client(**_kwargs):
        if raises is not None:
            raise raises
        api = type("Api", (), {"get": lambda _self, **_: _Response(auth, status_code)})()
        return type("Client", (), {"api": api})()

    monkeypatch.setattr("app.services.lxd.connection.pylxd.Client", client)
    return manager


def _connect(manager):
    return manager.connect_with_vault_cert(CLUSTER, "https://c:8443", "CERT", "KEY", verify=False, force=True)


def _flush_announcer():
    breaker_sync._announcer.submit(lambda: None).result(timeout=5)


def test_a_trusted_connect_announces_the_cluster(monkeypatch, cache):
    before = time.time()
    assert _connect(_manager(monkeypatch)) is not None
    _flush_announcer()
    assert [(ns, key, ttl) for ns, key, _value, ttl in cache.sets] == [
        (breaker_sync.PROVEN_NAMESPACE, CLUSTER, breaker_sync.PROVEN_TTL_S)
    ]
    assert before <= cache.sets[0][2] <= time.time()


def test_an_untrusted_connect_announces_nothing(monkeypatch, cache):
    with pytest.raises(LXDAuthorizationError):
        _connect(_manager(monkeypatch, auth="untrusted"))
    _flush_announcer()
    assert cache.sets == []


def test_a_transport_failure_announces_nothing(monkeypatch, cache):
    with pytest.raises(LXDConnectionError):
        _connect(_manager(monkeypatch, raises=ConnectionError("down")))
    _flush_announcer()
    assert cache.sets == []


def test_a_probe_that_never_said_trusted_announces_nothing(monkeypatch, cache):
    """The connect path keeps a client after a non-200 probe; that is not proof for anyone else."""
    _connect(_manager(monkeypatch, status_code=500))
    _flush_announcer()
    assert cache.sets == []


def test_a_cache_error_never_fails_a_good_connect(monkeypatch):
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache(fail_set=True))
    assert _connect(_manager(monkeypatch)) is not None
    _flush_announcer()
    assert not health.is_tripped(CLUSTER)


def test_a_hung_cache_never_holds_the_connect(monkeypatch):
    """The connect runs under _try_rotated_certificate's per-cluster lock; the write must not."""
    release = threading.Event()

    class _Hung(_Cache):
        def set(self, *args, **kwargs):
            release.wait(5)
            super().set(*args, **kwargs)

    hung = _Hung()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: hung)
    started = time.monotonic()
    assert _connect(_manager(monkeypatch)) is not None
    assert time.monotonic() - started < 1.0
    release.set()
    _flush_announcer()
    assert [key for _ns, key, _v, _ttl in hung.sets] == [CLUSTER]


# --- the sync retries once per marker --------------------------------------------------------

class _Service:
    def __init__(self, result=True, raises=None, records=None):
        self.result = result
        self.raises = raises
        self.records = records
        self.calls = []

    def ensure_connection(self, cluster_id):
        self.calls.append((cluster_id, health.is_tripped(cluster_id)))
        if self.records is not None:
            health.record_failure(cluster_id, "refused again", kind=self.records)
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    return now


def _failed_now(cluster_id=CLUSTER, *, kind=health.KIND_UNAUTHORIZED) -> float:
    health.record_failure(cluster_id, "certificate not trusted", kind=kind)
    return health.failure_times()[cluster_id]


def test_a_newer_proof_releases_and_reconnects_once(cache, clock):
    cache.proven[CLUSTER] = _failed_now() + 1
    clock[0] += 2
    service = _Service(result=True)

    assert breaker_sync.sync_once(service) == [CLUSTER]
    assert service.calls == [(CLUSTER, False)], "the trip is cleared before the healer's door is used"
    assert cache.reachability == [(CLUSTER,)]


def test_several_recoveries_invalidate_the_fleet_lists_once(cache, clock):
    other = "proven-sibling"
    _failed_now()
    _failed_now(other)
    cache.proven[CLUSTER] = cache.proven[other] = clock[0] + 1
    clock[0] += 2

    assert sorted(breaker_sync.sync_once(_Service(result=True))) == sorted([CLUSTER, other])
    assert len(cache.reachability) == 1 and sorted(cache.reachability[0]) == sorted([CLUSTER, other])


@pytest.mark.parametrize("proven_offset", [None, -50.0, 0.0])
def test_no_proof_or_an_older_one_changes_nothing(cache, clock, proven_offset):
    failed_at = _failed_now()
    if proven_offset is not None:
        cache.proven[CLUSTER] = failed_at + proven_offset
    service = _Service()

    assert breaker_sync.sync_once(service) == []
    assert service.calls == []
    assert health.is_tripped(CLUSTER), "a cluster nobody proved stays tripped"
    assert cache.reachability == []


@pytest.mark.parametrize("proven_at", [1e18, float("inf"), "not-a-time"])
def test_a_marker_from_the_future_or_garbage_is_not_proof(cache, clock, proven_at):
    _failed_now()
    cache.proven[CLUSTER] = proven_at
    service = _Service()

    assert breaker_sync.sync_once(service) == []
    assert service.calls == [], "honouring it would retry every failed cluster on every pass"


@pytest.mark.parametrize(
    "service",
    [
        _Service(result=False),
        _Service(result=False, records=health.KIND_UNAUTHORIZED),
    ],
    ids=["false-without-record", "refused-and-recorded"],
)
def test_a_failed_retry_keeps_the_trip_and_waits_for_a_newer_proof(cache, clock, service):
    cache.proven[CLUSTER] = _failed_now() + 1
    clock[0] += 2

    assert breaker_sync.sync_once(service) == []
    clock[0] += 5
    assert breaker_sync.sync_once(service) == []
    assert len(service.calls) == 1, "one attempt per marker, never a retry storm"
    assert health.is_tripped(CLUSTER)
    assert health.get_kind(CLUSTER) == health.KIND_UNAUTHORIZED, "a refused cluster must not read Offline"
    assert cache.reachability == []

    cache.proven[CLUSTER] = health.failure_times()[CLUSTER] + 1
    clock[0] += 2
    breaker_sync.sync_once(service)
    assert len(service.calls) == 2, "a newer proof earns one more attempt"


@pytest.mark.parametrize(
    "error",
    [LXDTransportPinError("pin changed"), LXDLocalStorageError("tmpfs full"), RuntimeError("bug")],
    ids=["pin", "local-storage", "unexpected"],
)
def test_a_fault_the_codebase_never_records_is_not_recorded_here(cache, clock, error):
    cache.proven[CLUSTER] = _failed_now() + 1
    clock[0] += 2
    service = _Service(raises=error)

    assert breaker_sync.sync_once(service) == []
    assert CLUSTER not in health.failure_times()
    assert not health.is_tripped(CLUSTER)
    assert breaker_sync.sync_once(service) == [] and len(service.calls) == 1


def test_an_expired_trip_is_not_revived_by_a_failed_retry(cache, clock):
    """The trip had already lapsed; a False with nothing recorded must not start a fresh one."""
    cache.proven[CLUSTER] = _failed_now() + 1
    clock[0] += health.DEFAULT_TTL + 1
    assert not health.is_tripped(CLUSTER)

    assert breaker_sync.sync_once(_Service(result=False)) == []
    assert not health.is_tripped(CLUSTER)


def test_a_worker_with_nothing_failed_reads_no_cache(cache):
    assert breaker_sync.due_for_retry() == []
    assert cache.gets == 0


def test_the_loop_does_no_work_while_nothing_failed_and_sheds_a_stuck_pass(monkeypatch):
    runs = []

    async def fake_run_blocking(fn, *args, **kwargs):
        runs.append(kwargs)
        if len(runs) == 1:
            raise BlockingPoolFull("lxd.breaker_sync", 1)

    monkeypatch.setattr(breaker_sync, "run_blocking", fake_run_blocking)
    monkeypatch.setattr("app.services.lxd.lxd_service.get_lxd_service", lambda: object())

    async def drive():
        task = asyncio.create_task(breaker_sync._loop(0.001))
        await asyncio.sleep(0.02)
        assert runs == []
        health.record_failure(CLUSTER, "down")
        await asyncio.sleep(0.05)
        assert not task.done(), "a shed pass must not end the loop"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert len(runs) >= 2
    assert all(kwargs["max_in_flight"] == 1 for kwargs in runs), "a stuck pass is skipped, never queued"


# --- caches and wiring -----------------------------------------------------------------------

@pytest.mark.parametrize("adopted", [True, False])
def test_a_verified_adopt_drops_the_reachability_caches(monkeypatch, cache, adopted):
    from app.services.lxd_certs import rotation

    class _Manager:
        def adopt_vault_certificate(self, *args):
            return object() if adopted else None

    service = type("Svc", (), {"connection_manager": _Manager()})()
    monkeypatch.setattr("app.services.lxd_service.get_lxd_service", lambda: service)

    rotation._adopt(CLUSTER, "https://c:8443", "CERT", "KEY")
    assert cache.reachability == ([(CLUSTER,)] if adopted else [])


def test_check_auth_uses_the_shared_reachability_invalidation():
    from app.routers import cluster_registration

    source = inspect.getsource(cluster_registration.check_cluster_authentication)
    assert "invalidate_cluster_reachability(cluster_id)" in source
    assert 'delete_pattern("api_responses", "nodes:*")' not in source, "one helper, not a second copy"


def _lifespan_calls() -> set:
    tree = ast.parse((BACKEND / "app" / "main.py").read_text())
    lifespan = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    return {
        node.func.id for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_every_worker_starts_and_stops_the_sync_loop():
    calls = _lifespan_calls()
    assert {"start_breaker_sync_loop", "stop_breaker_sync_loop"} <= calls
