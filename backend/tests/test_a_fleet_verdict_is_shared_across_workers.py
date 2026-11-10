"""One worker probes a dead cluster; the others take its word instead of dialing.

A dial against a silent host takes seconds, so every worker redialing each dead cluster whenever
its own trip lapses adds up, and a request can end up paying for one. Only the collector's leader
probes the clusters it holds down, on its own interval and past its own trip, and publishes a
"down at" marker with the time and the kind only. A follower's healer adopts a fresh marker instead
of dialing. A proof always outranks an older down verdict, a malformed marker is ignored, and
without a fresh marker (no leader probing, no Redis) every worker redials on its own trip.
"""

from __future__ import annotations

import ast
import time
import types
from pathlib import Path

import pytest

from app.core import cache as cache_module
from app.services import metrics_collector as mc
from app.services.lxd import breaker_sync, health
from app.services.lxd.exceptions import LXDTransportPinError

APP = Path(__file__).resolve().parents[1] / "app"
CLUSTER = "pandora"


@pytest.fixture(autouse=True)
def _clean():
    for cid in (CLUSTER, "blitz001", "live"):
        health.clear(cid)
        health._cleared_at.pop(cid, None)
    yield
    for cid in (CLUSTER, "blitz001", "live"):
        health.clear(cid)
        health._cleared_at.pop(cid, None)


@pytest.fixture
def shared(monkeypatch):
    """Shared mode on the memory cache conftest installs, with writes made synchronous."""
    monkeypatch.setattr(breaker_sync, "shared_mode", lambda: True)
    monkeypatch.setattr(breaker_sync._announcer, "submit", lambda fn, *a: fn(*a))
    return cache_module.get_cache_manager()


def _down(cache, cluster_id, at, kind=health.KIND_OFFLINE):
    cache.set(breaker_sync.DOWN_NAMESPACE, cluster_id, {"at": at, "kind": kind}, ttl=120)


# --- the verdict itself -------------------------------------------------------------------------


def test_a_well_formed_recent_marker_is_a_verdict(shared):
    now = time.time()
    _down(shared, CLUSTER, now - 3, health.KIND_UNAUTHORIZED)
    assert breaker_sync.shared_verdict(shared, CLUSTER) == (pytest.approx(now - 3), health.KIND_UNAUTHORIZED)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "down",
        {"at": "yesterday", "kind": "offline"},
        {"at": float("nan"), "kind": "offline"},
        {"at": time.time(), "kind": "pending"},
        {"at": time.time() + 3600, "kind": "offline"},
        {"kind": "offline"},
    ],
)
def test_anything_else_is_no_verdict(shared, value):
    if value is not None:
        shared.set(breaker_sync.DOWN_NAMESPACE, CLUSTER, value, ttl=120)
    assert breaker_sync.shared_verdict(shared, CLUSTER) is None


@pytest.mark.parametrize("proof_offset, verdict", [(-5, True), (0, False), (5, False)])
def test_a_proof_as_new_or_newer_wins(shared, proof_offset, verdict):
    observed = time.time() - 10
    _down(shared, CLUSTER, observed)
    shared.set(breaker_sync.PROVEN_NAMESPACE, CLUSTER, observed + proof_offset, ttl=300)
    assert (breaker_sync.shared_verdict(shared, CLUSTER) is not None) is verdict


def test_a_proof_lifts_the_down_marker(shared):
    _down(shared, CLUSTER, time.time() - 5)
    breaker_sync._write_marker(CLUSTER, time.time())
    assert shared.get(breaker_sync.DOWN_NAMESPACE, CLUSTER) is None


def test_the_marker_carries_only_the_time_and_the_kind(shared):
    breaker_sync.publish_down(CLUSTER, health.KIND_OFFLINE, 1234.5)
    assert shared.get(breaker_sync.DOWN_NAMESPACE, CLUSTER) == {"at": 1234.5, "kind": "offline"}
    breaker_sync.publish_down(CLUSTER, "anything else", 99.0)
    assert shared.get(breaker_sync.DOWN_NAMESPACE, CLUSTER) == {"at": 1234.5, "kind": "offline"}


# --- adoption ------------------------------------------------------------------------------------


def test_a_follower_adopts_a_fresh_verdict_with_its_kind(shared):
    observed = time.time() - 2
    _down(shared, CLUSTER, observed, health.KIND_UNAUTHORIZED)
    assert breaker_sync.adopt_shared(CLUSTER) is True
    assert health.known_unreachable(CLUSTER)
    reason, kind = health.last_failure(CLUSTER)
    assert kind == health.KIND_UNAUTHORIZED
    assert "://" not in reason and "." not in reason


def test_a_local_success_outranks_an_older_fleet_verdict(shared):
    _down(shared, CLUSTER, time.time() - 30)
    health.clear(CLUSTER)  # this worker connected after the leader's observation
    assert breaker_sync.adopt_shared(CLUSTER) is False
    assert not health.known_unreachable(CLUSTER)


def test_a_newer_local_failure_is_kept(shared):
    _down(shared, CLUSTER, time.time() - 30, health.KIND_UNAUTHORIZED)
    health.record_failure(CLUSTER, "connect failed: ConnectTimeout", kind=health.KIND_OFFLINE)
    assert breaker_sync.adopt_shared(CLUSTER) is True
    assert health.last_failure(CLUSTER)[1] == health.KIND_OFFLINE


def test_without_shared_mode_nothing_is_adopted(monkeypatch):
    monkeypatch.setattr(breaker_sync, "shared_mode", lambda: False)
    _down(cache_module.get_cache_manager(), CLUSTER, time.time())
    assert breaker_sync.adopt_shared(CLUSTER) is False


def test_shared_mode_needs_redis_and_can_be_switched_off(monkeypatch):
    # conftest's cache has no Redis, so every worker keeps its own breaker.
    assert breaker_sync.shared_mode() is False
    manager = cache_module.get_cache_manager()
    monkeypatch.setattr(manager, "redis_client", object(), raising=False)
    assert breaker_sync.shared_mode() is True
    monkeypatch.setenv("ORCASTRA_FLEET_VERDICT_SHARED", "false")
    assert breaker_sync.shared_mode() is False


# --- the leader's probe --------------------------------------------------------------------------


class _Service:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def ensure_connection(self, cluster_id, *, force=False):
        self.calls.append((cluster_id, force))
        result = self.outcome[cluster_id]
        if isinstance(result, BaseException):
            raise result
        if result is True:
            health.clear(cluster_id)
            return True
        health.record_failure(cluster_id, "connect failed: ConnectTimeout", kind=result)
        return False


def test_the_probe_redials_past_the_trip_and_publishes_what_it_saw(shared, monkeypatch):
    monkeypatch.setattr(shared, "invalidate_cluster_reachability", lambda *ids: None, raising=False)
    health.record_failure(CLUSTER, "old", kind=health.KIND_OFFLINE)
    health.record_failure("blitz001", "old", kind=health.KIND_UNAUTHORIZED)
    health.record_failure("live", "old", kind=health.KIND_OFFLINE)
    service = _Service({CLUSTER: health.KIND_OFFLINE, "blitz001": health.KIND_UNAUTHORIZED, "live": True})
    started = time.time()
    assert breaker_sync.probe_dead_once(service) == ["live"]
    assert sorted(service.calls) == [("blitz001", True), ("live", True), (CLUSTER, True)]
    down = shared.get(breaker_sync.DOWN_NAMESPACE, CLUSTER)
    assert down["kind"] == "offline" and down["at"] >= started - 1
    assert shared.get(breaker_sync.DOWN_NAMESPACE, "blitz001")["kind"] == "unauthorized"
    assert shared.get(breaker_sync.DOWN_NAMESPACE, "live") is None


def test_a_pin_violation_is_never_published(shared):
    health.record_failure(CLUSTER, "old", kind=health.KIND_OFFLINE)
    service = _Service({CLUSTER: LXDTransportPinError("pin")})
    assert breaker_sync.probe_dead_once(service) == []
    assert shared.get(breaker_sync.DOWN_NAMESPACE, CLUSTER) is None


def test_only_the_probe_publishes_a_down_verdict():
    """No request, router or service may mark a cluster down for the whole fleet."""
    callers = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        target = inner.func
                        name = getattr(target, "attr", None) or getattr(target, "id", None)
                        if name == "publish_down":
                            callers.append((path.relative_to(APP).as_posix(), node.name))
    assert sorted(set(callers)) == [("services/lxd/breaker_sync.py", "_probe_one")]


# --- the healer ----------------------------------------------------------------------------------


class _Manager:
    def __init__(self, pooled=()):
        self.pooled = set(pooled)
        self.dials = []

    def sync_registered_clusters(self, ids):
        pass

    def get_cached_client(self, cid):
        return object() if cid in self.pooled else None

    def get_client(self, cid):
        self.dials.append(("get_client", cid))
        return object() if cid in self.pooled else None

    def has_connection_info(self, cid):
        return False


def _healer(monkeypatch, manager, *, leading, clusters):
    settings = types.SimpleNamespace(vault_enabled=True, vault_token="t", vault_addr="http://v", vault_clusters_path="p")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.vault_client.get_vault_client", lambda addr, token: object())

    class _Certs:
        def __init__(self, vault_client, clusters_path):
            pass

        def list_clusters(self, use_cache=True):
            return list(clusters)

    monkeypatch.setattr("app.services.lxd_cert_manager.LXDCertificateManager", _Certs)
    collector = mc.MetricsCollector()
    collector._leading = leading
    dials = manager.dials

    def ensure(cid, **kwargs):
        dials.append(("ensure_connection", cid))
        return False

    collector._lxd_service = types.SimpleNamespace(connection_manager=manager, ensure_connection=ensure)
    collector._ensure_cluster_connections_blocking()
    return dials


def test_a_follower_never_dials_a_cluster_the_leader_holds_down(shared, monkeypatch):
    _down(shared, CLUSTER, time.time() - 2)
    manager = _Manager(pooled={"live"})
    dials = _healer(monkeypatch, manager, leading=False, clusters=[CLUSTER, "live"])
    assert dials == [("get_client", "live")]
    assert health.known_unreachable(CLUSTER)


def test_without_a_fresh_marker_a_follower_redials_as_before(shared, monkeypatch):
    health.record_failure(CLUSTER, "old", kind=health.KIND_OFFLINE)
    dials = _healer(monkeypatch, _Manager(), leading=False, clusters=[CLUSTER])
    assert ("get_client", CLUSTER) in dials


def test_the_leader_leaves_a_known_down_cluster_to_its_probe(shared, monkeypatch):
    health.record_failure(CLUSTER, "old", kind=health.KIND_OFFLINE)
    dials = _healer(monkeypatch, _Manager(), leading=True, clusters=[CLUSTER])
    assert dials == []


def test_without_shared_mode_the_healer_is_unchanged(monkeypatch):
    monkeypatch.setattr(breaker_sync, "shared_mode", lambda: False)
    health.record_failure(CLUSTER, "old", kind=health.KIND_OFFLINE)
    dials = _healer(monkeypatch, _Manager(), leading=True, clusters=[CLUSTER])
    assert ("get_client", CLUSTER) in dials
