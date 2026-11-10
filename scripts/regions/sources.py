"""Download and parse the sources the region catalogue is built and verified from.

Every file is fetched once into a cache directory and reused, so a rebuild is offline after
the first run and the report can pin the exact bytes (sha256) each run was checked against.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from geo import MultiPolygon, geojson_to_multipolygon

USER_AGENT = "orcastra-regions-build/1.0 (https://github.com/sctsivali/orcastra-cmp)"

# GitHub-hosted sources are pinned to a commit so a rebuild sees the same bytes. GeoNames
# publishes daily dumps with no versioned URL; its sha256 is recorded in the report instead.
SOURCES: Dict[str, Dict[str, str]] = {
    "kemendagri": {
        "url": "https://raw.githubusercontent.com/cahyadsn/wilayah/cb05064af1ccc6c6ae5ee33e498ad69a62034a0a/db/wilayah_level_1_2.sql",
        "file": "wilayah_level_1_2.sql",
        "title": "Kode dan Data Wilayah Administrasi Pemerintahan, Kepmendagri No 300.2.2-2430 Tahun 2025 (cahyadsn/wilayah, MIT)",
    },
    "geoboundaries": {
        "url": "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/IDN/ADM2/geoBoundaries-IDN-ADM2_simplified.geojson",
        "file": "geoBoundaries-IDN-ADM2_simplified.geojson",
        "title": "geoBoundaries IDN ADM2 (BPS, WFP, OCHA ROAP), CC BY 3.0 IGO",
    },
    "geonames_id": {
        "url": "https://download.geonames.org/export/dump/ID.zip",
        "file": "geonames-ID.zip",
        "title": "GeoNames Indonesia dump, CC BY 4.0",
    },
    "geonames_cities": {
        "url": "https://download.geonames.org/export/dump/cities500.zip",
        "file": "geonames-cities500.zip",
        "title": "GeoNames cities500, CC BY 4.0",
    },
    "geonames_countries": {
        "url": "https://download.geonames.org/export/dump/countryInfo.txt",
        "file": "geonames-countryInfo.txt",
        "title": "GeoNames countryInfo (ISO 3166-1, capital, continent), CC BY 4.0",
    },
    "natural_earth_places": {
        "url": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_populated_places_simple.geojson",
        "file": "ne-5.1.2-10m-populated-places-simple.geojson",
        "title": "Natural Earth 5.1.2 1:10m populated places, public domain",
    },
    "natural_earth": {
        "url": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_admin_0_map_units.geojson",
        "file": "ne-5.1.2-10m-admin0-map-units.geojson",
        "title": "Natural Earth 5.1.2 1:10m admin-0 map units, public domain",
    },
}

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
OVERPASS_API = "https://overpass-api.de/api/interpreter"

# Every regent's and mayor's office OpenStreetMap knows of in Indonesia, in one request.
OSM_OFFICES_QUERY = (
    '[out:json][timeout:300];area["ISO3166-1"="ID"][admin_level=2]->.id;'
    '(nwr["name"~"^(kantor )?(bupati|wali ?kota|walikota)( |$)",i](area.id););out center tags;'
)


@dataclass
class FetchedSource:
    key: str
    path: Path
    sha256: str
    size: int


def fetch(key: str, cache_dir: Path, refresh: bool = False) -> FetchedSource:
    spec = SOURCES[key]
    path = cache_dir / spec["file"]
    if refresh or not path.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(spec["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    data = path.read_bytes()
    return FetchedSource(key, path, hashlib.sha256(data).hexdigest(), len(data))


def sparql(query: str, cache_dir: Path, refresh: bool = False) -> List[Dict[str, str]]:
    """Run a Wikidata SPARQL query, cached by the query's hash. Returns flat string rows."""
    digest = hashlib.sha256(query.encode()).hexdigest()[:16]
    path = cache_dir / f"wikidata-{digest}.json"
    if refresh or not path.exists():
        url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
        last_error: Optional[Exception] = None
        # Retry with backoff: the public endpoint rate-limits and times out under load.
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read()
                json.loads(body)
                cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                break
            except Exception as exc:  # noqa: BLE001 - re-raised below after the last attempt
                last_error = exc
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"Wikidata query failed after retries: {last_error}")
    payload = json.loads(path.read_text())
    return [{k: v["value"] for k, v in row.items()} for row in payload["results"]["bindings"]]


@dataclass
class OsmOffice:
    osm_id: str
    name: str
    lat: float
    lng: float
    kind: str  # "bupati" | "walikota"


def fetch_osm_offices(cache_dir: Path, refresh: bool = False) -> Tuple[List[OsmOffice], FetchedSource]:
    path = cache_dir / "osm-offices.json"
    if refresh or not path.exists():
        body = urllib.parse.urlencode({"data": OSM_OFFICES_QUERY}).encode()
        last_error: Optional[Exception] = None
        # Retry with backoff: the shared Overpass instance answers 429 and 504 under load.
        for attempt in range(5):
            try:
                req = urllib.request.Request(OVERPASS_API, data=body, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=400) as resp:
                    data = resp.read()
                json.loads(data)
                cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                break
            except Exception as exc:  # noqa: BLE001 - re-raised below after the last attempt
                last_error = exc
                time.sleep(30 * (attempt + 1))
        else:
            raise RuntimeError(f"Overpass query failed after retries: {last_error}")
    raw = path.read_bytes()
    offices: List[OsmOffice] = []
    for el in json.loads(raw)["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name", "")
        # Residences (rumah jabatan), bus stops and car parks borrow the office's name.
        if re.search(r"\b(rujab|rumah|jabatan|halte|parkir|wakil)\b", name, re.I):
            continue
        if tags.get("amenity") in {"bus_station", "parking", "parking_entrance"} or "highway" in tags:
            continue
        center = el.get("center") or {"lat": el.get("lat"), "lon": el.get("lon")}
        if center.get("lat") is None:
            continue
        kind = "bupati" if re.search(r"bupati", name, re.I) else "walikota"
        offices.append(OsmOffice(f"{el['type']}/{el['id']}", name, float(center["lat"]), float(center["lon"]), kind))
    return offices, FetchedSource("osm_offices", path, hashlib.sha256(raw).hexdigest(), len(raw))


NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
_last_nominatim_call = [0.0]


def nominatim_places(query: str, cache_dir: Path, refresh: bool = False) -> List[Tuple[str, str, float, float]]:
    """Settlement nodes OpenStreetMap returns for a free-text query: (osm id, name, lat, lng).

    important: only called for the few seats nothing else could settle, and cached, because the
    public instance allows one request per second and no bulk geocoding.
    """
    digest = hashlib.sha256(query.encode()).hexdigest()[:16]
    path = cache_dir / f"nominatim-{digest}.json"
    if refresh or not path.exists():
        wait = 1.1 - (time.time() - _last_nominatim_call[0])
        if wait > 0:
            time.sleep(wait)
        url = NOMINATIM_SEARCH + "?" + urllib.parse.urlencode(
            {"q": query, "format": "jsonv2", "limit": 8, "countrycodes": "id"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
        _last_nominatim_call[0] = time.time()
        json.loads(body)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    settlements = {"city", "town", "village", "hamlet", "suburb", "quarter", "neighbourhood", "municipality"}
    out = []
    for r in json.loads(path.read_text()):
        if r.get("category") == "place" and r.get("type") in settlements:
            out.append((f"{r['osm_type']}/{r['osm_id']}", r.get("name") or "", float(r["lat"]), float(r["lon"])))
    return out


def parse_wkt_point(value: str) -> Optional[tuple]:
    m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", value or "")
    return (float(m.group(2)), float(m.group(1))) if m else None


# ---------------------------------------------------------------- Kemendagri (cahyadsn) ----

@dataclass
class AdminArea:
    code: str
    name: str
    seat: Optional[str]
    lat: float
    lng: float
    tz_hours: int
    rings: List[List[tuple]] = field(default_factory=list)
    # ok | repaired | missing | invalid
    polygon_status: str = "ok"


_ROW = re.compile(
    r"^\('([\d.]+)','((?:[^']|'')*)',(NULL|'(?:[^']|'')*'),\s*([-\d.eE]+),\s*([-\d.eE]+),\s*[-\d.]+,"
    r"\s*(\d+),\s*(?:[-\d.]+|NULL),\s*(?:[-\d.]+|NULL),\s*(NULL|'(.*)'),\s*\d+\);?\s*$"
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("''", "'")).strip()


def _rings(raw: Optional[str]) -> Tuple[List[List[tuple]], str]:
    if not raw:
        return [], "missing"
    status = "ok"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # One upstream row (32.79 Kota Banjar) is cut short and only lacks its closing
        # brackets. Rebalance once; anything still broken is reported as having no polygon.
        try:
            data = json.loads(raw + "]" * (raw.count("[") - raw.count("]")))
        except json.JSONDecodeError:
            return [], "invalid"
        status = "repaired"

    # The path column nests either one ring or a list of rings, as [lat, lng] pairs.
    def is_point(x: object) -> bool:
        return isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x)

    out: List[List[tuple]] = []

    def walk(node: object) -> None:
        if isinstance(node, list) and node and is_point(node[0]):
            out.append([(float(p[0]), float(p[1])) for p in node])
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data)
    return out, status


def parse_kemendagri(path: Path) -> Dict[str, AdminArea]:
    areas: Dict[str, AdminArea] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("('"):
                continue
            m = _ROW.match(line.rstrip("\n"))
            if not m:
                raise ValueError(f"unparsed Kemendagri row: {line[:80]}")
            code, name, seat, lat, lng, tz, _, path_raw = m.groups()
            rings, polygon_status = _rings(path_raw)
            areas[code] = AdminArea(
                code=code,
                name=_clean(name),
                seat=None if seat == "NULL" else (_clean(seat[1:-1]) or None),
                lat=float(lat),
                lng=float(lng),
                tz_hours=int(tz),
                rings=rings,
                polygon_status=polygon_status,
            )
    return areas


# ------------------------------------------------------------------------- GeoNames ----

@dataclass
class Place:
    geonameid: str
    name: str
    names: List[str]
    lat: float
    lng: float
    fclass: str
    fcode: str
    country: str
    population: int
    timezone: str


def _geonames_rows(fh: io.TextIOBase) -> Iterator[Place]:
    for line in fh:
        c = line.rstrip("\n").split("\t")
        if len(c) < 19:
            continue
        alt = [a for a in c[3].split(",") if a]
        yield Place(c[0], c[1], [c[1], c[2], *alt], float(c[4]), float(c[5]), c[6], c[7], c[8],
                    int(c[14] or 0), c[17])


def load_geonames_zip(path: Path, member: str, keep: Callable[["Place"], bool]) -> List[Place]:
    with zipfile.ZipFile(path) as zf, zf.open(member) as raw:
        return [p for p in _geonames_rows(io.TextIOWrapper(raw, encoding="utf-8")) if keep(p)]


def load_country_info(path: Path) -> List[Dict[str, str]]:
    cols = ["iso", "iso3", "iso_numeric", "fips", "name", "capital", "area", "population", "continent"]
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        rows.append(dict(zip(cols, line.split("\t")[: len(cols)])))
    return rows


# ------------------------------------------------------------------ polygons --------------

def load_geoboundaries(path: Path) -> List[tuple]:
    data = json.loads(path.read_text())
    return [(f["properties"]["shapeName"], geojson_to_multipolygon(f["geometry"])) for f in data["features"]]


@dataclass(frozen=True)
class MapUnit:
    admin: str
    label: Tuple[float, float]  # Natural Earth's own label point, (lat, lng)
    shape: MultiPolygon


def load_natural_earth_units(path: Path) -> Dict[str, List[MapUnit]]:
    """ISO 3166-1 alpha-2 -> map units, so overseas territories stand on their own.

    A code can span several units (Australia plus Ashmore and Cartier, Coral Sea; the four
    nations of the United Kingdom), so every unit is kept and the caller decides.
    """
    units: Dict[str, List[MapUnit]] = {}
    for f in json.loads(path.read_text())["features"]:
        props = f["properties"]
        iso = props.get("ISO_A2_EH") or ""
        if len(iso) == 2 and f.get("geometry"):
            label = (float(props["LABEL_Y"]), float(props["LABEL_X"]))
            units.setdefault(iso, []).append(MapUnit(props.get("ADMIN") or "", label, geojson_to_multipolygon(f["geometry"])))
    return units


def load_natural_earth_capitals(path: Path) -> Dict[str, List[Tuple[str, float, float]]]:
    """ISO alpha-2 -> national capitals Natural Earth records: (name, lat, lng)."""
    out: Dict[str, List[Tuple[str, float, float]]] = {}
    for f in json.loads(path.read_text())["features"]:
        props = f["properties"]
        if "Admin-0" not in (props.get("featurecla") or "") and not props.get("adm0cap"):
            continue
        iso = props.get("iso_a2") or ""
        if len(iso) == 2 and iso != "-9":
            out.setdefault(iso, []).append((props.get("name") or "", float(props["latitude"]), float(props["longitude"])))
    return out
