"""Geometry helpers for the region catalogue build. Standard library only.

Coordinates are (lat, lng) tuples everywhere in this package. GeoJSON and TopoJSON store
(lng, lat); the loaders in sources.py convert at the edge so nothing here has to guess.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

LatLng = Tuple[float, float]
Ring = List[LatLng]
# A polygon is an outer ring followed by zero or more holes.
Polygon = List[Ring]
MultiPolygon = List[Polygon]

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: LatLng, b: LatLng) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _point_in_ring(point: LatLng, ring: Sequence[LatLng]) -> bool:
    # Even-odd ray cast along the latitude line through the point.
    lat, lng = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        lat_i, lng_i = ring[i]
        lat_j, lng_j = ring[j]
        if (lat_i > lat) != (lat_j > lat):
            cross_lng = lng_i + (lat - lat_i) * (lng_j - lng_i) / (lat_j - lat_i)
            if lng < cross_lng:
                inside = not inside
        j = i
    return inside


def point_in_multipolygon(point: LatLng, shape: MultiPolygon) -> bool:
    for polygon in shape:
        if not polygon or not _point_in_ring(point, polygon[0]):
            continue
        if not any(_point_in_ring(point, hole) for hole in polygon[1:]):
            return True
    return False


def point_in_rings(point: LatLng, rings: Sequence[Ring]) -> bool:
    """Even-odd over every ring, for sources that do not say which ring is a hole.

    A point inside one island ring is inside; a point inside an outer ring and a hole ring
    is outside. Both read correctly without knowing the ring roles.
    """
    return sum(1 for ring in rings if _point_in_ring(point, ring)) % 2 == 1


def _segment_distance_km(p: LatLng, a: LatLng, b: LatLng) -> float:
    # Local equirectangular projection around p. Exact enough for the few-km tolerances used
    # here; it only answers "is the point near this edge".
    k = math.cos(math.radians(p[0]))
    px, py = 0.0, 0.0
    ax, ay = (a[1] - p[1]) * k, a[0] - p[0]
    bx, by = (b[1] - p[1]) * k, b[0] - p[0]
    dx, dy = bx - ax, by - ay
    seg = dx * dx + dy * dy
    t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(cx, cy) * (math.pi / 180) * EARTH_RADIUS_KM


def distance_to_boundary_km(point: LatLng, shape: MultiPolygon) -> float:
    best = math.inf
    for polygon in shape:
        for ring in polygon:
            for i in range(len(ring) - 1):
                best = min(best, _segment_distance_km(point, ring[i], ring[i + 1]))
    return best


def contains(point: LatLng, shape: MultiPolygon, tolerance_km: float = 0.0) -> bool:
    """Inside the shape, or within tolerance_km of its edge.

    The tolerance exists because simplified boundaries cut corners: a city on the waterfront
    can fall a few hundred metres into the sea of a generalised coastline.
    """
    if point_in_multipolygon(point, shape):
        return True
    return tolerance_km > 0 and distance_to_boundary_km(point, shape) <= tolerance_km


def ring_area(ring: Sequence[LatLng]) -> float:
    """Shoelace area in square degrees of longitude scaled by cos(latitude).

    Used only to rank rings against each other, so a flat projection around the ring's own
    mean latitude is enough. Unscaled degrees would rank Alaska above most of Europe.
    """
    if len(ring) < 3:
        return 0.0
    k = math.cos(math.radians(sum(p[0] for p in ring) / len(ring)))
    total = 0.0
    for i in range(len(ring)):
        lat_a, lng_a = ring[i]
        lat_b, lng_b = ring[(i + 1) % len(ring)]
        total += lng_a * k * lat_b - lng_b * k * lat_a
    return abs(total) / 2


def simplify_ring(ring: Sequence[LatLng], tolerance: float) -> Ring:
    # Iterative Douglas-Peucker (recursion would overflow on the longer coastlines); the same
    # algorithm frontend/scripts/build-basemap.mjs applies to the basemap.
    points = list(ring)
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        ay, ax = points[start]
        by, bx = points[end]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        worst, worst_index = -1.0, -1
        for i in range(start + 1, end):
            py, px = points[i]
            distance = math.hypot(px - ax, py - ay) if span == 0 else abs(dx * (ay - py) - (ax - px) * dy) / span
            if distance > worst:
                worst, worst_index = distance, i
        if worst > tolerance:
            keep[worst_index] = True
            stack += [(start, worst_index), (worst_index, end)]
    return [p for p, k in zip(points, keep) if k]


def interior_point(polygon: Polygon, grid: int = 16, rounds: int = 4) -> LatLng:
    """The point inside the polygon farthest from its edge, by a deterministic refining grid.

    For the few countries whose published label point lies in the sea between islands. A
    centroid would not do: a crescent or an archipelago puts its centroid in the water too.
    """
    shape: MultiPolygon = [polygon]
    s, w, n, e = bbox(shape)
    best: Tuple[float, LatLng] = (-1.0, (0.0, 0.0))
    for _ in range(rounds):
        step_lat, step_lng = (n - s) / grid, (e - w) / grid
        for i in range(grid + 1):
            for j in range(grid + 1):
                candidate = (s + i * step_lat, w + j * step_lng)
                if point_in_multipolygon(candidate, shape):
                    depth = distance_to_boundary_km(candidate, shape)
                    if depth > best[0]:
                        best = (depth, candidate)
        if best[0] < 0:
            raise ValueError("no grid point fell inside the polygon")
        lat, lng = best[1]
        s, n = lat - step_lat, lat + step_lat
        w, e = lng - step_lng, lng + step_lng
    return best[1]


def bbox(shape: MultiPolygon) -> Tuple[float, float, float, float]:
    lats = [p[0] for polygon in shape for ring in polygon for p in ring]
    lngs = [p[1] for polygon in shape for ring in polygon for p in ring]
    return min(lats), min(lngs), max(lats), max(lngs)


def geojson_to_multipolygon(geometry: dict) -> MultiPolygon:
    def ring(coords: Iterable[Sequence[float]]) -> Ring:
        return [(float(c[1]), float(c[0])) for c in coords]

    if geometry["type"] == "Polygon":
        return [[ring(r) for r in geometry["coordinates"]]]
    if geometry["type"] == "MultiPolygon":
        return [[ring(r) for r in poly] for poly in geometry["coordinates"]]
    raise ValueError(f"unsupported geometry type {geometry['type']}")
