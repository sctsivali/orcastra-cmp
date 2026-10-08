"""A location change must say in the audit trail what it replaced.

The location PATCH recorded only the new location in its `cluster.location.update` event, so
an operator reading the trail could see where a cluster was moved to but not where it had been.
The region backfill records both for the same action, and the two must read alike.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

OLD = {
    "region_id": "id-jkb", "region_name": "Jakarta Barat", "country_code": "ID",
    "country_name": "Indonesia", "city": "Jakarta Barat", "latitude": -6.1674,
    "longitude": 106.7637, "timezone": "Asia/Jakarta", "continent": "AS",
    "facility_name": "Rack Room 2",
}
NEW = {
    "region_id": "c-sg", "region_name": "Singapore", "country_code": "SG",
    "country_name": "Singapore", "city": "Singapore", "latitude": 1.29, "longitude": 103.85,
    "timezone": "Asia/Singapore", "continent": "AS", "facility_name": "Rack Room 2",
}


class _Store:
    def __init__(self, location):
        self.location = location
        self.saved = {}

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {"metadata": {"endpoint": "https://192.0.2.40:8443", "display_name": cluster_id,
                             "type": "LXD", "zone": "Jakarta Barat", "location": self.location}}

    def save_cluster_metadata(self, **kwargs):
        self.saved = kwargs
        return True


def _client(monkeypatch, store):
    from app.routers import cluster_registration as module
    from app.core.dependencies import check_read_only_mode
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    events = []
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    monkeypatch.setattr(module.activity_logger, "log_activity", lambda **kw: events.append(kw))
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), events


def test_the_event_carries_the_location_before_and_after(monkeypatch):
    store = _Store(dict(OLD))
    client, events = _client(monkeypatch, store)

    response = client.patch("/clusters/qa-audit/location", json={"location": NEW})
    assert response.status_code == 200, response.text

    (event,) = [e for e in events if e["action"] == "cluster.location.update"]
    assert event["before_state"] == OLD
    assert event["after_state"] == store.saved["location"]
    assert event["after_state"]["region_id"] == "c-sg"


def test_a_first_location_records_an_empty_before(monkeypatch):
    client, events = _client(monkeypatch, _Store({}))

    client.patch("/clusters/qa-audit/location", json={"location": NEW})

    (event,) = [e for e in events if e["action"] == "cluster.location.update"]
    assert event["before_state"] == {}


def test_a_refused_change_records_nothing(monkeypatch):
    store = _Store(dict(OLD))
    client, events = _client(monkeypatch, store)

    response = client.patch("/clusters/qa-audit/location",
                            json={"location": {**NEW, "country_code": "sg"}})
    assert response.status_code == 422, response.text
    assert events == [] and store.saved == {}
