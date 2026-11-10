"""Whole-country outlines for the regions map, and the one point each country is placed at.

A cluster registered in a country outside Indonesia is shown as that whole country, because the
site inside it is rarely known when the cluster is registered. The outlines come from the same
Natural Earth map units the catalogue checks its country points against, so every country entry
has exactly one shape and its point lies inside it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from geo import (
    LatLng,
    MultiPolygon,
    Polygon,
    Ring,
    bbox,
    distance_to_boundary_km,
    interior_point,
    point_in_multipolygon,
    point_in_rings,
    ring_area,
    simplify_ring,
)
from indonesia import Finding
from sources import MapUnit

# important: /geo/* is served with an immutable one-year cache (frontend/next.config.js), so any
# change to this file's content must ship under a new version in the name.
AREAS_FILE = "country-areas.v1.json"
# The basemap's simplification (frontend/scripts/build-basemap.mjs), so outlines and coast agree.
TOLERANCE_DEG = 0.05
PRECISION = 2
# A micro-state (Monaco, Vatican, Gibraltar) collapses at the basemap tolerance. Its largest ring
# is a dozen points or fewer as published, so it is kept unsimplified at this precision instead of
# vanishing from the map.
TINY_PRECISION = 4
MIN_RING_POINTS = 4
# The map frames a country on its main landmasses: polygons at least this share of the largest.
# 0.25 frames Japan on Honshu and Hokkaido, the Philippines on Luzon and Mindanao, and the United
# States on the lower 48 rather than a box stretched to Alaska.
FOCUS_SHARE = 0.25
MAX_FOCUS_SPAN_DEG = 180.0


@dataclass
class CountryShape:
    iso: str
    main: MapUnit
    shape: MultiPolygon
    dropped: List[str] = field(default_factory=list)
    readmitted: List[str] = field(default_factory=list)


@dataclass
class AreasSummary:
    features: int
    size: int
    tiny: List[str]
    dropped: List[Tuple[str, str]]
    readmitted: List[Tuple[str, str]]


def _polygon_area(polygon: Polygon) -> float:
    return ring_area(polygon[0]) if polygon else 0.0


def country_shape(iso: str, units: Sequence[MapUnit]) -> CountryShape:
    """The country as the map shades it: the units governed by the same ADMIN as the largest one.

    Natural Earth files some dependencies under their sovereign's ISO code (the US Minor Outlying
    Islands under US, Clipperton under FR, the Coral Sea Islands under AU). They are not part of
    the country a cluster is registered in, so they are left out. A unit of another ADMIN that
    sits in a hole of the kept shape (the Baikonur lease inside Kazakhstan) is let back in, or the
    country would be drawn with a hole in it.
    """
    main = max(units, key=lambda u: sum(_polygon_area(p) for p in u.shape))
    result = CountryShape(iso, main, [p for u in units if u.admin == main.admin for p in u.shape])
    holes = [ring for polygon in result.shape for ring in polygon[1:]]
    for unit in units:
        if unit.admin == main.admin:
            continue
        if holes and point_in_rings(unit.label, holes):
            result.shape.extend(unit.shape)
            result.readmitted.append(unit.admin)
        else:
            result.dropped.append(unit.admin)
    return result


def representative_point(country: CountryShape) -> Tuple[LatLng, Optional[str]]:
    """Natural Earth's label point for the main unit, or the interior of the largest polygon.

    The label point is what a cartographer would letter the country at, so it reads as "the
    country" rather than any one city in it. Where it lies in the sea between islands (New
    Zealand, Equatorial Guinea) the point moves to the deepest spot of the largest polygon, and
    the returned note says why.
    """
    label = country.main.label
    if point_in_multipolygon(label, country.shape):
        return label, None
    largest = max(country.shape, key=_polygon_area)
    offshore_km = distance_to_boundary_km(label, country.shape)
    return interior_point(largest), f"Natural Earth label point lies {offshore_km:.0f} km outside the country; placed inside its largest polygon"


def _round_ring(ring: Ring, tolerance: float, precision: int) -> Optional[List[List[float]]]:
    out: List[List[float]] = []
    for lat, lng in simplify_ring(ring, tolerance):
        point = [round(lng, precision), round(lat, precision)]
        # Rounding collapses neighbours onto each other; a duplicate draws nothing.
        if not out or out[-1] != point:
            out.append(point)
    if len(out) >= 3 and out[0] != out[-1]:
        out.append(list(out[0]))
    return out if len(out) >= MIN_RING_POINTS else None


def _simplified(shape: MultiPolygon) -> List[List[List[List[float]]]]:
    polygons = []
    for polygon in shape:
        rings = [_round_ring(ring, TOLERANCE_DEG, PRECISION) for ring in polygon]
        if rings and rings[0] is not None:  # Holes mean nothing once the outer ring has collapsed.
            polygons.append([r for r in rings if r is not None])
    return polygons


def _focus(shape: MultiPolygon, point: LatLng) -> List[float]:
    largest = max(_polygon_area(p) for p in shape)
    main = [p for p in shape if _polygon_area(p) >= largest * FOCUS_SHARE]
    s, w, n, e = bbox(main)
    s, w, n, e = min(s, point[0]), min(w, point[1]), max(n, point[0]), max(e, point[1])
    # Rounded outward, so the box never cuts a sliver off the country it frames.
    return [math.floor(s * 100) / 100, math.floor(w * 100) / 100, math.ceil(n * 100) / 100, math.ceil(e * 100) / 100]


def _number(value: float) -> object:
    return int(value) if value == int(value) else value


def render(shapes: Dict[str, CountryShape], points: Dict[str, LatLng]) -> Tuple[str, List[Finding], AreasSummary]:
    """One GeoJSON feature per country entry, keyed by ISO code, one feature per line."""
    findings: List[Finding] = []
    lines: List[str] = []
    tiny: List[str] = []
    for iso in sorted(points):
        country = shapes[iso]
        polygons = _simplified(country.shape)
        if not polygons:
            ring = _round_ring(max(country.shape, key=_polygon_area)[0], 0.0, TINY_PRECISION)
            if ring is None:
                findings.append(Finding("fail", iso, iso, "area", "outline collapses even unsimplified"))
                continue
            polygons = [[ring]]
            tiny.append(iso)
        focus = _focus(country.shape, points[iso])
        if focus[3] - focus[1] > MAX_FOCUS_SPAN_DEG:
            findings.append(Finding("fail", iso, iso, "area", f"focus box spans {focus[3] - focus[1]:.1f} degrees of longitude"))
            continue
        coordinates = [[[[_number(x), _number(y)] for x, y in ring] for ring in polygon] for polygon in polygons]
        feature = {
            "type": "Feature",
            "id": iso,
            "properties": {"focus": [_number(v) for v in focus]},
            "geometry": {"type": "MultiPolygon", "coordinates": coordinates},
        }
        lines.append(json.dumps(feature, separators=(",", ":")))
    text = (
        '{"type":"FeatureCollection",\n'
        '"source":"Natural Earth 5.1.2 1:10m admin 0 map units, public domain; built by scripts/regions/build_regions.py",\n'
        '"features":[\n' + ",\n".join(lines) + "\n]}\n"
    )
    summary = AreasSummary(
        features=len(lines),
        size=len(text.encode()),
        tiny=tiny,
        dropped=[(iso, admin) for iso in sorted(points) for admin in shapes[iso].dropped],
        readmitted=[(iso, admin) for iso in sorted(points) for admin in shapes[iso].readmitted],
    )
    return text, findings, summary
