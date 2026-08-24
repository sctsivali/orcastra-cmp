"""The node-picker endpoint's whole contract, driven through the real handler.

A picker that hides a dead cluster is worse than one with no status at all: an operator who
cannot find a cluster in the list concludes it was deregistered and goes looking for a
registration bug. Every registered cluster must be present, and a cluster that is merely down
must read `offline`, not `error` and not `not_authorized`.

Hermetic. No Vault, no pylxd, no Redis, no database.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.cluster_status import ClusterStatus
from app.services.lxd import health

ALIVE = ["nuc2", "pandora-semanan"]
DEAD = ["blitzkrieg", "local-cluster", "franklin-lxd"]
ALL = sorted(ALIVE + DEAD)


class _ConnectionManager:
    """Alive clusters hand back a pooled client; dead ones return None, exactly as the real
    manager does once health.is_tripped() is true."""

    def get_client(self, host, *args, **kwargs):
        return object() if host in ALIVE else None

    def connect_with_vault_cert(self, cluster_id, **kwargs):
        from app.services.lxd.exceptions import LXDConnectionError
        if cluster_id in ALIVE:
            return object()
        raise LXDConnectionError(f"Cannot connect to {cluster_id}")


class _Vault:
    def vault_read(self, path):
        cluster_id = path.split("/")[-2]
        return {"endpoint": f"https://{cluster_id}:8443", "display_name": cluster_id}


class _CertManager:
    def __init__(self):
        self.vault = _Vault()

    def get_cluster_certificate(self, cluster_id):
        return {"client.crt": "crt", "client.key": "key"}


class _Service:
    def __init__(self):
        self.connection_manager = _ConnectionManager()
        self.cert_manager = _CertManager()

    def get_node_names(self):
        return list(ALL)


class _Cache:
    """A cache that always misses, so the handler is forced down the probe path."""

    def __init__(self):
        self.writes = {}

    def get(self, namespace, key):
        return None

    def set(self, namespace, key, value, ttl=None):
        self.writes[key] = (value, ttl)

    def delete_pattern(self, *args, **kwargs):
        return None


@pytest.fixture
def client(monkeypatch):
    from app.routers import nodes as nodes_module
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    cache = _Cache()
    monkeypatch.setattr(nodes_module, "get_cache_manager", lambda: cache)
    for name in DEAD:
        health.record_failure(name, "connection refused")
    for name in ALIVE:
        health.clear(name)

    app = FastAPI()
    app.include_router(nodes_module.router)
    app.dependency_overrides[get_lxd_service] = _Service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    yield TestClient(app, raise_server_exceptions=False), cache
    for name in ALL:
        health.clear(name)


def test_every_registered_cluster_is_present(client):
    tc, _ = client
    body = tc.get("/nodes/availability").json()
    assert sorted(row["name"] for row in body["clusters"]) == ALL
    assert body["total_count"] == len(ALL)


def test_a_down_cluster_reads_offline_not_error(client):
    tc, _ = client
    body = tc.get("/nodes/availability").json()
    by_name = {row["name"]: row for row in body["clusters"]}
    for name in DEAD:
        assert by_name[name]["status"] == ClusterStatus.OFFLINE.value, by_name[name]
    # ERROR means a fault in us. A cluster being down is not one.
    assert not [r for r in body["clusters"] if r["status"] == ClusterStatus.ERROR.value]


def test_a_reachable_cluster_reads_online_and_available(client):
    tc, _ = client
    body = tc.get("/nodes/availability").json()
    by_name = {row["name"]: row for row in body["clusters"]}
    for name in ALIVE:
        assert by_name[name]["status"] == ClusterStatus.ONLINE.value
        assert by_name[name]["available"] is True
    assert body["online_count"] == len(ALIVE)


def test_it_never_answers_503_for_a_dead_fleet(client):
    """"That cluster is down" is this endpoint's payload, not its failure."""
    tc, _ = client
    response = tc.get("/nodes/availability")
    assert response.status_code == 200, response.text


def test_offline_rows_carry_a_short_operator_safe_reason(client):
    tc, _ = client
    body = tc.get("/nodes/availability").json()
    for row in body["clusters"]:
        if row["status"] == ClusterStatus.ONLINE.value:
            continue
        assert row["reason"], f"{row['name']} is not online and must say why"
        assert len(row["reason"]) <= 200
        assert "Traceback" not in row["reason"]


def test_the_name_is_the_id_a_create_request_sends(client):
    """The join key. If this endpoint keyed on anything other than the string
    /nodes/names returns, the picker's left-join yields all Unknown and the feature
    looks like it works while carrying no information."""
    tc, _ = client
    body = tc.get("/nodes/availability").json()
    assert {row["name"] for row in body["clusters"]} == set(_Service().get_node_names())


def test_the_probe_result_is_cached_with_the_endpoints_ttl(client):
    from app.core.cache import NODES_AVAILABILITY_CACHE_KEY, NODES_AVAILABILITY_CACHE_TTL

    tc, cache = client
    tc.get("/nodes/availability")
    written = [(k, v) for k, v in cache.writes.items() if NODES_AVAILABILITY_CACHE_KEY in k]
    assert written, f"nothing was cached; saw {list(cache.writes)}"
    assert written[0][1][1] == NODES_AVAILABILITY_CACHE_TTL


def test_an_all_offline_result_is_still_cached(client):
    """Unlike the list fan-outs, an empty-of-reachable-clusters answer IS the answer."""
    from app.core.cache import NODES_AVAILABILITY_CACHE_KEY

    tc, cache = client
    for name in ALL:
        health.record_failure(name, "connection refused")
    body = tc.get("/nodes/availability").json()
    assert body["online_count"] == 0
    assert body["total_count"] == len(ALL)
    assert any(NODES_AVAILABILITY_CACHE_KEY in k for k in cache.writes)


def test_a_reason_never_carries_infrastructure_detail(client):
    """The picker is reachable by every role down to tenant.

    Raw exception text from this layer carries the Vault base URL, the KV path to a cluster's
    certificates and the temp client-cert path. `reason_code` already carries everything a
    client keys off, so the human sentence is curated and the detail stays in the logs.
    """
    tc, _ = client
    body = tc.get("/nodes/availability").json()
    for row in body["clusters"]:
        reason = row["reason"] or ""
        for leak in ("secret/data", "vault", "client.key", "client.crt", "/tmp/",
                     "Traceback", "HTTPSConnectionPool", "http://", "https://"):
            assert leak.lower() not in reason.lower(), f"{row['name']} leaked {leak!r}: {reason}"


def test_a_probe_exception_does_not_become_the_reason(client, monkeypatch):
    """The catch-all path is the one most likely to carry a stack-trace-ish string."""
    from app.services.lxd import status as lxd_status

    tc, _ = client

    def _raise(cluster_id, **kwargs):
        raise RuntimeError(
            "Vault read failed: 404 for url: https://vault.internal:8200/v1/orcastra/"
            "clusters/nuc2/certs"
        )

    monkeypatch.setattr(lxd_status, "probe_cluster", _raise)
    body = tc.get("/nodes/availability").json()
    assert body["total_count"] == len(ALL), "a raising probe must still name every cluster"
    for row in body["clusters"]:
        assert "vault" not in (row["reason"] or "").lower(), row
