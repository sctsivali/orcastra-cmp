"""`GET /clusters` must cost about one row, not the sum of its rows, and never re-dial a dead one.

Every list row probes its cluster: a pooled-client revalidation, a Vault read and, for a cluster
with no live connection, a TLS connect. The rows were built by coroutines that made those calls
inline, so `asyncio.gather` ran them one after another on the loop thread. On the dev fleet five
offline clusters made every cold read take about 40s, which is how long the migration wizard sat
on "Loading clusters".

The row also forced a reconnect even when this worker had just seen that cluster fail, so each dead
cluster paid at least one more connect timeout on every cold list read. The background healer
already retries dead clusters and clears the verdict on success, so the list reports the verdict
instead. Refresh (fresh=true) is the operator asking for a real recheck, and that one still forces it.
"""

import threading
import time
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.cluster_status import ClusterStatus
from app.models.schemas import ClusterInfo
from app.services.lxd import health
from app.services.lxd_cert_manager import ConnectionProbe

LIVE = "qa-list-live"
DEAD = "qa-list-dead"


class _CertManager:
    clusters_path = "secret/data/clusters"
    ids: List[str] = [LIVE, DEAD]

    def list_clusters(self, use_cache: bool = True) -> List[str]:
        return list(self.ids)

    def get_cluster_info(self, cluster_id: str, include_cert_content: bool = False) -> dict:
        return {
            "metadata": {"endpoint": f"https://{cluster_id}:8443", "display_name": cluster_id},
            "certificate": {"has_certificate": True, "common_name": "orcastra"},
        }

    def get_cluster_certificate(self, cluster_id: str) -> dict:
        return {"client.crt": "crt", "client.key": "key"}

    def probe_connection(self, cluster_id: str, use_breaker: bool = True) -> ConnectionProbe:
        return ConnectionProbe(False, False, "Cannot connect to LXD endpoint.")


class _ConnectionManager:
    def __init__(self) -> None:
        self.connects: List[str] = []
        self.revalidated: List[str] = []

    def get_cached_client(self, cluster_id: str) -> Any:
        return self.get_client(cluster_id) if cluster_id == LIVE else None

    def get_client(self, cluster_id: str, *args: Any, **kwargs: Any) -> Any:
        if cluster_id != LIVE:
            # The real one auto-reconnects from cached credentials here: a dial of its own.
            self.revalidated.append(cluster_id)
            return None
        return SimpleNamespace(
            host_info={"auth": "trusted", "api_extensions": [], "environment": {"server": "lxd"}}
        )

    def connect_with_vault_cert(self, cluster_id: str, **kwargs: Any) -> Any:
        self.connects.append(cluster_id)
        raise ConnectionError("connect failed: ClientConnectionFailed")


@pytest.fixture
def connections() -> _ConnectionManager:
    return _ConnectionManager()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, connections: _ConnectionManager) -> TestClient:
    import app.services.lxd_service as lxd_service_module
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import cluster_registration as module

    monkeypatch.setattr(
        lxd_service_module, "get_lxd_service", lambda: SimpleNamespace(connection_manager=connections)
    )
    monkeypatch.setattr(module, "apply_cluster_filter", lambda ids: ids)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    health.clear(LIVE)
    health.clear(DEAD)

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = _CertManager
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    yield TestClient(app, raise_server_exceptions=False)
    health.clear(LIVE)
    health.clear(DEAD)


def _rows(client: TestClient, path: str = "/clusters") -> dict:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return {row["cluster_id"]: row for row in response.json()["clusters"]}


def test_rows_are_built_side_by_side_off_the_event_loop(client, monkeypatch) -> None:
    from app.routers import cluster_registration as module

    loop_thread = threading.get_ident()
    threads: List[int] = []
    monkeypatch.setattr(_CertManager, "ids", [f"qa-row-{n}" for n in range(6)])

    def slow_row(cluster_id: str, cert_manager: Any, *, force: bool = False) -> ClusterInfo:
        threads.append(threading.get_ident())
        time.sleep(0.4)
        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=cluster_id,
            endpoint="https://host:8443",
            status=ClusterStatus.OFFLINE,
            has_certificate=True,
        )

    monkeypatch.setattr(module, "_cluster_info_blocking", slow_row)

    started = time.monotonic()
    rows = _rows(client)
    elapsed = time.monotonic() - started

    assert len(rows) == 6
    # One after another this is 2.4s. Side by side it is one row plus scheduling.
    assert elapsed < 1.5, elapsed
    assert loop_thread not in threads


def _lapse(cluster_id: str) -> None:
    """Age the trip past its TTL, as it is between two healer retries of a dead cluster."""
    with health._lock:
        failed_at, reason, kind = health._failed[cluster_id]
        health._failed[cluster_id] = (failed_at - health.DEFAULT_TTL - 1, reason, kind)
    assert not health.is_tripped(cluster_id)


@pytest.mark.parametrize("lapsed", [False, True], ids=["tripped", "trip-lapsed"])
def test_a_plain_read_takes_the_breaker_verdict_without_dialing(client, connections, lapsed) -> None:
    health.record_failure(DEAD, "connect failed: ClientConnectionFailed")
    if lapsed:
        _lapse(DEAD)

    rows = _rows(client)

    assert connections.connects == []
    assert connections.revalidated == []
    assert rows[DEAD]["status"] == ClusterStatus.OFFLINE.value
    # The breaker's raw reason stays in the logs; the row carries the curated sentence.
    assert rows[DEAD]["error_message"] == "Cluster did not answer."
    assert rows[DEAD]["endpoint"] == f"https://{DEAD}:8443"
    assert rows[LIVE]["status"] == ClusterStatus.ONLINE.value


@pytest.mark.parametrize("lapsed", [False, True], ids=["tripped", "trip-lapsed"])
def test_a_refused_certificate_stays_pending_trust_from_the_breaker(client, connections, lapsed) -> None:
    health.record_failure(DEAD, "certificate not trusted", kind=health.KIND_UNAUTHORIZED)
    if lapsed:
        _lapse(DEAD)

    rows = _rows(client)

    assert connections.connects == []
    assert rows[DEAD]["status"] == ClusterStatus.NOT_AUTHORIZED.value
    assert "rejected our certificate" in rows[DEAD]["error_message"]


def test_refresh_still_forces_a_real_recheck(client, connections) -> None:
    health.record_failure(DEAD, "connect failed: ClientConnectionFailed")

    rows = _rows(client, "/clusters?fresh=true")

    assert connections.revalidated == [DEAD]
    assert connections.connects == [DEAD]
    assert rows[DEAD]["status"] == ClusterStatus.OFFLINE.value


def test_a_cluster_this_worker_never_saw_fail_is_still_dialed_once(client, connections) -> None:
    rows = _rows(client)

    assert connections.connects == [DEAD]
    assert rows[DEAD]["status"] == ClusterStatus.OFFLINE.value


def test_a_row_that_outlives_its_budget_does_not_hold_the_list(client, monkeypatch) -> None:
    from app.routers import cluster_registration as module

    real_row = module._cluster_info_blocking

    def hung_row(cluster_id: str, cert_manager: Any, *, force: bool = False) -> Any:
        if cluster_id == DEAD:
            # The thread outlives the test, so it must not reach the real service afterwards.
            time.sleep(1.0)
            return None
        return real_row(cluster_id, cert_manager, force=force)

    monkeypatch.setattr(module, "_cluster_info_blocking", hung_row)
    monkeypatch.setattr(module, "_CLUSTER_ROW_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    rows = _rows(client)

    assert time.monotonic() - started < 0.9
    assert rows[DEAD]["status"] == ClusterStatus.ERROR.value
    assert "did not finish in time" in rows[DEAD]["error_message"]
    assert rows[LIVE]["status"] == ClusterStatus.ONLINE.value


def test_a_list_with_an_unfinished_row_is_never_cached(client, monkeypatch) -> None:
    from app.routers import cluster_registration as module

    class _Cache:
        def __init__(self) -> None:
            self.writes: List[str] = []

        def get(self, namespace: str, key: str) -> Any:
            return None

        def set(self, namespace: str, key: str, value: Any, ttl: int = 0) -> None:
            self.writes.append(key)

    cache = _Cache()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: cache)
    monkeypatch.setattr(module, "get_resource_list_generation", lambda *_args, **_kwargs: 1)
    real_row = module._cluster_info_blocking

    def hung_row(cluster_id: str, cert_manager: Any, *, force: bool = False) -> Any:
        if cluster_id == DEAD:
            time.sleep(1.0)
            return None
        return real_row(cluster_id, cert_manager, force=force)

    monkeypatch.setattr(module, "_cluster_info_blocking", hung_row)
    monkeypatch.setattr(module, "_CLUSTER_ROW_TIMEOUT_SECONDS", 0.2)

    assert _rows(client)[DEAD]["status"] == ClusterStatus.ERROR.value
    assert cache.writes == []

    # The same read with every row answered is cached as before.
    monkeypatch.setattr(module, "_cluster_info_blocking", real_row)
    _rows(client)
    assert len(cache.writes) == 1


def test_a_finished_list_is_fresh_for_thirty_seconds(client, monkeypatch) -> None:
    """Owner decision 2026-11-04: a cluster going down shows within about 35 s, not 180.

    A recovery clears the list at once and a failure does not, so after a cluster stopped
    answering the list read online until its entry aged out: 88 s in the lab, up to 180.
    """
    from app.core.swr import SWR_MAX_STALE_SECONDS
    from app.routers import cluster_registration as module

    class _Cache:
        def __init__(self) -> None:
            self.writes: List[Any] = []

        def get(self, namespace: str, key: str) -> Any:
            return None

        def set(self, namespace: str, key: str, value: Any, ttl: int = 0) -> None:
            self.writes.append((value, ttl))

    cache = _Cache()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: cache)
    monkeypatch.setattr(module, "get_resource_list_generation", lambda *_args, **_kwargs: 1)

    _rows(client)

    [(envelope, ttl)] = cache.writes
    assert envelope["s"] == 30
    assert ttl == 30 + SWR_MAX_STALE_SECONDS
