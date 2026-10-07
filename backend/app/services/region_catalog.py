"""The bundled region catalogue: every Indonesian kabupaten and kota, plus one entry per country.

Generated, never edited by hand. scripts/regions/build_regions.py writes app/data/regions_catalog.json
from verified sources (scripts/regions/verify-report.md records how each point was checked), and
writes a byte-identical copy that the frontend bundles for its location picker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "regions_catalog.json"

# The location keys the catalogue owns. Everything else on a stored location, such as the
# facility name and provider, is the operator's and is never touched.
CATALOG_FIELDS = (
    "region_id", "region_name", "official_name", "country_code", "country_name", "city", "province", "admin_code",
    "latitude", "longitude", "timezone", "continent",
)


@dataclass(frozen=True)
class RegionCatalog:
    version: str
    entries: Dict[str, Dict[str, Any]]  # selectable and hidden legacy entries, by id
    aliases: Dict[str, str]  # retired id -> the id that replaced it

    def resolve(self, region_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not region_id:
            return None
        return self.entries.get(self.aliases.get(region_id, region_id))

    def canonical_location(self, location: Dict[str, Any]) -> Dict[str, Any]:
        """The location with every catalogue-owned field taken from the catalogue.

        A retired id is rewritten to the one that replaced it. A region id the catalogue does
        not know, such as one an API client defines for itself, passes through unchanged.
        """
        entry = self.resolve(location.get("region_id"))
        if entry is None:
            return dict(location)
        out = {k: v for k, v in location.items() if k not in CATALOG_FIELDS}
        out.update({
            "region_id": entry["id"],
            "region_name": entry["name"],
            "country_code": entry["country_code"],
            "country_name": entry["country_name"],
            "city": entry["city"],
            "latitude": entry["latitude"],
            "longitude": entry["longitude"],
            "timezone": entry["timezone"],
            "continent": entry["continent"],
        })
        for key in ("official_name", "province", "admin_code"):
            if entry.get(key):
                out[key] = entry[key]
        return out


@lru_cache(maxsize=1)
def get_region_catalog() -> RegionCatalog:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entries = {e["id"]: e for e in data["regions"] + data["legacy"]}
    return RegionCatalog(version=data["version"], entries=entries, aliases=dict(data["aliases"]))
