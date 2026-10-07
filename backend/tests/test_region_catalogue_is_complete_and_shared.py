"""The region catalogue behind the location picker and the regions map.

The picker used to offer 98 kota under a 34-province map, so 416 kabupaten could not be chosen
at all, Sorong sat in a province it left in 2022, and the IKN point was 35 km from Nusantara.
These hold the regenerated catalogue to what it claims: every kabupaten and kota of the 2025
Kepmendagri list, one entry per country, every id the old catalogue shipped still resolving,
and the backend reading exactly the file the frontend bundles.
"""

import collections
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.region_catalog import CATALOG_PATH, get_region_catalog

REPO = Path(__file__).resolve().parents[2]
FRONTEND_COPY = REPO / "frontend" / "src" / "lib" / "regions" / "catalog.json"
LEGACY_SNAPSHOT = REPO / "scripts" / "regions" / "legacy_regions.json"
CONTINENTS = {"AF", "AN", "AS", "EU", "NA", "OC", "SA"}


@pytest.fixture(scope="module")
def raw():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _indonesia(raw):
    return [r for r in raw["regions"] if r["country_code"] == "ID"]


def test_every_kabupaten_and_kota_of_the_2025_list_is_selectable(raw):
    regencies = _indonesia(raw)
    kinds = collections.Counter(r["official_name"].split(" ")[0] for r in regencies)
    assert len(regencies) == 514, len(regencies)
    assert kinds == {"Kabupaten": 416, "Kota": 98}, kinds
    provinces = {r["province"] for r in regencies}
    assert len(provinces) == 38, sorted(provinces)
    for new_province in ("Papua Selatan", "Papua Tengah", "Papua Pegunungan", "Papua Barat Daya"):
        assert new_province in provinces, new_province
    assert all(r["id"] == "id-" + r["admin_code"].replace(".", "") for r in regencies)


def test_outside_indonesia_there_is_one_entry_per_country(raw):
    countries = [r for r in raw["regions"] if r["country_code"] != "ID"]
    codes = [r["country_code"] for r in countries]
    assert len(codes) == len(set(codes)), "a country appears twice"
    assert len(countries) >= 240, len(countries)
    assert all(r["id"] == "c-" + r["country_code"].lower() for r in countries)
    assert all("province" not in r for r in countries)


def test_names_drop_the_kind_except_where_a_kabupaten_and_kota_share_one(raw):
    regencies = _indonesia(raw)
    names = [r["name"] for r in regencies]
    assert len(names) == len(set(names)), "two regencies display the same name"
    assert not any(n.startswith(("Kabupaten ", "Kota ")) for n in names)
    by_id = {r["id"]: r for r in regencies}
    assert by_id["id-3201"]["name"] == "Bogor (Regency)" and by_id["id-3271"]["name"] == "Bogor (City)"
    assert by_id["id-3205"]["name"] == "Garut" and by_id["id-3205"]["official_name"] == "Kabupaten Garut"


def test_every_entry_is_well_formed(raw):
    entries = raw["regions"] + raw["legacy"]
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for e in entries:
        assert -90 <= e["latitude"] <= 90 and -180 <= e["longitude"] <= 180, e
        assert (e["latitude"], e["longitude"]) != (0, 0), e  # the backend's "no location" marker
        assert e["continent"] in CONTINENTS, e
        ZoneInfo(e["timezone"])
        assert e["name"].strip() == e["name"] and e["city"].strip() == e["city"], e


@pytest.mark.parametrize(
    "region_id, name, province, south, west, north, east",
    [
        # bounding boxes drawn independently of the build, from the places themselves
        ("id-3174", "Jakarta Selatan", "Daerah Khusus Ibukota Jakarta", -6.37, 106.75, -6.20, 106.88),
        # the source row put Wakatobi at longitude 23.5, in Africa
        ("id-7407", "Wakatobi", "Sulawesi Tenggara", -6.5, 123.3, -5.0, 124.3),
        ("id-9671", "Sorong (City)", "Papua Barat Daya", -1.0, 131.1, -0.7, 131.5),
        ("id-9503", "Yahukimo", "Papua Pegunungan", -5.2, 139.2, -4.5, 139.8),
        ("id-1602", "Ogan Komering Ilir", "Sumatera Selatan", -3.8, 104.5, -3.0, 105.2),
    ],
)
def test_known_places_are_where_they_should_be(region_id, name, province, south, west, north, east):
    entry = get_region_catalog().resolve(region_id)
    assert entry is not None, region_id
    assert entry["name"] == name and entry["province"] == province, entry
    assert south <= entry["latitude"] <= north and west <= entry["longitude"] <= east, entry


def test_every_id_the_old_catalogue_shipped_still_resolves():
    catalog = get_region_catalog()
    old = json.loads(LEGACY_SNAPSHOT.read_text()) if LEGACY_SNAPSHOT.exists() else []
    if not old:
        pytest.skip("legacy snapshot is not in this checkout")
    assert len(old) == 100
    for row in old:
        entry = catalog.resolve(row["id"])
        assert entry is not None, row["id"]
        assert entry["country_code"] == "ID", entry
    assert catalog.resolve("id-jks")["id"] == "id-3174"
    assert catalog.resolve("id-sor")["province"] == "Papua Barat Daya"
    ikn = catalog.resolve("id-ikn")
    assert -1.1 <= ikn["latitude"] <= -0.85 and 116.6 <= ikn["longitude"] <= 116.8, ikn


def test_every_alias_points_at_a_selectable_entry(raw):
    selectable = {r["id"] for r in raw["regions"]}
    assert raw["aliases"], "no aliases"
    for old, new in raw["aliases"].items():
        assert new in selectable, (old, new)
        assert old not in selectable, old


def test_the_backend_reads_the_file_the_frontend_bundles():
    if not FRONTEND_COPY.exists():
        pytest.skip("frontend tree is not in this checkout")
    assert FRONTEND_COPY.read_bytes() == CATALOG_PATH.read_bytes(), (
        "the two catalogue copies differ; rebuild with scripts/regions/build_regions.py")
