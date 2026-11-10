"""A stored cluster location that is not a complete one reads as no location, everywhere.

Registering a cluster over the API without a location stores {} (lxd_certs/storage.py), and an
older or hand-edited record can hold null, part of a location, a string or a list. A reader that
takes the raw value fails: `GeoLocation(**partial)` raises, and `.get` on a null, string or list
crashes. These pin one rule through every reader, and that a server fault answers a fixed message
while the detail stays in the log.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import vault_client
from app.core.error_sanitizer import GENERIC_SERVER_ERROR
from app.models.cluster_status import ClusterStatus
from app.models.schemas import ClusterAuthCheckRequest, NodeUpdateRequest
from app.routers import cluster_registration, integrations, nodes, regions
from app.services import lxd_cert_manager
from app.services.lxd_cert_manager import ConnectionProbe
from app.services.lxd_certs import storage

SENTINEL = "SECRET-VALUE-7"
BAD_LOCATIONS = {
    # Coordinates but no country, city, timezone or continent.
    "partial": {
        "region_id": f"{SENTINEL}-region",
        "region_name": "Jakarta",
        "latitude": -6.2,
        "longitude": 106.8,
        "facility_name": f"{SENTINEL}-dc",
    },
    "null": None,
    "string": f"Jakarta {SENTINEL}",
    "list": [SENTINEL, -6.2, 106.8],
}
COMPLETE = {
    "region_id": "id-3174",
    "region_name": "Jakarta Selatan",
    "country_code": "ID",
    "country_name": "Indonesia",
    "city": "Jakarta Selatan",
    "latitude": -6.26,
    "longitude": 106.81,
    "timezone": "Asia/Jakarta",
    "continent": "AS",
    "facility_name": "DC One",
    "facility_provider": "Acme",
}
SHAPES = sorted(BAD_LOCATIONS)


def _metadata(location, **changes):
    values = {
        "endpoint": "https://host:8443",
        "display_name": "qa cluster",
        "type": "LXD",
        "location": location,
    }
    values.update(changes)
    return values


def _never_logged(caplog):
    assert all(SENTINEL not in record.getMessage() for record in caplog.records)


# --- The reader


@pytest.mark.parametrize("shape", SHAPES)
def test_an_incomplete_stored_location_reads_as_none(shape):
    metadata = _metadata(BAD_LOCATIONS[shape])

    assert storage.stored_location(f"qa-reader-{shape}", metadata) is None
    assert storage.stored_location_fields(f"qa-reader-{shape}", metadata) == {}


def test_a_complete_stored_location_reads_back_whole():
    location = storage.stored_location("qa-complete", _metadata(COMPLETE))

    assert location is not None
    assert location.region_id == "id-3174"
    assert location.latitude == -6.26
    fields = storage.stored_location_fields("qa-complete", _metadata(COMPLETE))
    assert fields["facility_name"] == "DC One"
    assert "province" not in fields


@pytest.mark.parametrize("metadata", [_metadata({}), {"endpoint": "https://host:8443"}])
def test_no_location_is_not_a_fault_and_logs_nothing(caplog, metadata):
    caplog.set_level(logging.WARNING, logger=storage.__name__)

    assert storage.stored_location("qa-no-location", metadata) is None
    assert caplog.records == []


def test_the_warning_names_the_cluster_and_the_missing_fields_never_the_value(caplog):
    caplog.set_level(logging.WARNING, logger=storage.__name__)

    storage.stored_location("qa-warn-partial", _metadata(BAD_LOCATIONS["partial"]))

    message = caplog.records[-1].getMessage()
    assert "qa-warn-partial" in message
    for field in ("country_code", "country_name", "city", "timezone", "continent"):
        assert field in message
    _never_logged(caplog)


@pytest.mark.parametrize("shape", ["null", "string", "list"])
def test_a_location_that_is_not_a_record_is_named_by_kind_never_by_value(caplog, shape):
    caplog.set_level(logging.WARNING, logger=storage.__name__)

    storage.stored_location(f"qa-warn-{shape}", _metadata(BAD_LOCATIONS[shape]))

    assert f"qa-warn-{shape}" in caplog.records[-1].getMessage()
    _never_logged(caplog)


def test_a_cluster_is_warned_about_once_not_on_every_read(caplog):
    caplog.set_level(logging.WARNING, logger=storage.__name__)

    for _ in range(3):
        storage.stored_location("qa-warn-once", _metadata(BAD_LOCATIONS["string"]))

    assert len(caplog.records) == 1


# --- /clusters, /clusters/{id} and /clusters/facilities


class _CertManager:
    clusters_path = "secret/data/clusters"

    def __init__(self, locations):
        self.locations = locations
        self.vault = SimpleNamespace(vault_read=lambda path: {"endpoint": "https://host:8443"})

    def list_clusters(self, use_cache=True):
        return list(self.locations)

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {
            "metadata": _metadata(self.locations[cluster_id], display_name=cluster_id),
            "certificate": {"has_certificate": True, "common_name": "orcastra"},
        }

    def get_cluster_certificate(self, cluster_id):
        return {"client.crt": "crt", "client.key": "key"}

    def probe_connection(self, cluster_id, use_breaker=True):
        return ConnectionProbe(True, True, "Certificate trusted and authenticated")


class _ConnectionManager:
    def get_cached_client(self, cluster_id):
        return None

    def get_client(self, cluster_id, *args, **kwargs):
        return None

    def connect_with_vault_cert(self, cluster_id, **kwargs):
        return None


def _api(monkeypatch, locations):
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    import app.services.lxd_service as lxd_service_module

    monkeypatch.setattr(
        lxd_service_module,
        "get_lxd_service",
        lambda: SimpleNamespace(connection_manager=_ConnectionManager()),
    )
    monkeypatch.setattr(cluster_registration, "apply_cluster_filter", lambda ids: ids)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    app = FastAPI()
    app.include_router(cluster_registration.router)
    app.dependency_overrides[cluster_registration.get_cert_manager] = lambda: _CertManager(
        locations
    )
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("shape", SHAPES)
def test_the_cluster_detail_answers_with_no_location(monkeypatch, shape):
    api = _api(monkeypatch, {"qa-bad": BAD_LOCATIONS[shape]})

    response = api.get("/clusters/qa-bad")

    assert response.status_code == 200, response.text
    assert response.json()["location"] is None


@pytest.mark.parametrize("shape", SHAPES)
def test_the_cluster_list_keeps_the_row_whole(monkeypatch, shape):
    api = _api(monkeypatch, {"qa-bad": BAD_LOCATIONS[shape], "qa-good": COMPLETE})

    rows = {row["cluster_id"]: row for row in api.get("/clusters").json()["clusters"]}

    assert rows["qa-bad"]["endpoint"] == "https://host:8443", rows["qa-bad"]
    assert rows["qa-bad"]["status"] != ClusterStatus.ERROR.value
    assert rows["qa-bad"]["location"] is None
    assert rows["qa-good"]["location"]["region_id"] == "id-3174"


@pytest.mark.parametrize("shape", SHAPES)
def test_facility_suggestions_come_only_from_readable_locations(monkeypatch, shape):
    api = _api(monkeypatch, {"qa-bad": BAD_LOCATIONS[shape], "qa-good": COMPLETE})

    response = api.get("/clusters/facilities")

    assert response.status_code == 200, response.text
    assert response.json() == [{"facility_name": "DC One", "facility_provider": "Acme"}]


# --- Integrations: the API-key cluster info and regions reads


@pytest.mark.parametrize("shape", SHAPES)
def test_the_integration_cluster_info_reports_no_location(shape):
    cert_manager = SimpleNamespace(
        get_cluster_info=lambda cluster_id: {"metadata": _metadata(BAD_LOCATIONS[shape])}
    )

    (info,) = integrations._fetch_cluster_info(cert_manager, "qa-bad")

    assert info["location_data"] is None
    assert info["region_id"] is None
    assert info["zone"] is None
    assert info["endpoint"] == "https://host:8443"


def test_the_integration_cluster_info_still_reports_a_complete_location():
    cert_manager = SimpleNamespace(
        get_cluster_info=lambda cluster_id: {"metadata": _metadata(COMPLETE)}
    )

    (info,) = integrations._fetch_cluster_info(cert_manager, "qa-good")

    assert info["region_id"] == "id-3174"
    assert info["zone"] == "Jakarta Selatan"


@pytest.mark.parametrize("shape", SHAPES)
def test_the_integration_regions_skip_an_unreadable_location(monkeypatch, shape):
    locations = {"qa-bad": BAD_LOCATIONS[shape], "qa-good": COMPLETE}
    cert_manager = SimpleNamespace(
        get_cluster_info=lambda cluster_id: {"metadata": _metadata(locations[cluster_id])}
    )

    async def none_unregistered(_ids):
        return frozenset()

    async def no_hardware(_service, _clusters):
        return {"cpu": 0, "memory": 0, "storage": 0, "gpu": 0, "gpu_cards": 0}

    monkeypatch.setattr(
        integrations,
        "get_settings",
        lambda: SimpleNamespace(
            vault_enabled=True,
            vault_token="t",
            vault_addr="http://vault.invalid",
            vault_clusters_path="secret/data/clusters",
        ),
    )
    monkeypatch.setattr(integrations, "get_vault_client", lambda addr, token: object())
    monkeypatch.setattr(
        lxd_cert_manager,
        "LXDCertificateManager",
        lambda vault_client, clusters_path: cert_manager,
    )
    monkeypatch.setattr(integrations, "_unregistered_cluster_ids", none_unregistered)
    monkeypatch.setattr(integrations, "_integration_hardware_totals", no_hardware)

    body = asyncio.run(
        integrations.get_integration_regions(
            api_key={"cluster_permissions": [{"cluster_id": cid} for cid in locations]},
            lxd_service=SimpleNamespace(),
        )
    )

    assert [region["region_id"] for region in body["regions"]] == ["id-3174"]
    assert body["total_nodes"] == 1


# --- The regions map


@pytest.mark.parametrize("shape", SHAPES)
def test_the_regions_map_places_an_unreadable_location_under_unknown(monkeypatch, shape):
    locations = {"qa-bad": BAD_LOCATIONS[shape], "qa-good": COMPLETE}
    cert_manager = SimpleNamespace(
        list_clusters=lambda use_cache=True: list(locations),
        get_cluster_info=lambda cluster_id: {
            "metadata": _metadata(locations[cluster_id], display_name=cluster_id)
        },
        get_cluster_certificate=lambda cluster_id: (_ for _ in ()).throw(
            RuntimeError("offline")
        ),
    )
    cache = SimpleNamespace(
        get=lambda *_args, **_kwargs: None,
        set=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(regions, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(regions, "get_resource_list_generation", lambda *_args: 0)
    monkeypatch.setattr(regions, "apply_cluster_filter", lambda ids: ids)
    monkeypatch.setattr(
        regions,
        "get_settings",
        lambda: SimpleNamespace(
            vault_enabled=True,
            vault_token="t",
            vault_addr="http://vault.invalid",
            vault_clusters_path="secret/data/clusters",
        ),
    )
    monkeypatch.setattr(vault_client, "get_vault_client", lambda addr, token: object())
    monkeypatch.setattr(
        lxd_cert_manager,
        "LXDCertificateManager",
        lambda vault_client, clusters_path: cert_manager,
    )
    offline = SimpleNamespace(
        connection_manager=SimpleNamespace(
            get_client=lambda cluster_id: None,
            connect_with_vault_cert=lambda **_kwargs: None,
        )
    )

    body = asyncio.run(regions.build_regions_overview(lxd_service=offline, fresh=True))

    placed = {
        node.cluster_id: region.region_id for region in body.regions for node in region.nodes
    }
    assert placed == {"qa-bad": "unknown", "qa-good": "id-3174"}


# --- A rename re-saves only a location the readers can use


@pytest.mark.parametrize("shape", SHAPES)
def test_a_rename_never_saves_an_unreadable_location_back(monkeypatch, shape):
    saved = []
    cert_manager = SimpleNamespace(
        get_cluster_info=lambda cluster_id: {"metadata": _metadata(BAD_LOCATIONS[shape])},
        save_cluster_metadata=lambda **kwargs: saved.append(kwargs),
    )
    _rename_harness(monkeypatch, cert_manager)

    asyncio.run(
        nodes.update_node(
            node_name="qa-bad",
            request=NodeUpdateRequest(display_name="QA renamed"),
            lxd_service=SimpleNamespace(get_node_names=lambda: ["qa-bad"]),
            _=None,
            _rbac=None,
        )
    )

    assert saved[0]["display_name"] == "QA renamed"
    assert saved[0]["location"] == {}


def test_a_rename_keeps_a_complete_location(monkeypatch):
    saved = []
    cert_manager = SimpleNamespace(
        get_cluster_info=lambda cluster_id: {"metadata": _metadata(COMPLETE)},
        save_cluster_metadata=lambda **kwargs: saved.append(kwargs),
    )
    _rename_harness(monkeypatch, cert_manager)

    asyncio.run(
        nodes.update_node(
            node_name="qa-good",
            request=NodeUpdateRequest(display_name="QA renamed"),
            lxd_service=SimpleNamespace(get_node_names=lambda: ["qa-good"]),
            _=None,
            _rbac=None,
        )
    )

    assert saved[0]["location"] == COMPLETE


def _rename_harness(monkeypatch, cert_manager):
    import app.core.node_metadata as node_metadata

    monkeypatch.setattr(nodes, "verify_cluster_access", lambda _cluster_id: None)
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: SimpleNamespace(
            vault_enabled=True,
            vault_token="t",
            vault_addr="http://vault.invalid",
            vault_clusters_path="secret/data/clusters",
        ),
    )
    monkeypatch.setattr(nodes, "get_vault_client", lambda addr, token: object())
    monkeypatch.setattr(
        lxd_cert_manager,
        "LXDCertificateManager",
        lambda vault_client, clusters_path: cert_manager,
    )
    monkeypatch.setattr(
        node_metadata,
        "get_node_metadata_store",
        lambda: SimpleNamespace(set_node_display_name=lambda *_args: None),
    )
    monkeypatch.setattr(nodes, "get_cache_manager", lambda: None)


# --- A server fault answers a fixed message; the detail goes to the log


class _FaultyCertManager:
    def list_clusters(self, use_cache=True):
        raise RuntimeError(f"vault said {SENTINEL} at /v1/secret/data/clusters")

    def get_cluster_info(self, cluster_id, include_cert_content=False):
        raise RuntimeError(f"vault said {SENTINEL} at /v1/secret/data/clusters/{cluster_id}")

    def probe_connection(self, cluster_id, use_breaker=True):
        raise RuntimeError(f"probe said {SENTINEL}")


def _logged_in_full(caplog):
    return any(SENTINEL in record.getMessage() for record in caplog.records)


def test_the_cluster_detail_answers_a_server_fault_without_its_text(monkeypatch, caplog):
    monkeypatch.setattr(cluster_registration, "verify_cluster_access", lambda _cluster_id: None)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            cluster_registration.get_cluster("qa-faulty", cert_manager=_FaultyCertManager())
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == GENERIC_SERVER_ERROR
    assert _logged_in_full(caplog)


def test_the_cluster_list_answers_a_server_fault_without_its_text(monkeypatch, caplog):
    api = _api(monkeypatch, {})
    api.app.dependency_overrides[cluster_registration.get_cert_manager] = _FaultyCertManager

    response = api.get("/clusters?fresh=true")

    assert response.status_code == 500
    assert response.json()["detail"] == GENERIC_SERVER_ERROR
    assert _logged_in_full(caplog)


def test_a_list_row_that_cannot_be_read_does_not_carry_the_fault_text(monkeypatch, caplog):
    _api(monkeypatch, {})

    row = cluster_registration._cluster_info_blocking("qa-faulty", _FaultyCertManager())

    assert row.status == ClusterStatus.ERROR
    assert SENTINEL not in (row.error_message or "")
    assert "/v1/" not in (row.error_message or "")
    assert _logged_in_full(caplog)


def test_check_auth_answers_a_server_fault_without_its_text(monkeypatch, caplog):
    monkeypatch.setattr(cluster_registration, "verify_cluster_access", lambda _cluster_id: None)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            cluster_registration.check_cluster_authentication(
                request=ClusterAuthCheckRequest(cluster_id="qa-faulty"),
                cert_manager=_FaultyCertManager(),
                _rbac=None,
            )
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == GENERIC_SERVER_ERROR
    assert _logged_in_full(caplog)
