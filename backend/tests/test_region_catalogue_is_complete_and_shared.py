"""The region catalogue behind the location picker and the regions map.

The catalogue holds every kabupaten and kota of the 2025 Kepmendagri list across 38 provinces and
one entry per country standing for the whole country. Every id of the legacy catalogue still
resolves, and the backend reads exactly the file the frontend bundles.
"""

import collections
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.region_catalog import CATALOG_PATH, get_region_catalog

REPO = Path(__file__).resolve().parents[2]
FRONTEND_COPY = REPO / "frontend" / "src" / "lib" / "regions" / "catalog.json"
COUNTRY_AREAS = REPO / "frontend" / "public" / "geo" / "country-areas.v1.json"
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


def test_a_country_entry_names_no_city(raw):
    # The site of a cluster abroad is rarely known, so a country entry stands for all of it.
    for r in raw["regions"]:
        if r["country_code"] != "ID":
            assert r["city"] == r["country_name"] == r["name"], r


def _km(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@pytest.mark.parametrize(
    "region_id, capital",
    [
        ("c-jp", (35.6895, 139.6917)),  # Tokyo
        ("c-us", (38.8951, -77.0364)),  # Washington
        ("c-gb", (51.5074, -0.1278)),  # London
        ("c-au", (-35.2809, 149.13)),  # Canberra
    ],
)
def test_a_country_is_not_pinned_at_its_capital(region_id, capital):
    entry = get_region_catalog().resolve(region_id)
    assert _km((entry["latitude"], entry["longitude"]), capital) > 100, entry


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
        # Bounding boxes drawn independently of the build, from the places themselves.
        ("id-3174", "Jakarta Selatan", "Daerah Khusus Ibukota Jakarta", -6.37, 106.75, -6.20, 106.88),
        # The source row puts Wakatobi at longitude 23.5, in Africa.
        ("id-7407", "Wakatobi", "Sulawesi Tenggara", -6.5, 123.3, -5.0, 124.3),
        ("id-9671", "Sorong (City)", "Papua Barat Daya", -1.0, 131.1, -0.7, 131.5),
        ("id-9503", "Yahukimo", "Papua Pegunungan", -5.2, 139.2, -4.5, 139.8),
        ("id-1602", "Ogan Komering Ilir", "Sumatera Selatan", -3.8, 104.5, -3.0, 105.2),
        ("c-nz", "New Zealand", None, -47.5, 166.0, -34.0, 179.0),
        ("c-gq", "Equatorial Guinea", None, 0.9, 9.3, 2.4, 11.4),
        ("c-jp", "Japan", None, 30.0, 129.0, 46.0, 146.0),
        ("c-us", "United States", None, 24.0, -125.0, 50.0, -66.0),
        ("c-sg", "Singapore", None, 1.15, 103.6, 1.48, 104.1),
    ],
)
def test_known_places_are_where_they_should_be(region_id, name, province, south, west, north, east):
    entry = get_region_catalog().resolve(region_id)
    assert entry is not None, region_id
    assert entry["name"] == name and entry.get("province") == province, entry
    assert south <= entry["latitude"] <= north and west <= entry["longitude"] <= east, entry


def _areas():
    if not COUNTRY_AREAS.exists():
        pytest.skip("frontend tree is not in this checkout")
    return {f["id"]: f for f in json.loads(COUNTRY_AREAS.read_text())["features"]}


def _in_ring(lng, lat, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        (xi, yi), (xj, yj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lng < xi + (lat - yi) * (xj - xi) / (yj - yi):
            inside = not inside
        j = i
    return inside


# Natural Earth letters New Zealand in the Tasman Sea and Equatorial Guinea in the Gulf of Guinea.
# The point must fall on the country the map shades, outside the water beside it.
@pytest.mark.parametrize("code", ["NZ", "GQ", "JP", "US", "GB", "SG", "FJ", "RU"])
def test_a_country_point_sits_on_its_own_outline(code):
    entry = get_region_catalog().resolve("c-" + code.lower())
    polygons = _areas()[code]["geometry"]["coordinates"]
    assert any(
        _in_ring(entry["longitude"], entry["latitude"], polygon[0])
        and not any(_in_ring(entry["longitude"], entry["latitude"], hole) for hole in polygon[1:])
        for polygon in polygons
    ), entry


def test_every_country_has_an_outline_the_map_can_shade(raw):
    # The build checks each point against the full-resolution outline. CI has no source cache, so
    # this ties the bundled outlines to the bundled catalogue there.
    features = _areas()
    countries = {r["country_code"]: r for r in raw["regions"] if r["id"].startswith("c-")}
    assert set(features) == set(countries)
    for code, feature in features.items():
        polygons = feature["geometry"]["coordinates"]
        assert feature["geometry"]["type"] == "MultiPolygon" and polygons, code
        for ring in (ring for polygon in polygons for ring in polygon):
            assert len(ring) >= 4 and ring[0] == ring[-1], code
        south, west, north, east = feature["properties"]["focus"]
        entry = countries[code]
        assert south <= entry["latitude"] <= north and west <= entry["longitude"] <= east, (code, entry)
        assert east - west <= 180, code


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
