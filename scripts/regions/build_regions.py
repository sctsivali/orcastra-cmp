#!/usr/bin/env python3
"""Build and verify the region catalogue behind the cluster location picker and the regions map.

    python3 scripts/regions/build_regions.py            # fetch (cached), verify, write
    python3 scripts/regions/build_regions.py --check    # rebuild from cache, fail if outputs differ
    python3 scripts/regions/build_regions.py --refresh  # re-download every source first

Writes the same catalogue to the frontend (bundled into the picker) and the backend (read by the
location backfill), plus verify-report.md. Standard library only. Nothing is written when any
verification fails; the failures are printed and the exit code is 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import countries  # noqa: E402
import indonesia  # noqa: E402
import legacy  # noqa: E402
import report  # noqa: E402
from sources import (  # noqa: E402
    fetch,
    fetch_osm_offices,
    load_country_info,
    load_geoboundaries,
    load_geonames_zip,
    load_natural_earth,
    load_natural_earth_capitals,
    nominatim_places,
    parse_kemendagri,
    sparql,
)

REPO = HERE.parent.parent
OUTPUTS = {
    "frontend": REPO / "frontend" / "src" / "lib" / "regions" / "catalog.json",
    "backend": REPO / "backend" / "app" / "data" / "regions_catalog.json",
}
REPORT = HERE / "verify-report.md"
SCHEMA = 1


def _catalogue_json(regions: List[Dict[str, object]], legacy_entries: List[Dict[str, object]], aliases: Dict[str, str]) -> Tuple[str, str]:
    def entry(e: Dict[str, object]) -> str:
        return json.dumps(e, ensure_ascii=False, separators=(", ", ": "))

    body = {"regions": regions, "legacy": legacy_entries, "aliases": dict(sorted(aliases.items()))}
    version = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    # one entry per line, so a data change reads as a one-line diff in review
    parts = [
        "{",
        f'  "schema": {SCHEMA},',
        f'  "version": "{version}",',
        '  "source": "scripts/regions/build_regions.py (see scripts/regions/verify-report.md)",',
        '  "regions": [',
        ",\n".join("    " + entry(e) for e in regions),
        "  ],",
        '  "legacy": [',
        ",\n".join("    " + entry(e) for e in legacy_entries),
        "  ],",
        '  "aliases": ' + json.dumps(body["aliases"], ensure_ascii=False, separators=(", ", ": ")),
        "}",
    ]
    return "\n".join(parts) + "\n", version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", type=Path, default=HERE / ".cache")
    parser.add_argument("--refresh", action="store_true", help="re-download every source")
    parser.add_argument("--check", action="store_true", help="verify committed outputs match a rebuild; write nothing")
    args = parser.parse_args()
    cache = args.cache_dir

    fetched = [fetch(key, cache, args.refresh) for key in (
        "kemendagri", "geoboundaries", "geonames_id", "geonames_cities", "geonames_countries",
        "natural_earth", "natural_earth_places")]
    by_key = {f.key: f for f in fetched}
    offices, offices_source = fetch_osm_offices(cache, args.refresh)
    fetched.append(offices_source)
    print(f"sources ready in {cache}", file=sys.stderr)

    areas = parse_kemendagri(by_key["kemendagri"].path)
    id_places = load_geonames_zip(by_key["geonames_id"].path, "ID.txt", lambda p: p.fclass == "P" or p.fcode == "ADM3")
    id_result = indonesia.build(
        areas,
        load_geoboundaries(by_key["geoboundaries"].path),
        id_places,
        sparql(indonesia.WIKIDATA_REGENCIES, cache, args.refresh),
        offices,
        lambda query: nominatim_places(query, cache, args.refresh),
    )
    country_entries, country_findings, excluded = countries.build(
        load_country_info(by_key["geonames_countries"].path),
        load_geonames_zip(by_key["geonames_cities"].path, "cities500.txt", lambda p: p.fclass == "P"),
        sparql(countries.WIKIDATA_CAPITALS, cache, args.refresh),
        load_natural_earth(by_key["natural_earth"].path),
        load_natural_earth_capitals(by_key["natural_earth_places"].path),
    )
    aliases, legacy_entries, comparisons, legacy_findings = legacy.build(
        json.loads((HERE / "legacy_regions.json").read_text()),
        id_result.entries,
        areas,
        {p.geonameid: p for p in id_places},
        sparql(legacy.wikidata_query(), cache, args.refresh),
    )

    findings = id_result.findings + country_findings + legacy_findings
    failures = [f for f in findings if f.level == "fail"]
    ids = [str(e["id"]) for e in id_result.entries + country_entries + legacy_entries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        failures.append(indonesia.Finding("fail", "--", "catalogue", "ids", f"duplicate ids {duplicates}"))
    if failures:
        for f in failures:
            print(f"FAIL [{f.check}] {f.code} {f.name}: {f.detail}", file=sys.stderr)
        print(f"{len(failures)} verification failure(s); nothing written", file=sys.stderr)
        return 1

    regions = id_result.entries + sorted(country_entries, key=lambda e: str(e["name"]))
    catalogue, version = _catalogue_json(regions, legacy_entries, aliases)
    report_text = report.render(fetched, id_result, country_entries, excluded, legacy_entries, comparisons, findings, version)

    outputs = {**{path: catalogue for path in OUTPUTS.values()}, REPORT: report_text}
    if args.check:
        stale = [str(p.relative_to(REPO)) for p, text in outputs.items() if not p.exists() or p.read_text() != text]
        if stale:
            print("out of date: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"catalogue {version} is up to date", file=sys.stderr)
        return 0
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"catalogue {version}: {len(regions)} regions, {len(legacy_entries)} legacy, {len(aliases)} aliases", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
