"""A catalogue region is stored as the catalogue has it, whatever the request carried.

The client used to send the coordinates, names and time zone of the region it picked, and the
server stored them verbatim. A stale browser bundle, a script, or a hand-written request could
therefore save "id-3174" (Jakarta Selatan) at a point in the sea, and nothing downstream could
tell. Now a known region id is resolved on the server, a retired id is rewritten to the one that
replaced it, and only an id the catalogue does not know (an API client's own) passes through.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models.schemas import GeoLocation, GeoLocationInput
from app.services.region_catalog import get_region_catalog

STALE_JAKARTA_SELATAN = {
    "region_id": "id-jks",
    "region_name": "Jakarta Selatan",
    "country_code": "ID",
    "country_name": "Indonesia",
    "city": "Jakarta Selatan",
    "latitude": -6.2615,
    "longitude": 106.8106,
    "timezone": "Asia/Jakarta",
    "continent": "AS",
    "facility_name": "Main DC",
    "facility_provider": "Acme",
}


def test_a_retired_id_is_rewritten_with_catalogue_fields_and_the_facility_kept():
    out = get_region_catalog().canonical_location(STALE_JAKARTA_SELATAN)
    entry = get_region_catalog().resolve("id-3174")
    assert out["region_id"] == "id-3174"
    assert out["region_name"] == "Jakarta Selatan"
    assert out["official_name"] == "Kota Administrasi Jakarta Selatan"
    assert out["province"] == "Daerah Khusus Ibukota Jakarta" and out["admin_code"] == "31.74"
    assert (out["latitude"], out["longitude"]) == (entry["latitude"], entry["longitude"])
    assert out["facility_name"] == "Main DC" and out["facility_provider"] == "Acme"


def test_coordinates_sent_with_a_catalogue_id_are_not_trusted():
    forged = {**STALE_JAKARTA_SELATAN, "region_id": "id-3174", "latitude": -60.0, "longitude": 10.0, "city": "Nowhere"}
    out = get_region_catalog().canonical_location(forged)
    assert -6.4 < out["latitude"] < -6.2 and 106.7 < out["longitude"] < 106.9, out
    assert out["city"] == "Jakarta Selatan", out


def test_a_country_entry_carries_no_province():
    out = get_region_catalog().canonical_location({**STALE_JAKARTA_SELATAN, "region_id": "c-sg", "province": "stale"})
    assert out["country_code"] == "SG" and out["region_name"] == "Singapore"
    assert "province" not in out and "admin_code" not in out and "official_name" not in out, out


def test_a_client_defined_region_passes_through_untouched():
    custom = {**STALE_JAKARTA_SELATAN, "region_id": "us-east-dc1", "region_name": "US East", "country_code": "US"}
    assert get_region_catalog().canonical_location(custom) == custom


@pytest.mark.parametrize("field, value", [
    ("region_id", "../etc"),
    ("region_id", "has space"),
    ("region_id", "x" * 65),
    ("latitude", 91),
    ("longitude", -181),
    ("latitude", float("nan")),
    ("continent", "asia"),
    ("country_code", "idn"),
    ("admin_code", "3174"),
    ("city", ""),
    ("facility_name", "x" * 201),
])
def test_bad_input_is_refused_at_the_boundary(field, value):
    with pytest.raises(ValidationError):
        GeoLocationInput(**{**STALE_JAKARTA_SELATAN, field: value})


def test_stored_locations_still_read_back_without_the_input_constraints():
    # a location saved before the constraints existed must not turn a read into a 500
    GeoLocation(**{**STALE_JAKARTA_SELATAN, "region_id": "legacy id with spaces", "continent": "Asia"})


class _Store:
    def __init__(self):
        self.saved = {}

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {"metadata": {"endpoint": "https://host:8443", "display_name": cluster_id, "type": "LXD",
                             "zone": "Jakarta Selatan", "location": STALE_JAKARTA_SELATAN}}

    def save_cluster_metadata(self, **kwargs):
        self.saved = kwargs
        return True


@pytest.fixture
def patch_client(monkeypatch):
    from app.routers import cluster_registration as module
    from app.core.dependencies import check_read_only_mode
    from app.core.rbac import get_current_user, require_partner
    from app.core.org_scope import org_scope_dependency

    store = _Store()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_partner()] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), store


def test_the_location_endpoint_stores_and_returns_the_catalogue_entry(patch_client):
    client, store = patch_client
    forged = {**STALE_JAKARTA_SELATAN, "latitude": 1.0, "longitude": 1.0}
    response = client.patch("/clusters/qa-loc/location", json={"location": forged})
    assert response.status_code == 200, response.text
    body = response.json()["location"]
    assert body["region_id"] == "id-3174" and body["province"] == "Daerah Khusus Ibukota Jakarta", body
    assert -6.4 < body["latitude"] < -6.2, body

    stored = store.saved["location"]
    assert stored == {k: v for k, v in body.items() if v is not None}, (stored, body)
    assert store.saved["zone"] == stored["city"] == "Jakarta Selatan"
    assert stored["facility_name"] == "Main DC"


def test_the_location_endpoint_refuses_a_malformed_region_id(patch_client):
    client, store = patch_client
    response = client.patch("/clusters/qa-loc/location", json={"location": {**STALE_JAKARTA_SELATAN, "region_id": "a b"}})
    assert response.status_code == 422, response.text
    assert store.saved == {}
