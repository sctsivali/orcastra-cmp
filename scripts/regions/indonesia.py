"""Indonesian kabupaten and kota: names from Kepmendagri, seats placed at GeoNames town points.

Kepmendagri is the legal authority for codes, names and seats, so those come from it (with the
corrections in curation.py). Its coordinate column is not a seat location, though: for a large
share of rows it is a point somewhere inside the area, up to tens of kilometres from the town.
The point therefore comes from the GeoNames populated place that carries the seat's name and
lies inside the regency's own geoBoundaries polygon, which picks the right Martapura out of
several. Every chosen point must then be corroborated by a second source within 10 km.
"""

from __future__ import annotations

import collections
import datetime
import difflib
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import curation
from geo import LatLng, MultiPolygon, bbox, contains, haversine_km
from sources import AdminArea, OsmOffice, Place, parse_wkt_point

TZ_BY_UTC_HOURS = {7: "Asia/Jakarta", 8: "Asia/Makassar", 9: "Asia/Jayapura"}
# Kepmendagri 300.2.2-2430 Tahun 2025 totals; a refreshed source that drifts fails the build.
EXPECTED_COUNTS = {"provinces": 38, "kabupaten": 416, "kota": 98}

POLYGON_TOLERANCE_KM = 1.0
CORROBORATION_KM = 10.0
# A mapped regent's or mayor's office is the seat by definition; this only guards a mis-tag.
OFFICE_SANITY_KM = 30.0
# Two sources this close did not measure independently; one copied the other.
SAME_ORIGIN_KM = 0.1
FUZZY_RATIO = 0.88
FEATURE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5, "PPLX": 6, "ADM3": 8}

WIKIDATA_REGENCIES = """SELECT ?item ?code ?label ?labelEn ?coord ?seatLabel ?seatCoord WHERE {
  ?item wdt:P2588 ?code . FILTER(REGEX(?code, "^[0-9]{2}\\\\.[0-9]{2}$"))
  OPTIONAL { ?item wdt:P625 ?coord }
  OPTIONAL { ?item rdfs:label ?label FILTER(LANG(?label) = "id") }
  OPTIONAL { ?item rdfs:label ?labelEn FILTER(LANG(?labelEn) = "en") }
  OPTIONAL { ?item wdt:P36 ?seat .
    OPTIONAL { ?seat rdfs:label ?seatLabel FILTER(LANG(?seatLabel) = "id") }
    OPTIONAL { ?seat wdt:P625 ?seatCoord } }
}"""

PREFIXES = (
    ("Kabupaten Administrasi ", "kabupaten"),
    ("Kota Administrasi ", "kota"),
    ("Kabupaten ", "kabupaten"),
    ("Kota ", "kota"),
)


def norm(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def split_name(name: str) -> Tuple[str, str]:
    """'Kota Administrasi Jakarta Selatan' -> ('kota', 'Jakarta Selatan')."""
    for prefix, kind in PREFIXES:
        if name.startswith(prefix):
            return kind, name[len(prefix):]
    return "kabupaten", name


def _names_match(name: str, keys: List[str]) -> bool:
    # "Biromaru" is how OpenStreetMap labels the town Kepmendagri calls "Sigi Biromaru"
    return any(name == k or (min(len(name), len(k)) >= 5 and (name in k or k in name)) for k in keys)


def _label_base(label: str) -> str:
    """Wikidata labels: 'Kabupaten Ciamis', 'Ciamis Regency', 'Bogor City', 'Ciamis'."""
    label = re.sub(r"\s+(Regency|City)$", "", label.strip())
    return split_name(label)[1]


def place_key(name: str) -> str:
    # GeoNames and Wikidata both decorate a seat: "Kota Waisai", "Rantau (kota)", "Buntok Kota"
    name = re.sub(r"\s*\(kota\)$", "", name, flags=re.I)
    name = re.sub(r"^Kota\s+", "", name)
    name = re.sub(r"\s+Kota$", "", name)
    return norm(name)


@dataclass
class Finding:
    level: str  # "fail" stops the build, "note" is reported
    code: str
    name: str
    check: str
    detail: str


@dataclass
class Resolved:
    entry: Dict[str, object]
    source: str
    moved_km: float  # distance from the Kepmendagri coordinate column
    support: List[str]  # independent source groups that agree within CORROBORATION_KM


@dataclass
class Witness:
    """One source's claim about where a seat is."""

    group: str  # independence group: osm-office | geonames | wikidata | kemendagri | override
    label: str
    point: LatLng
    rank: int  # tie-break preference, lower wins
    place: Optional[Place] = None
    fuzzy: bool = False


@dataclass
class IndonesiaResult:
    entries: List[Dict[str, object]] = field(default_factory=list)
    resolved: List[Resolved] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    provinces: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)


class PlaceIndex:
    """Name and 0.5-degree grid lookup over GeoNames features."""

    def __init__(self, places: List[Place]) -> None:
        self.by_name: Dict[str, List[Place]] = collections.defaultdict(list)
        self.grid: Dict[Tuple[int, int], List[Place]] = collections.defaultdict(list)
        self.by_id: Dict[str, Place] = {}
        for p in places:
            self.by_id[p.geonameid] = p
            self.grid[(int(p.lat // 0.5), int(p.lng // 0.5))].append(p)
            for key in {place_key(n) for n in p.names}:
                if key:
                    self.by_name[key].append(p)

    def in_box(self, shape: MultiPolygon) -> List[Place]:
        la0, lo0, la1, lo1 = bbox(shape)
        out: List[Place] = []
        for i in range(int(la0 // 0.5), int(la1 // 0.5) + 1):
            for j in range(int(lo0 // 0.5), int(lo1 // 0.5) + 1):
                out.extend(self.grid.get((i, j), []))
        return out


def _utc_hours(tz: str) -> Optional[float]:
    try:
        offset = datetime.datetime(2026, 1, 1, tzinfo=ZoneInfo(tz)).utcoffset()
    except Exception:  # noqa: BLE001 - an unknown zone name is reported, not fatal here
        return None
    return offset.total_seconds() / 3600 if offset is not None else None


def _polygon_index(boundaries: List[Tuple[str, MultiPolygon]]) -> Dict[Tuple[str, str], Tuple[str, MultiPolygon]]:
    index: Dict[Tuple[str, str], Tuple[str, MultiPolygon]] = {}
    for name, shape in boundaries:
        kind, base = split_name(name)
        index[(kind, norm(base))] = (name, shape)
        # geoBoundaries spells Kabupaten Kotabaru "Kota Baru", which reads as a kota prefix
        index.setdefault(("kabupaten", norm(name)), (name, shape))
    return index


def _seat_candidates(keys: List[str], shape: MultiPolygon, index: PlaceIndex) -> List[Tuple[tuple, Place, bool]]:
    found: List[Tuple[tuple, Place, bool]] = []
    for ki, key in enumerate(keys):
        for p in index.by_name.get(key, []):
            if contains((p.lat, p.lng), shape, POLYGON_TOLERANCE_KM):
                rank = (0, 0 if p.fclass == "P" else 1, ki, FEATURE_RANK.get(p.fcode, 7), -p.population)
                found.append((rank, p, False))
    if found:
        return sorted(found, key=lambda t: t[0])
    # Spelling drifts between sources (Taluk/Teluk Kuantan, Nanga/Nangah Pinoh). Fuzzy matching
    # is safe only because the candidate must also sit inside the polygon.
    for p in index.in_box(shape):
        names = {place_key(n) for n in p.names}
        for ki, key in enumerate(keys):
            if any(abs(len(n) - len(key)) <= 3 and difflib.SequenceMatcher(None, key, n).ratio() >= FUZZY_RATIO for n in names):
                if contains((p.lat, p.lng), shape, POLYGON_TOLERANCE_KM):
                    rank = (1, 0 if p.fclass == "P" else 1, ki, FEATURE_RANK.get(p.fcode, 7), -p.population)
                    found.append((rank, p, True))
                break
    return sorted(found, key=lambda t: t[0])


def _witnesses(
    kind: str,
    base: str,
    shape: MultiPolygon,
    kemendagri_point: LatLng,
    wd_rows: List[Dict[str, str]],
    candidates: List[Tuple[tuple, Place, bool]],
    offices: List[OsmOffice],
) -> List[Witness]:
    out: List[Witness] = []
    office_kind = "walikota" if kind == "kota" else "bupati"
    named = [o for o in offices if o.kind == office_kind and norm(base) in norm(o.name)]
    generic = [o for o in offices if o.kind == office_kind and norm(o.name) in {"kantorbupati", "bupati", "kantorwalikota", "walikota"}]
    for pool, rank in ((named, 0), (generic, 1)):
        inside = [o for o in pool if contains((o.lat, o.lng), shape, POLYGON_TOLERANCE_KM)]
        if rank == 1 and len(inside) != 1:
            continue  # An unnamed office only counts when it is the only one in the polygon.
        for o in sorted(inside, key=lambda o: haversine_km((o.lat, o.lng), kemendagri_point)):
            out.append(Witness("osm-office", f"osm:{o.osm_id} {o.name}", (o.lat, o.lng), rank))
        if inside:
            break
    for i, (_, place, fuzzy) in enumerate(candidates):
        out.append(Witness("geonames", f"geonames:{place.geonameid} {place.fcode} {place.name}", (place.lat, place.lng), 10 + i, place, fuzzy))
    if contains(kemendagri_point, shape, POLYGON_TOLERANCE_KM):
        out.append(Witness("kemendagri", "kepmendagri", kemendagri_point, 40))
    for r in wd_rows:
        for key, label in (("seatCoord", "wikidata seat"), ("coord", "wikidata kota")):
            if key == "coord" and kind != "kota":
                continue  # A kabupaten's own P625 is usually its centroid, not its seat.
            point = parse_wkt_point(r.get(key, ""))
            if point and contains(point, shape, POLYGON_TOLERANCE_KM):
                same = haversine_km(point, kemendagri_point) <= SAME_ORIGIN_KM
                out.append(Witness("kemendagri" if same else "wikidata", label, point, 30))
    return out


def _choose_point(witnesses: List[Witness]) -> Tuple[Optional[Witness], set, str]:
    """A mapped office wins outright; otherwise the point most independent groups agree on."""
    offices = [w for w in witnesses if w.group == "osm-office"]
    if offices:
        office = min(offices, key=lambda w: w.rank)
        support = {w.group for w in witnesses if haversine_km(w.point, office.point) <= OFFICE_SANITY_KM}
        if support == {"osm-office"}:
            return None, support, f"{office.label} is over {OFFICE_SANITY_KM:g} km from every other source"
        return office, support, ""
    best: Optional[Tuple[tuple, Witness, set]] = None
    for w in witnesses:
        support = {o.group for o in witnesses if haversine_km(o.point, w.point) <= CORROBORATION_KM}
        key = (-len(support), w.rank)
        if best is None or key < best[0]:
            best = (key, w, support)
    if best is None:
        return None, set(), "no source places this seat inside the regency polygon"
    if len(best[2]) < 2:
        return None, best[2], "no point is corroborated by two independent sources"
    return best[1], best[2], ""


def _shorten_names(entries: List[Dict[str, object]]) -> None:
    """Show the place name alone: "Garut" for "Kabupaten Garut".

    Where a kabupaten and a kota share a name (Bogor, Malang, Sorong, 26 pairs), the kind is added
    in English, "Bogor (City)" and "Bogor (Regency)", because the two are different places with
    different points. official_name keeps the Kepmendagri form.
    """
    bases = collections.Counter(split_name(str(e["official_name"]))[1] for e in entries)
    for e in entries:
        kind, base = split_name(str(e["official_name"]))
        e["name"] = f"{base} ({'City' if kind == 'kota' else 'Regency'})" if bases[base] > 1 else base


def build(
    areas: Dict[str, AdminArea],
    boundaries: List[Tuple[str, MultiPolygon]],
    places: List[Place],
    wikidata_rows: List[Dict[str, str]],
    offices: List[OsmOffice],
    osm_lookup: Optional[Callable[[str], List[Tuple[str, str, float, float]]]] = None,
) -> IndonesiaResult:
    result = IndonesiaResult()
    index = PlaceIndex(places)
    polygons = _polygon_index(boundaries)
    wikidata: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
    for row in wikidata_rows:
        wikidata[row["code"]].append(row)

    provinces = {code: a for code, a in areas.items() if "." not in code}
    regencies = {code: a for code, a in areas.items() if code.count(".") == 1}
    result.provinces = {code: a.name for code, a in sorted(provinces.items())}

    def fail(code: str, name: str, check: str, detail: str) -> None:
        result.findings.append(Finding("fail", code, name, check, detail))

    def note(code: str, name: str, check: str, detail: str) -> None:
        result.findings.append(Finding("note", code, name, check, detail))

    for code, area in sorted(regencies.items()):
        name = curation.NAME_FIXES.get(code, (area.name,))[0]
        if code in curation.NAME_FIXES:
            note(code, name, "name", f"corrected from '{area.name}': {curation.NAME_FIXES[code][1]}")
        kind, base = split_name(name)
        seat = curation.SEAT_FIXES.get(code, (area.seat,))[0]
        if code in curation.SEAT_FIXES:
            note(code, name, "seat", curation.SEAT_FIXES[code][1])
        province_code = code.split(".")[0]
        province = provinces.get(province_code)
        if province is None:
            fail(code, name, "province", f"no province row {province_code}")
            continue

        wd_rows = wikidata.get(code, [])
        gb_name = curation.GEOBOUNDARIES_NAME.get(code, (None, ""))[0]

        # polygon
        poly_key = ("kabupaten", norm(gb_name)) if gb_name else (kind, norm(base.replace("Kep. ", "Kepulauan ")))
        if gb_name and gb_name.startswith("Kota "):
            poly_key = split_name(gb_name)[0], norm(split_name(gb_name)[1])
        polygon = polygons.get(poly_key)
        if polygon is None:
            fail(code, name, "polygon", "no geoBoundaries ADM2 polygon matches this name")
            continue
        shape = polygon[1]

        # name: Kepmendagri against Wikidata (id or en label), else against the boundary set
        wd_names = {_label_base(r[k]) for r in wd_rows for k in ("label", "labelEn") if r.get(k)}
        if not wd_rows:
            fail(code, name, "name", "no Wikidata item carries this administrative code (P2588)")
            continue
        if wd_names and not any(norm(n) == norm(base) for n in wd_names):
            fail(code, name, "name", f"Wikidata names it {sorted(wd_names)}")
            continue
        if not wd_names:
            if gb_name:
                fail(code, name, "name", "Wikidata has no label and geoBoundaries only matches through a rename")
                continue
            note(code, name, "name", "Wikidata item has no id or en label; name confirmed by geoBoundaries alone")

        # timezone: the regency must match its province, and the province must be a known zone
        tz = TZ_BY_UTC_HOURS.get(area.tz_hours)
        if tz is None or area.tz_hours != province.tz_hours:
            fail(code, name, "timezone", f"UTC+{area.tz_hours} vs province UTC+{province.tz_hours}")
            continue

        keys: List[str] = []
        if kind == "kota":
            keys.append(place_key(base))
        if seat:
            keys.append(place_key(seat))
        keys += [place_key(r["seatLabel"]) for r in wd_rows if r.get("seatLabel")]
        aliases = curation.SEAT_ALIASES.get(code, ([], ""))
        keys += [place_key(a) for a in aliases[0]]
        keys = [k for i, k in enumerate(keys) if k and k not in keys[:i]]
        if aliases[0]:
            note(code, name, "seat", f"also searched as {aliases[0]}: {aliases[1]}")

        kemendagri_point: LatLng = (area.lat, area.lng)
        override = curation.COORDINATE_OVERRIDES.get(code)
        if override:
            chosen = Witness("override", f"override ({override[2]})", (override[0], override[1]), 0)
            support = {"override"}
            note(code, name, "coordinate", f"override: {override[2]}")
            if not contains(chosen.point, shape, POLYGON_TOLERANCE_KM):
                fail(code, name, "coordinate", "override point is outside the regency polygon")
                continue
        else:
            candidates = _seat_candidates(keys, shape, index)
            witnesses = _witnesses(kind, base, shape, kemendagri_point, wd_rows, candidates, offices)
            picked, support, reason = _choose_point(witnesses)
            if picked is None and osm_lookup is not None:
                # Last resort, and the only network call per regency: OSM settlement nodes.
                for query in dict.fromkeys(q for q in [seat, *aliases[0], base if kind == "kota" else None] if q):
                    for osm_id, osm_name, lat, lng in osm_lookup(f"{query}, {base}, Indonesia"):
                        if _names_match(place_key(osm_name), keys) and contains((lat, lng), shape, POLYGON_TOLERANCE_KM):
                            witnesses.append(Witness("osm-place", f"osm:{osm_id} {osm_name}", (lat, lng), 20))
                picked, support, reason = _choose_point(witnesses)
            if picked is None:
                listing = "; ".join(f"{w.label} ({w.point[0]:.4f}, {w.point[1]:.4f})" for w in witnesses) or "none"
                fail(code, name, "coordinate", f"{reason}. Candidates: {listing}")
                continue
            chosen = picked
            if chosen.place is not None and chosen.fuzzy:
                note(code, name, "seat", f"seat '{seat}' matched GeoNames '{chosen.place.name}' by spelling similarity")
            if chosen.place is not None and chosen.place.fclass != "P":
                note(code, name, "seat", f"no populated place named '{seat}'; using the {chosen.place.fcode} '{chosen.place.name}' point")
            outliers = [w for w in witnesses if haversine_km(w.point, chosen.point) > CORROBORATION_KM]
            if outliers:
                note(code, name, "coordinate", "disagreeing sources set aside: " + "; ".join(
                    f"{w.label} {haversine_km(w.point, chosen.point):.1f} km away" for w in outliers))
            if chosen.place is not None:
                hours = _utc_hours(chosen.place.timezone)
                if hours is not None and hours != area.tz_hours:
                    fail(code, name, "timezone", f"GeoNames zone {chosen.place.timezone} is UTC+{hours:g}, Kepmendagri says UTC+{area.tz_hours}")
                    continue

        entry: Dict[str, object] = {
            "id": "id-" + code.replace(".", ""),
            "name": name,
            "official_name": name,
            "city": base if kind == "kota" else (seat or base),
            "province": province.name,
            "admin_code": code,
            "country_code": "ID",
            "country_name": "Indonesia",
            "latitude": round(chosen.point[0], 4),
            "longitude": round(chosen.point[1], 4),
            "timezone": tz,
            "continent": "AS",
        }
        result.entries.append(entry)
        result.resolved.append(Resolved(entry, chosen.label, haversine_km(chosen.point, kemendagri_point), sorted(support)))

    kinds = collections.Counter(split_name(str(e["official_name"]))[0] for e in result.entries)
    _shorten_names(result.entries)
    result.counts = {"provinces": len(provinces), "kabupaten": kinds["kabupaten"], "kota": kinds["kota"]}
    for key, expected in EXPECTED_COUNTS.items():
        if result.counts.get(key) != expected:
            fail("--", "Indonesia", "counts", f"{key}: expected {expected}, built {result.counts.get(key)}")
    for pcode, pname in result.provinces.items():
        if not any(str(e["admin_code"]).startswith(pcode + ".") for e in result.entries):
            fail(pcode, pname, "counts", "province has no kabupaten or kota in the catalogue")
    return result
