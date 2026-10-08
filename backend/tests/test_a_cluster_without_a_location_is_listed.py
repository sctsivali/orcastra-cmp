"""A cluster registered without a location must read back as a cluster with no location.

Registering through the API without a location stores `location: {}` in the cluster's Vault
metadata. An empty dict is not a valid GeoLocation, whose nine fields are all required, so
building ClusterInfo from it raised: GET /clusters/{id} answered 500 and the list degraded the
row to endpoint "unknown" with status error. The form always sends a region, so only API callers
reached it, and the region backfill lists such clusters as "without a location" rather than
fixing them, so the read has to cope.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd_cert_manager import ConnectionProbe

BARE = "bare-api-cluster"
PLACED = "placed-cluster"

PLACED_LOCATION = {
    "region_id": "id-3273", "region_name": "Bandung", "country_code": "ID",
    "country_name": "Indonesia", "city": "Bandung", "latitude": -6.9147, "longitude": 107.6098,
    "timezone": "Asia/Jakarta", "continent": "AS", "province": "Jawa Barat",
}


class _CertManager:
    clusters_path = "secret/data/clusters"

    def list_clusters(self, use_cache=True):
        return [BARE, PLACED]

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        location = {} if cluster_id == BARE else dict(PLACED_LOCATION)
        return {
            "metadata": {"endpoint": "https://192.0.2.20:8443", "display_name": cluster_id,
                         "type": "LXD", "zone": "", "location": location},
            "certificate": {"has_certificate": True, "common_name": "orcastra"},
        }

    def get_cluster_certificate(self, cluster_id):
        return {"client.crt": "crt", "client.key": "key"}

    def probe_connection(self, cluster_id, use_breaker=True):
        return ConnectionProbe(False, False, "Cannot connect to LXD endpoint.")


class _ConnectionManager:
    def get_client(self, cluster_id, *args, **kwargs):
        return None

    def connect_with_vault_cert(self, cluster_id, **kwargs):
        from app.services.lxd.exceptions import LXDConnectionError
        raise LXDConnectionError(f"Cannot connect to {cluster_id}")


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


def test_the_list_keeps_the_real_row(client):
    row = _row(client, BARE)
    assert row["endpoint"] == "https://192.0.2.20:8443", row
    assert row["status"] != "error", row
    assert row["location"] is None


def test_the_single_read_answers_with_no_location(client):
    response = client.get(f"/clusters/{BARE}")
    assert response.status_code == 200, response.text
    assert response.json()["location"] is None


def test_a_placed_cluster_still_reads_its_location(client):
    assert _row(client, PLACED)["location"]["province"] == "Jawa Barat"
    single = client.get(f"/clusters/{PLACED}").json()
    assert single["location"]["region_id"] == "id-3273"
