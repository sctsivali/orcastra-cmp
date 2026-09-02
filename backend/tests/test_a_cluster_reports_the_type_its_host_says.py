"""The clusters endpoints must report what the host says now, not what registration guessed.

`/clusters` read the type string stored at registration time and normalised it. That string was
produced by an unauthenticated probe that cannot see `environment`, so it was "LXD" for every
LXD-family cluster, forever, unless an operator clicked Check Authentication. A three-node
MicroCloud sat in the list badged LXD while its own /1.0 reported server_clustered=true.

These drive the two read endpoints through a fake connection pool whose client carries a real
host_info body, and assert the response follows the host: the live payload outranks the stored
string, clustering is reported as its own fact, and an unauthenticated cluster reports neither
rather than defaulting.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.cluster_status import ClusterStatus
from app.services.lxd_cert_manager import ConnectionProbe

MICROCLOUD = "central2"
STANDALONE = "pandora"
UNTRUSTED = "vdc-hetzner"
NO_LOCATION = "qa-no-location"

HOST_INFO = {
    MICROCLOUD: {
        "auth": "trusted",
        "api_extensions": ["clustering"],
        "environment": {
            "server": "lxd",
            "server_clustered": True,
            "server_name": "microcloud30",
        },
    },
    "qa-no-location": {
        "auth": "trusted",
        "api_extensions": [],
        "environment": {"server": "lxd", "server_clustered": False, "server_name": "nowhere"},
    },
    STANDALONE: {
        "auth": "trusted",
        "api_extensions": [],
        "environment": {
            "server": "lxd",
            "server_clustered": False,
            "server_name": "pandora",
        },
    },
}


class _Client:
    def __init__(self, host_info):
        self.host_info = host_info


class _Vault:
    def vault_read(self, path):
        return {"endpoint": "https://host:8443"}


class _CertManager:
    clusters_path = "secret/data/clusters"

    def __init__(self):
        self.vault = _Vault()

    def list_clusters(self, use_cache=True):
        return [MICROCLOUD, STANDALONE, UNTRUSTED, NO_LOCATION]

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {
            # The stale registration-time guess every one of these carries.
            "metadata": {
                "endpoint": "https://host:8443",
                "display_name": cluster_id,
                "type": "LXD",
                # What save_cluster_metadata writes when no region was chosen.
                "location": {},
            },
            "certificate": {"has_certificate": True, "common_name": "orcastra"},
        }

    def get_cluster_certificate(self, cluster_id):
        return {"client.crt": "crt", "client.key": "key"}

    def probe_connection(self, cluster_id, use_breaker=True):
        if cluster_id == UNTRUSTED:
            return ConnectionProbe(True, False, "Certificate not trusted by hypervisor.")
        return ConnectionProbe(True, True, "Certificate trusted and authenticated")


class _ConnectionManager:
    def get_client(self, cluster_id, *args, **kwargs):
        info = HOST_INFO.get(cluster_id)
        return _Client(info) if info else None

    def connect_with_vault_cert(self, cluster_id, **kwargs):
        # The real one raises rather than returning None when the host refuses the cert.
        if cluster_id == UNTRUSTED:
            from app.services.lxd.exceptions import LXDAuthorizationError
            raise LXDAuthorizationError(f"Certificate not trusted by {cluster_id}")
        return self.get_client(cluster_id)

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


def _row(client, cluster_id):
    body = client.get("/clusters").json()
    return next(c for c in body["clusters"] if c["cluster_id"] == cluster_id)


def test_the_list_follows_the_host_over_the_stored_guess(client):
    row = _row(client, MICROCLOUD)
    assert row["cluster_type"] == "MicroCloud", row
    assert row["clustered"] is True, row
    assert row["status"] == ClusterStatus.ONLINE.value, row


def test_a_standalone_host_is_reported_lxd_and_not_clustered(client):
    row = _row(client, STANDALONE)
    assert row["cluster_type"] == "LXD", row
    assert row["clustered"] is False, row


def test_a_stored_clustering_state_unlocks_the_stored_type(client, monkeypatch):
    """An offline host that proved both facts keeps reporting both.

    The two are gated differently and the marker is the reason. A stored `type` on its own can
    still be the registration-time guess this fixture carries, so an unauthenticated cluster
    must not report it. A stored `clustered` can only have been written by check-auth, which
    writes the family in the same statement, so its presence is the evidence that the stored
    type came off a host rather than out of a default.
    """
    original = _CertManager.get_cluster_info

    def with_clustering(self, cluster_id, include_cert_content=False):
        info = original(self, cluster_id, include_cert_content)
        info["metadata"]["type"] = "MicroCloud"
        info["metadata"]["clustered"] = True
        return info

    monkeypatch.setattr(_CertManager, "get_cluster_info", with_clustering)

    row = _row(client, UNTRUSTED)
    assert row["clustered"] is True, row
    assert row["cluster_type"] == "MicroCloud", row


def test_an_untrusted_cluster_reports_neither_rather_than_defaulting(client):
    row = _row(client, UNTRUSTED)
    assert row["cluster_type"] is None, row
    assert row["clustered"] is None, row
    assert row["status"] == ClusterStatus.NOT_AUTHORIZED.value, row


def test_the_single_cluster_endpoint_agrees_with_the_list(client):
    listed = _row(client, MICROCLOUD)
    single = client.get(f"/clusters/{MICROCLOUD}").json()
    assert single["cluster_type"] == listed["cluster_type"], (single, listed)
    assert single["clustered"] == listed["clustered"], (single, listed)


def test_a_cluster_without_a_location_does_not_500(client):
    """An empty location dict is "no location", not an invalid GeoLocation.

    save_cluster_metadata stores {} when a caller registers without one. GeoLocation requires
    all nine of its fields, so building ClusterInfo straight from that raised nine validation
    errors: a 500 on the single-cluster read, and on the list a row silently degraded to
    endpoint="unknown" by the handler's own except branch. Only API callers hit it, because
    the register form always sends a region.
    """
    single = client.get(f"/clusters/{NO_LOCATION}")
    assert single.status_code == 200, single.text
    assert single.json()["location"] is None, single.json()

    row = _row(client, NO_LOCATION)
    assert row["endpoint"] != "unknown", row
    assert row["status"] != ClusterStatus.ERROR.value, row
