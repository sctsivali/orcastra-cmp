"""The 100 original region ids, from before the catalogue covered every kabupaten and kota.

Clusters may store those ids in their location metadata, so every one must keep resolving: 98
map onto the kota they stand for, and two (the province-wide Jakarta entry and IKN) become
hidden legacy entries. The comparison rows feed the build report.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import curation
from geo import haversine_km
from indonesia import Finding, TZ_BY_UTC_HOURS, norm, split_name
from sources import AdminArea, Place, parse_wkt_point

LEGACY_AGREEMENT_KM = 10.0


@dataclass
class Comparison:
    old_id: str
    old_name: str
    old_province: str
    new_id: str
    new_name: str
    new_province: str
    moved_km: float


def _base(name: str) -> str:
    return norm(re.sub(r"\s*\(.*?\)", "", name))


def build(
    legacy_rows: List[Dict[str, object]],
    indonesia_entries: List[Dict[str, object]],
    areas: Dict[str, AdminArea],
    id_places: Dict[str, Place],
    wikidata_rows: List[Dict[str, str]],
) -> Tuple[Dict[str, str], List[Dict[str, object]], List[Comparison], List[Finding]]:
    aliases: Dict[str, str] = {}
    legacy_entries: List[Dict[str, object]] = []
    comparisons: List[Comparison] = []
    findings: List[Finding] = []
    kota = {_base(split_name(str(e["official_name"]))[1]): e for e in indonesia_entries if split_name(str(e["official_name"]))[0] == "kota"}
    wikidata_points = {row["item"].rsplit("/", 1)[-1]: parse_wkt_point(row.get("coord", "")) for row in wikidata_rows}

    for row in legacy_rows:
        old_id, old_name = str(row["id"]), str(row["name"])
        old_point = (float(row["latitude"]), float(row["longitude"]))

        if old_id in curation.LEGACY_ONLY:
            spec = curation.LEGACY_ONLY[old_id]
            place = id_places.get(spec["geonames_id"])
            witness = wikidata_points.get(spec["wikidata"])
            if place is None or witness is None:
                findings.append(Finding("fail", old_id, old_name, "legacy", "GeoNames place or Wikidata item not found"))
                continue
            if place.timezone not in TZ_BY_UTC_HOURS.values():
                findings.append(Finding("fail", old_id, old_name, "legacy", f"unexpected zone {place.timezone}"))
                continue
            distance = haversine_km((place.lat, place.lng), witness)
            if distance > LEGACY_AGREEMENT_KM:
                findings.append(Finding("fail", old_id, old_name, "legacy",
                                        f"GeoNames {place.name} is {distance:.1f} km from Wikidata {spec['wikidata']}"))
                continue
            province = areas.get(spec["province_code"]) if spec["province_code"] else None
            entry: Dict[str, object] = {
                "id": old_id,
                "name": spec["name"],
                "city": spec["city"],
                **({"province": province.name, "admin_code": province.code} if province else {}),
                "country_code": "ID",
                "country_name": "Indonesia",
                "latitude": round(place.lat, 4),
                "longitude": round(place.lng, 4),
                "timezone": place.timezone,
                "continent": "AS",
            }
            legacy_entries.append(entry)
            findings.append(Finding("note", old_id, old_name, "legacy", spec["reason"]))
            comparisons.append(Comparison(old_id, old_name, str(row["province_header"]), old_id, spec["name"],
                                          province.name if province else "(special capital region)",
                                          haversine_km(old_point, (place.lat, place.lng))))
            continue

        target: Optional[Dict[str, object]] = kota.get(_base(old_name))
        if target is None:
            close = difflib.get_close_matches(_base(old_name), list(kota), n=1, cutoff=0.85)
            target = kota[close[0]] if close else None
            if target is not None:
                findings.append(Finding("note", old_id, old_name, "legacy", f"matched {target['name']} by spelling similarity"))
        if target is None:
            findings.append(Finding("fail", old_id, old_name, "legacy", "no kota in the catalogue matches this legacy entry"))
            continue
        if target["id"] in aliases.values():
            findings.append(Finding("fail", old_id, old_name, "legacy", f"two legacy ids map onto {target['id']}"))
            continue
        aliases[old_id] = str(target["id"])
        comparisons.append(Comparison(old_id, old_name, str(row["province_header"]), str(target["id"]), str(target["name"]),
                                      str(target["province"]),
                                      haversine_km(old_point, (float(target["latitude"]), float(target["longitude"])))))
    return aliases, legacy_entries, comparisons, findings


WIKIDATA_LEGACY = """SELECT ?item ?coord WHERE {{
  VALUES ?item {{ {items} }}
  ?item wdt:P625 ?coord .
}}"""


def wikidata_query() -> str:
    return WIKIDATA_LEGACY.format(items=" ".join(f"wd:{spec['wikidata']}" for spec in curation.LEGACY_ONLY.values()))
