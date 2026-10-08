"""The integration sync and /integrations/regions keep to the location keys they always sent.

A catalogue location carries three keys the integrator never had: `province`, `admin_code` and
`official_name`. The integrator stores what it is sent and renders it to buyers, so a new key is a
contract change, not a free addition. Both payloads pass the stored location through one
allowlist; this pins that the allowlist still holds them back.
"""

from typing import Any, Dict

from app.routers import integrations as module

EXTRAS = {"province", "admin_code", "official_name"}
LOCATION: Dict[str, Any] = {
    "region_id": "id-3174", "region_name": "Jakarta Selatan",
    "official_name": "Kota Administrasi Jakarta Selatan", "province": "Daerah Khusus Ibukota Jakarta",
    "admin_code": "31.74", "country_code": "ID", "country_name": "Indonesia",
    "city": "Jakarta Selatan", "latitude": -6.2615, "longitude": 106.8106,
    "timezone": "Asia/Jakarta", "continent": "AS", "facility_name": "Rack Room 7",
}


class _CertManager:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_cluster_info(self, cluster_id: str) -> Dict[str, Any]:
        return {"metadata": {"type": "LXD", "display_name": "Rack A", "location": dict(LOCATION)}}


def test_the_sync_row_holds_the_extras_back() -> None:
    (row,) = module._fetch_cluster_info(_CertManager(), "cluster-a")

    assert not EXTRAS & set(row), row
    assert not EXTRAS & set(row["location_data"]), row["location_data"]
    assert row["region_id"] == "id-3174" and row["city"] == "Jakarta Selatan"
    assert row["location_data"]["facility_name"] == "Rack Room 7"


def test_the_regions_rows_hold_the_extras_back(monkeypatch) -> None:
    monkeypatch.setattr("app.services.lxd_cert_manager.LXDCertificateManager", _CertManager)
    monkeypatch.setattr(module, "get_vault_client", lambda *args, **kwargs: object())

    regions = module._region_rows_blocking({"cluster-a"})

    (row,) = regions.values()
    assert not EXTRAS & set(row), row
    assert row["region_id"] == "id-3174" and row["node_count"] == 1
