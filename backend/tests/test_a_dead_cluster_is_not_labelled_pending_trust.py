"""A powered-off cluster must not read as a certificate problem.

Reporting every failed connect as NOT_AUTHORIZED ("Pending Trust" on the Clusters page)
would send an operator to rotate a certificate on a host that is not answering.
connect_with_vault_cert raises LXDAuthorizationError for "answered and refused us" and
LXDConnectionError for "never answered", so /clusters can report OFFLINE for the second.
These tests pin both halves.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.cluster_status import ClusterStatus
from app.services.lxd_cert_manager import ConnectionProbe

DEAD = "blitzkrieg"
UNTRUSTED = "central1-argon"


class _Vault:
    def vault_read(self, path):
        return {"endpoint": "https://host:8443"}


class _CertManager:
    """Answers like the real one, but from a fixture instead of a socket."""

    clusters_path = "secret/data/clusters"

    def __init__(self):
        self.vault = _Vault()

    def list_clusters(self, use_cache=True):
        return [DEAD, UNTRUSTED]

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {
            "metadata": {"endpoint": "https://host:8443", "display_name": cluster_id,
                         "type": "LXD"},
            "certificate": {"has_certificate": True, "common_name": "orcastra"},
        }

    def get_cluster_certificate(self, cluster_id):
        return {"client.crt": "crt", "client.key": "key"}

    def probe_connection(self, cluster_id, use_breaker=True):
        if cluster_id == UNTRUSTED:
            return ConnectionProbe(True, False, "Certificate not trusted by hypervisor.")
        return ConnectionProbe(False, False, "Cannot connect to LXD endpoint.")


class _ConnectionManager:
    def get_client(self, cluster_id, *args, **kwargs):
        return None

    def connect_with_vault_cert(self, cluster_id, **kwargs):
        from app.services.lxd.exceptions import LXDAuthorizationError, LXDConnectionError
        if cluster_id == UNTRUSTED:
            raise LXDAuthorizationError(f"Certificate not trusted by {cluster_id}")
        raise LXDConnectionError(f"Cannot connect to {cluster_id}")

    def register_cluster(self, *args, **kwargs):
        return None


class _Service:
    def __init__(self):
        self.connection_manager = _ConnectionManager()


@pytest.fixture
def client(monkeypatch):
    from app.routers import cluster_registration as module
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency
    import app.services.lxd_service as lxd_service_module

    monkeypatch.setattr(lxd_service_module, "get_lxd_service", lambda: _Service())
    monkeypatch.setattr(module, "apply_cluster_filter", lambda ids: ids)
    monkeypatch.setattr(module, "get_cache_manager", lambda: None, raising=False)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = _CertManager
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_a_cluster_that_never_answered_is_offline(client):
    body = client.get("/clusters").json()
    row = next(c for c in body["clusters"] if c["cluster_id"] == DEAD)
    assert row["status"] == ClusterStatus.OFFLINE.value, row


def test_a_cluster_that_answered_and_refused_us_is_pending_trust(client):
    body = client.get("/clusters").json()
    row = next(c for c in body["clusters"] if c["cluster_id"] == UNTRUSTED)
    assert row["status"] == ClusterStatus.NOT_AUTHORIZED.value, row


def test_the_two_failures_are_not_the_same_status(client):
    """An unreachable cluster and an untrusted one never share a status."""
    body = client.get("/clusters").json()
    statuses = {c["cluster_id"]: c["status"] for c in body["clusters"]}
    assert statuses[DEAD] != statuses[UNTRUSTED]


def test_an_offline_cluster_says_why(client):
    body = client.get("/clusters").json()
    row = next(c for c in body["clusters"] if c["cluster_id"] == DEAD)
    assert row["error_message"], "an operator needs to know what failed"


def test_the_single_cluster_endpoint_agrees_with_the_list(client):
    """Two pages describing one cluster must not disagree about whether it is down."""
    listed = {c["cluster_id"]: c["status"] for c in client.get("/clusters").json()["clusters"]}
    for cluster_id in (DEAD, UNTRUSTED):
        single = client.get(f"/clusters/{cluster_id}").json()
        assert single["status"] == listed[cluster_id], cluster_id
