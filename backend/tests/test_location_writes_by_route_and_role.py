"""Register and the location PATCH store catalogue regions, and only the right roles may write.

The catalogue tests cover canonical_location itself and the PATCH with an all-powerful dev user.
These drive the two routes the way callers reach them: register with a retired id, a country, a
client-defined id and malformed input, and the PATCH as a Tenant, as a Partner on a cluster
outside its organizations, as a Partner on its own cluster and as an Admin. A refused write must
leave no Vault write, no database row and no audit event behind.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser

FORGED_JAKARTA_SELATAN = {
    "region_id": "id-jks", "region_name": "Somewhere Else", "country_code": "ID",
    "country_name": "Indonesia", "city": "Nowhere", "latitude": 1.0, "longitude": 1.0,
    "timezone": "Asia/Jakarta", "continent": "AS",
    "facility_name": "Rack Room 7", "facility_provider": "QA Provider",
}
JAPAN = {
    "region_id": "c-jp", "region_name": "Japan", "country_code": "JP", "country_name": "Japan",
    "city": "Tokyo", "latitude": 35.68, "longitude": 139.69, "timezone": "Asia/Tokyo",
    "continent": "AS",
}
CLIENT_DEFINED = {
    "region_id": "acme-dc-7", "region_name": "Acme DC 7", "country_code": "DE",
    "country_name": "Germany", "city": "Frankfurt", "latitude": 50.11, "longitude": 8.68,
    "timezone": "Europe/Berlin", "continent": "EU",
}


class _NoCertificateYet:
    """Vault for a cluster registered for the first time: nothing at its certs path."""

    def vault_read(self, path, use_cache=True):
        from app.core.vault_client import VaultClientError

        raise VaultClientError(f"404 {path}")


class _Cert:
    # Registration reads the cluster's current certificate first, to record it if it replaces one.
    clusters_path = "secret/data/clusters"

    def __init__(self):
        self.saved = []
        self.vault = _NoCertificateYet()

    def save_cluster_metadata(self, **kwargs):
        self.saved.append(kwargs)
        return True

    def save_cluster_secrets(self, **kwargs):
        return True

    def read_registry(self, use_cache=True, **_kwargs):
        # Registration reads the registry from Vault to decide which generation it starts.
        from types import SimpleNamespace

        return SimpleNamespace(source="vault", cluster_ids=[])

    def generate_and_store_certificate(self, **kwargs):
        return {"cert": "-----BEGIN CERTIFICATE-----\nQA\n-----END CERTIFICATE-----"}

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {"metadata": {"endpoint": "https://192.0.2.50:8443", "display_name": cluster_id,
                             "type": "LXD", "zone": "Jakarta", "location": {}}}


class _Result:
    def scalar_one_or_none(self):
        return None

    def all(self):
        return []


class _Db:
    def __init__(self):
        self.added = []

    async def execute(self, *args, **kwargs):
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None


class _Store:
    def set_node_display_name(self, *args):
        return None

    def set_node_type(self, *args):
        return None


class _ConnectionManager:
    def register_cluster(self, *args, **kwargs):
        return None


class _Service:
    connection_manager = _ConnectionManager()


def _user(role: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"qa-{role}", email=f"qa-{role}@example.test",
                             groups=[f"role_{role}"])


def _client(monkeypatch, user=None, allowed=None):
    from app.routers import cluster_registration as module
    from app.core.database import get_db
    from app.core.dependencies import check_read_only_mode
    from app.core.org_scope import org_scope_dependency, set_allowed_clusters
    from app.core.rbac import get_current_user

    cert, db, events = _Cert(), _Db(), []

    async def detect(endpoint, timeout=5.0):
        return "LXD", {}

    async def org_ids(user, db):
        return [7]

    async def scope():
        set_allowed_clusters(allowed)

    monkeypatch.setattr(module, "detect_hypervisor_type_from_endpoint", detect)
    monkeypatch.setattr(module.activity_logger, "log_activity", lambda **kw: events.append(kw))
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    monkeypatch.setattr("app.core.node_metadata.get_node_metadata_store", lambda: _Store())
    monkeypatch.setattr("app.services.lxd_service.get_lxd_service", lambda: _Service())
    monkeypatch.setattr("app.core.org_scope.get_user_org_ids", org_ids)

    async def nobody_else_holds(*_args, **_kwargs):
        return set()

    # Moving a cluster needs the whole cluster; who holds it is pinned on real rows in
    # test_whole_cluster_state_needs_the_whole_cluster.py.
    monkeypatch.setattr("app.services.cluster_parties.clusters_held_by_another_party", nobody_else_holds)

    async def nothing_bought(*_args, **_kwargs):
        return set()

    async def not_bought(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.core.sale_term.purchase_only_clusters", nothing_bought)
    monkeypatch.setattr("app.core.sale_term.reaches_only_by_purchase", not_bought)

    async def no_generation(*_args, **_kwargs):
        return None

    # The registration generation is its own test (test_a_grant_reaches_only_the_registration_it_was_given_for.py).
    monkeypatch.setattr(module.cluster_generations, "begin_registration", no_generation)

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = lambda: cert
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope_dependency] = scope
    app.dependency_overrides[check_read_only_mode] = lambda: None
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False), cert, db, events


def _register(client, location, cluster_id="qa-hf3-reg"):
    return client.post("/clusters/register", json={
        "cluster_id": cluster_id, "display_name": "QA register",
        "endpoint": "https://192.0.2.50:8443", "location": location,
    })


def test_register_stores_the_catalogue_entry_for_a_retired_id(monkeypatch):
    client, cert, db, _ = _client(monkeypatch)
    response = _register(client, FORGED_JAKARTA_SELATAN)
    assert response.status_code == 201, response.text

    stored = cert.saved[0]["location"]
    assert stored["region_id"] == "id-3174"
    assert stored["region_name"] == "Jakarta Selatan"
    assert stored["province"] == "Daerah Khusus Ibukota Jakarta"
    assert stored["admin_code"] == "31.74" and stored["official_name"]
    assert -6.4 < stored["latitude"] < -6.2 and 106.7 < stored["longitude"] < 106.9, stored
    assert stored["facility_name"] == "Rack Room 7"
    assert stored["facility_provider"] == "QA Provider"
    assert len(db.added) == 1, "the cluster is still bound to the registrant's organization"


def test_register_stores_a_country_without_province_keys(monkeypatch):
    client, cert, _, _ = _client(monkeypatch)
    assert _register(client, JAPAN).status_code == 201
    stored = cert.saved[0]["location"]
    assert stored["region_id"] == "c-jp" and stored["country_code"] == "JP"
    assert not {"province", "admin_code", "official_name"} & set(stored), stored


def test_register_keeps_a_client_defined_region_as_sent(monkeypatch):
    client, cert, _, _ = _client(monkeypatch)
    assert _register(client, CLIENT_DEFINED).status_code == 201
    assert cert.saved[0]["location"] == CLIENT_DEFINED


@pytest.mark.parametrize("field,value", [
    ("region_id", "../etc"),
    ("region_id", "x" * 65),
    ("continent", "asia"),
    ("country_code", "idn"),
    ("city", ""),
    ("latitude", 91),
    ("facility_name", "f" * 201),
    ("city", "Jkt\nFORGED LOG LINE"),
    ("region_name", "\x1b[31mred"),
])
def test_register_refuses_malformed_input_and_writes_nothing(monkeypatch, field, value):
    client, cert, db, events = _client(monkeypatch)
    response = _register(client, {**JAPAN, field: value})
    assert response.status_code == 422, response.text
    assert cert.saved == [] and db.added == [] and events == []


def _patch(client, cluster_id="qa-hf3-own"):
    return client.patch(f"/clusters/{cluster_id}/location", json={"location": JAPAN})


def test_a_tenant_cannot_move_a_cluster(monkeypatch):
    client, cert, _, events = _client(monkeypatch, user=_user("tenant"), allowed={"qa-hf3-own"})
    response = _patch(client)
    assert response.status_code == 403, response.text
    assert cert.saved == []
    assert not [e for e in events if e["action"] == "cluster.location.update"]


def test_a_partner_cannot_move_a_cluster_outside_its_organizations(monkeypatch):
    client, cert, _, events = _client(monkeypatch, user=_user("partner"), allowed={"qa-hf3-own"})
    response = _patch(client, cluster_id="someone-elses-cluster")
    assert response.status_code == 403, response.text
    assert cert.saved == [] and events == []


def test_a_partner_moves_its_own_cluster(monkeypatch):
    client, cert, _, _ = _client(monkeypatch, user=_user("partner"), allowed={"qa-hf3-own"})
    assert _patch(client).status_code == 200
    assert cert.saved[0]["location"]["region_id"] == "c-jp"


def test_an_admin_moves_any_cluster(monkeypatch):
    client, cert, _, _ = _client(monkeypatch, user=_user("admin"), allowed=None)
    assert _patch(client, cluster_id="someone-elses-cluster").status_code == 200
    assert cert.saved[0]["location"]["region_id"] == "c-jp"
