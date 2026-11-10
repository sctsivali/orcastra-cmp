#!/usr/bin/env node
/**
 * Regenerates the offline basemap served from public/geo/world-land-50m.v1.json.
 *
 * The regions map draws its landmass from this vendored file instead of a third-party
 * tile CDN, so the page keeps working on self-hosted and air-gapped installs. This
 * script makes the 460 KB file reproducible from its public source.
 *
 * Source:  Natural Earth 1:50m Admin 0 Countries
 * License: public domain (https://www.naturalearthdata.com/about/terms-of-use/)
 *
 * Run from the frontend directory, no dependencies required:
 *   node scripts/build-basemap.mjs
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const SOURCE_URL =
  "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson";

// Douglas-Peucker tolerance in degrees, and coordinate precision in decimal places.
// 0.05 / 2dp keeps the Indonesian archipelago legible at the zoom levels this map uses
// (4 to 7) while cutting the source from 3.3 MB to roughly 460 KB.
const TOLERANCE = 0.05;
const PRECISION = 2;

// A ring below this many points after simplification has collapsed into a sliver and is
// dropped rather than drawn as a degenerate shape.
const MIN_RING_POINTS = 4;

const OUT_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../public/geo/world-land-50m.v1.json"
);

// Iterative Douglas-Peucker. Recursion would blow the stack on the larger coastlines.
function simplifyRing(points, tolerance) {
  if (points.length < 3) return points;

  const keep = new Array(points.length).fill(false);
  keep[0] = true;
  keep[points.length - 1] = true;

  const stack = [[0, points.length - 1]];
  while (stack.length > 0) {
    const [start, end] = stack.pop();
    if (end <= start + 1) continue;

    const [ax, ay] = points[start];
    const [bx, by] = points[end];
    const dx = bx - ax;
    const dy = by - ay;
    const span = Math.hypot(dx, dy);

    let worst = -1;
    let worstIndex = -1;
    for (let i = start + 1; i < end; i += 1) {
      const [px, py] = points[i];
      const distance =
        span === 0
          ? Math.hypot(px - ax, py - ay)
          : Math.abs(dx * (ay - py) - (ax - px) * dy) / span;
      if (distance > worst) {
        worst = distance;
        worstIndex = i;
      }
    }

    if (worst > tolerance) {
      keep[worstIndex] = true;
      stack.push([start, worstIndex], [worstIndex, end]);
    }
  }

  return points.filter((_, index) => keep[index]);
}

function roundRing(points, precision) {
  const factor = 10 ** precision;
  const rounded = [];
  for (const [x, y] of points) {
    const point = [Math.round(x * factor) / factor, Math.round(y * factor) / factor];
    const previous = rounded[rounded.length - 1];
    // Rounding collapses neighbours onto each other; keeping duplicates would bloat the
    // file with points that draw nothing.
    if (!previous || previous[0] !== point[0] || previous[1] !== point[1]) {
      rounded.push(point);
    }
  }
  if (rounded.length >= 3) {
    const first = rounded[0];
    const last = rounded[rounded.length - 1];
    if (first[0] !== last[0] || first[1] !== last[1]) rounded.push([first[0], first[1]]);
  }
  return rounded;
}

function processRing(ring) {
  const flattened = ring.map(([x, y]) => [x, y]);
  const result = roundRing(simplifyRing(flattened, TOLERANCE), PRECISION);
  return result.length >= MIN_RING_POINTS ? result : null;
}

function processPolygon(rings) {
  const kept = rings.map(processRing).filter(Boolean);
  // Collapsed rings are dropped, and a polygon with no ring left is dropped with them.
  return kept.length > 0 ? kept : null;
}

function processGeometry(geometry) {
  if (geometry.type === "Polygon") {
    const coordinates = processPolygon(geometry.coordinates);
    return coordinates ? { type: "Polygon", coordinates } : null;
  }
  if (geometry.type === "MultiPolygon") {
    const coordinates = geometry.coordinates.map(processPolygon).filter(Boolean);
    return coordinates.length > 0 ? { type: "MultiPolygon", coordinates } : null;
  }
  return null;
}

async function main() {
  process.stdout.write(`Fetching ${SOURCE_URL}\n`);
  const response = await fetch(SOURCE_URL);
  if (!response.ok) {
    throw new Error(`Source returned HTTP ${response.status}`);
  }
  const source = await response.json();

  const features = [];
  for (const feature of source.features) {
    const geometry = processGeometry(feature.geometry);
    // Properties are dropped: the layer is a backdrop with no labels and no
    // interaction, so every property is dead weight over the wire.
    if (geometry) features.push({ type: "Feature", properties: {}, geometry });
  }

  const output = {
    type: "FeatureCollection",
    // Provenance travels with the data, so the licence can be read from the file itself.
    source: "Natural Earth 1:50m Admin 0 Countries (public domain)",
    generator: "scripts/build-basemap.mjs",
    simplification: { tolerance: TOLERANCE, precision: PRECISION },
    features,
  };

  const serialised = JSON.stringify(output);
  mkdirSync(dirname(OUT_PATH), { recursive: true });
  writeFileSync(OUT_PATH, serialised);

  const points = features.reduce((total, feature) => {
    const polygons =
      feature.geometry.type === "Polygon"
        ? [feature.geometry.coordinates]
        : feature.geometry.coordinates;
    return total + polygons.flat().reduce((sum, ring) => sum + ring.length, 0);
  }, 0);

  const raw = Buffer.byteLength(serialised);
  const gzipped = gzipSync(serialised, { level: 9 }).length;
  process.stdout.write(
    `Wrote ${OUT_PATH}\n` +
      `  features ${features.length}, points ${points}\n` +
      `  ${(raw / 1024).toFixed(0)} KB raw, ${(gzipped / 1024).toFixed(0)} KB gzipped\n`
  );
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
