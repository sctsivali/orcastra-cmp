"""The integration sync and /integrations/regions send only the allowlisted location keys.

A catalogue location carries three keys outside the integrator contract: `province`, `admin_code`
and `official_name`. The integrator stores what it is sent and renders it to buyers, so a new key
is a contract change. Both payloads pass the stored location through one allowlist, which must
keep holding these back.
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


def test_the_regions_rows_hold_the_extras_back() -> None:
    """/integrations/regions builds its rows from the same allowlisted location."""
    public = module._public_location_data(dict(LOCATION))

    assert not EXTRAS & set(public), public
    assert public["region_id"] == "id-3174" and public["facility_name"] == "Rack Room 7"
