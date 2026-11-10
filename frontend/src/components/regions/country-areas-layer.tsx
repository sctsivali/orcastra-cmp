"use client";

import * as React from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { RegionSummary } from "@/types/api";
import { OFFLINE_BASEMAP_ATTRIBUTION, escapeHtml } from "@/lib/map-basemap";
import type { CountryArea, CountryAreas } from "@/lib/regions/country-areas";
import { createPopupContent, regionStatus } from "./map-icons";
import { plural } from "@/lib/utils";

// important: above overlayPane (400), where the offline basemap's canvas lives and would take
// every pointer event, and below markers (600), tooltips (650) and popups (700). Tiles sit at
// 200, so tile mode stacks the same way.
const PANE = "orcastra-country-areas";
const PANE_Z_INDEX = "450";

export function countryAreaFor(areas: CountryAreas | null, region: RegionSummary): CountryArea | undefined {
  return areas?.get(region.country_code.toUpperCase());
}

export function focusBounds(area: CountryArea): L.LatLngBounds {
  const [south, west, north, east] = area.properties.focus;
  return L.latLngBounds([south, west], [north, east]);
}

function areaLabel(region: RegionSummary): string {
  const clusters = plural(region.node_count, "cluster");
  return `${region.country_name}, ${clusters}`;
}

interface CountryAreasLayerProps {
  // Whole-country regions only.
  regions: RegionSummary[];
  areas: CountryAreas | null;
  onSelectRegion: (regionId: string) => void;
}

/**
 * Shades each whole-country region across its full outline, in its status colour.
 *
 * SVG rather than canvas: there is one path per country in the fleet, and an SVG path takes a
 * class, so the fill follows the theme tokens in map-theme.css and the hover is plain CSS. The
 * chip in the marker layer stays the keyboard target.
 */
export function CountryAreasLayer({ regions, areas, onSelectRegion }: CountryAreasLayerProps) {
  const map = useMap();
  const rendererRef = React.useRef<L.SVG | null>(null);
  // Read when a tooltip or popup opens, so counts stay current without a redraw on every poll.
  const latest = React.useRef(regions);
  latest.current = regions;
  const onSelectRef = React.useRef(onSelectRegion);
  onSelectRef.current = onSelectRegion;

  const signature = regions
    .map((r) => `${r.region_id}:${regionStatus(r)}`)
    .sort()
    .join("|");

  // One renderer per map. Leaflet adds a renderer to the map on first use and never takes it
  // away, so creating one per redraw would leave an orphaned <svg> behind each poll.
  React.useEffect(() => {
    if (!map) return;
    if (!map.getPane(PANE)) map.createPane(PANE).style.zIndex = PANE_Z_INDEX;
    const renderer = L.svg({ pane: PANE, padding: 0.5 });
    rendererRef.current = renderer;
    return () => {
      renderer.remove();
      rendererRef.current = null;
    };
  }, [map]);

  React.useEffect(() => {
    const renderer = rendererRef.current;
    if (!map || !areas || !renderer) return;

    const group = L.featureGroup();
    for (const region of latest.current) {
      const area = countryAreaFor(areas, region);
      if (!area) continue;
      const regionId = region.region_id;
      const current = () => latest.current.find((r) => r.region_id === regionId) ?? region;
      // The outline's own id is the ISO code that matched it, so it is always two letters.
      const className = `country-area country-area--${regionStatus(region)} country-area-${String(area.id).toLowerCase()}`;
      const layer = L.geoJSON(area, {
        attribution: OFFLINE_BASEMAP_ATTRIBUTION,
        style: { renderer, className, weight: 1 },
      });
      layer.bindTooltip(() => escapeHtml(areaLabel(current())), {
        sticky: true,
        direction: "top",
        className: "country-area-tooltip",
      });
      layer.bindPopup(() => createPopupContent(current()), { className: "region-popup", maxWidth: 300 });
      layer.on("click", () => onSelectRef.current(regionId));
      group.addLayer(layer);
    }
    group.addTo(map);

    return () => {
      group.remove();
    };
  }, [map, areas, signature]);

  return null;
}
