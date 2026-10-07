"""One catalogue entry per country outside Indonesia, placed at its capital.

The country list, names, capitals and continents come from GeoNames countryInfo (ISO 3166-1).
The capital point is the GeoNames populated place, checked against Wikidata's current capital
(P36 with no end date) and against the country's own Natural Earth polygon.
"""

from __future__ import annotations

import collections
import re
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import curation
from geo import MultiPolygon, contains, haversine_km
from indonesia import Finding
from sources import Place, parse_wkt_point

CAPITAL_AGREEMENT_KM = 25.0
# note: atoll and coastal capitals sit a few km off a 1:10m outline (Tarawa 4 km, Flying Fish Cove 2 km)
POLYGON_TOLERANCE_KM = 5.0
CONTINENTS = {"AF", "AN", "AS", "EU", "NA", "OC", "SA"}

WIKIDATA_CAPITALS = """SELECT ?iso ?capLabel ?coord WHERE {
  ?country wdt:P297 ?iso .
  ?country p:P36 ?statement . ?statement ps:P36 ?capital .
  FILTER NOT EXISTS { ?statement pq:P582 ?ended }
  ?capital wdt:P625 ?coord .
  OPTIONAL { ?capital rdfs:label ?capLabel FILTER(LANG(?capLabel) = "en") }
}"""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _capital_place(capital: str, in_country: List[Place]) -> Optional[Place]:
    named = [p for p in in_country if p.fclass == "P" and _norm(capital) in {_norm(n) for n in p.names}]
    seats = [p for p in in_country if p.fcode == "PPLC"]
    # a country can carry more than one PPLC (seat of government vs constitutional capital), so
    # the place the capital name points at wins, then any PPLC, each by population
    for pool in ([p for p in seats if p in named], named, seats):
        if pool:
            return max(pool, key=lambda p: p.population)
    return None


def build(
    country_info: List[Dict[str, str]],
    places: List[Place],
    wikidata_rows: List[Dict[str, str]],
    polygons: Dict[str, MultiPolygon],
    ne_capitals: Dict[str, List[Tuple[str, float, float]]],
) -> Tuple[List[Dict[str, object]], List[Finding], List[Tuple[str, str]]]:
    entries: List[Dict[str, object]] = []
    findings: List[Finding] = []
    excluded: List[Tuple[str, str]] = []
    by_country: Dict[str, List[Place]] = collections.defaultdict(list)
    for p in places:
        by_country[p.country].append(p)
    capitals: Dict[str, List[Tuple[str, Tuple[float, float]]]] = collections.defaultdict(list)
    for row in wikidata_rows:
        point = parse_wkt_point(row.get("coord", ""))
        if point:
            capitals[row["iso"]].append((row.get("capLabel", ""), point))

    for row in sorted(country_info, key=lambda r: r["name"]):
        iso, name = row["iso"], row["name"]
        if iso in curation.EXCLUDED_COUNTRIES:
            excluded.append((iso, f"{name}: {curation.EXCLUDED_COUNTRIES[iso]}"))
            continue
        if not row["capital"]:
            excluded.append((iso, f"{name}: no capital (uninhabited or administered territory)"))
            continue

        def fail(detail: str) -> None:
            findings.append(Finding("fail", iso, name, "country", detail))

        if row["continent"] not in CONTINENTS:
            fail(f"unknown continent code {row['continent']!r}")
            continue
        capital = row["capital"]
        if iso in curation.COUNTRY_CAPITAL_OVERRIDES:
            capital, reason = curation.COUNTRY_CAPITAL_OVERRIDES[iso]
            findings.append(Finding("note", iso, name, "capital", f"placed at {capital}: {reason}"))
        place = _capital_place(capital, by_country.get(iso, []))
        if place is None:
            fail(f"capital {capital!r} not found among GeoNames populated places")
            continue
        point = (place.lat, place.lng)

        # two independent witnesses for the capital: Wikidata P36 and Natural Earth's capitals
        witnesses = [("wikidata " + label, p) for label, p in capitals.get(iso, [])]
        witnesses += [("natural earth " + n, (lat, lng)) for n, lat, lng in ne_capitals.get(iso, [])]
        agreeing = sorted({w[0].split()[0] + " " + w[0].split()[1] if w[0].startswith("natural") else "wikidata"
                           for w in witnesses if haversine_km(point, w[1]) <= CAPITAL_AGREEMENT_KM})
        if not agreeing:
            listing = "; ".join(f"{label} {haversine_km(point, p):.1f} km" for label, p in witnesses) or "no witnesses"
            fail(f"GeoNames capital {place.name} is not within {CAPITAL_AGREEMENT_KM:g} km of any witness ({listing})")
            continue
        if len(agreeing) == 1:
            findings.append(Finding("note", iso, name, "capital", f"{place.name} confirmed by {agreeing[0]} only"))

        shape = polygons.get(iso)
        if shape is None:
            findings.append(Finding("note", iso, name, "polygon", "no Natural Earth map unit; capital checked against point witnesses only"))
        elif not contains(point, shape, POLYGON_TOLERANCE_KM):
            fail(f"capital {place.name} lies outside the country's Natural Earth polygon")
            continue

        try:
            ZoneInfo(place.timezone)
        except Exception:  # noqa: BLE001 - reported as a failure
            fail(f"GeoNames time zone {place.timezone!r} is not a valid IANA zone")
            continue

        entries.append({
            "id": f"c-{iso.lower()}",
            "name": name,
            "city": place.name,
            "country_code": iso,
            "country_name": name,
            "latitude": round(place.lat, 4),
            "longitude": round(place.lng, 4),
            "timezone": place.timezone,
            "continent": row["continent"],
        })
    return entries, findings, excluded
