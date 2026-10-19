"use client";

import * as React from "react";
import { AttributionControl, MapContainer, TileLayer, useMap, ZoomControl } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.markercluster";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
// important: after Leaflet's own stylesheets, so these overrides win on document order.
import "./map-theme.css";
import { RegionSummary } from "@/types/api";
import { useTheme } from "@/components/theme-provider";
import { BasemapConfig, OFFLINE_BASEMAP, OFFLINE_MAX_ZOOM } from "@/lib/map-basemap";
import type { CountryAreas } from "@/lib/regions/country-areas";
import { isWholeCountry } from "@/lib/regions/place";
import { OfflineBasemap } from "./offline-basemap";
import { CountryAreasLayer, countryAreaFor, focusBounds } from "./country-areas-layer";
import { createClusterIcon, createCountryChip, createPopupContent, getRegionIcon } from "./map-icons";

/** Repeated failures mean the endpoint is wrong or gone; one is a hiccup on a single tile. */
const TILE_ERROR_LIMIT = 4;

const WORLD_CENTER: L.LatLngTuple = [15, 20];
const WORLD_ZOOM = 2;
// note: one copy of the world, a little past the antimeridian. The bundled outline is drawn
// once, so panning further sideways only showed empty sea.
const WORLD_BOUNDS = L.latLngBounds([-85, -200], [85, 200]);
// Fitting never zooms in further than this, so one cluster still shows its surroundings.
const FIT_MAX_ZOOM = 6;
// A whole country with no outline to frame (the outlines failed to load) is shown from this far
// out at most: closer in, a point inside the country reads as a site.
const COUNTRY_POINT_ZOOM = 4;
const FIT_PADDING: L.PointTuple = [40, 40];

/** Where the map should look for one region: a country's main landmasses, or the point. */
function regionTarget(region: RegionSummary, areas: CountryAreas | null): L.LatLngBounds | L.LatLngTuple {
  const area = isWholeCountry(region) ? countryAreaFor(areas, region) : undefined;
  return area ? focusBounds(area) : [region.latitude, region.longitude];
}

interface FitToRegionsProps {
  regions: RegionSummary[];
  areas: CountryAreas | null;
  // false while country outlines are still loading, so the map frames once rather than twice
  areasSettled: boolean;
  fitSignal: number;
  maxZoom: number;
}

/**
 * Frames every plotted region when the set of regions changes or the operator asks for it.
 * Keyed on ids and coordinates rather than the array, which the overview poll replaces every
 * cycle: re-fitting on each poll would yank the map away from wherever the operator panned.
 */
function FitToRegions({ regions, areas, areasSettled, fitSignal, maxZoom }: FitToRegionsProps) {
  const map = useMap();
  const latest = React.useRef({ regions, areas, maxZoom });
  latest.current = { regions, areas, maxZoom };
  const signature = regions
    .map((r) => `${r.region_id}@${r.latitude},${r.longitude}`)
    .sort()
    .join("|");

  React.useEffect(() => {
    if (!map || !areasSettled) return;
    const { regions: current, areas: outlines } = latest.current;
    const ceiling = Math.min(FIT_MAX_ZOOM, latest.current.maxZoom);
    const bounds = L.latLngBounds([]);
    for (const region of current) bounds.extend(regionTarget(region, outlines));
    if (!bounds.isValid()) {
      map.setView(WORLD_CENTER, WORLD_ZOOM);
    } else if (bounds.getSouthWest().equals(bounds.getNorthEast())) {
      const onlyCountries = current.every(isWholeCountry);
      map.setView(bounds.getCenter(), onlyCountries ? Math.min(COUNTRY_POINT_ZOOM, ceiling) : ceiling);
    } else {
      map.fitBounds(bounds, { padding: FIT_PADDING, maxZoom: ceiling });
    }
  }, [map, signature, fitSignal, areasSettled]);

  return null;
}

interface FocusSelectedRegionProps {
  regions: RegionSummary[];
  areas: CountryAreas | null;
  selectedRegion: string | null;
  // bumped by "View on Map", so asking for the region already selected still moves the map
  focusSignal: number;
  focusZoom: number;
}

/**
 * Flies to the region the operator picked: a whole country framed on its main landmasses, any
 * other region on its point.
 *
 * important: keyed on the selection alone. It used to depend on the regions array, which the
 * overview poll replaces, so every refresh after a selection flew the map back to it.
 */
function FocusSelectedRegion({ regions, areas, selectedRegion, focusSignal, focusZoom }: FocusSelectedRegionProps) {
  const map = useMap();
  const latest = React.useRef({ regions, areas });
  latest.current = { regions, areas };

  React.useEffect(() => {
    if (!map || !selectedRegion) return;
    const region = latest.current.regions.find((r) => r.region_id === selectedRegion);
    if (!region) return;
    const target = regionTarget(region, latest.current.areas);
    if (target instanceof L.LatLngBounds) {
      map.flyToBounds(target, { padding: FIT_PADDING, maxZoom: focusZoom, duration: 1.5 });
    } else {
      const zoom = isWholeCountry(region) ? Math.min(COUNTRY_POINT_ZOOM, focusZoom) : focusZoom;
      map.flyTo(target, zoom, { duration: 1.5 });
    }
  }, [map, selectedRegion, focusSignal, focusZoom]);

  return null;
}

interface MarkerClusterLayerProps {
  regions: RegionSummary[];
  onSelectRegion: (regionId: string) => void;
}

function MarkerClusterLayer({ regions, onSelectRegion }: MarkerClusterLayerProps) {
  const map = useMap();
  const clusterGroupRef = React.useRef<L.MarkerClusterGroup | null>(null);

  React.useEffect(() => {
    if (!map) return;

    if (clusterGroupRef.current) {
      map.removeLayer(clusterGroupRef.current);
    }

    const clusterGroup = L.markerClusterGroup({
      iconCreateFunction: createClusterIcon,
      maxClusterRadius: 60,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      chunkedLoading: true,
      // note: no colour here, and a class of our own rather than markercluster's. It only
      // attaches .leaflet-cluster-spider-leg on its animated branch, so at rest the legs
      // carry no class at all and a stylesheet cannot reach them. Leaflet does apply
      // options.className to the SVG path in every branch, so this one always lands and the
      // stroke can follow the theme live instead of being frozen at construction.
      spiderLegPolylineOptions: {
        weight: 1.5,
        opacity: 0.6,
        className: "orcastra-spider-leg",
      },
    });

    regions.forEach((region) => {
      const country = isWholeCountry(region);
      // note: a country's chip joins the same cluster group as the pins, so a bubble still counts
      // regions and a crowded corner of the map (Singapore beside Batam) merges instead of
      // stacking chips. Its shading is drawn outside the group and stays visible either way.
      const marker = L.marker([region.latitude, region.longitude], {
        icon: country ? createCountryChip(region) : getRegionIcon(region),
        title: country ? `${region.region_name}, whole country` : region.region_name,
      });

      marker.bindPopup(createPopupContent(region), {
        className: "region-popup",
        maxWidth: 300,
      });

      marker.on("click", () => {
        onSelectRegion(region.region_id);
      });
      // Leaflet makes a marker focusable but its Enter only opens the popup and never fires
      // click, so a keyboard user could reach a region on the map and not select it.
      marker.on("keypress", (event: L.LeafletKeyboardEvent) => {
        if (event.originalEvent.key === "Enter") onSelectRegion(region.region_id);
      });

      clusterGroup.addLayer(marker);
    });

    map.addLayer(clusterGroup);
    clusterGroupRef.current = clusterGroup;

    return () => {
      if (clusterGroupRef.current) {
        map.removeLayer(clusterGroupRef.current);
        clusterGroupRef.current = null;
      }
    };
  }, [map, regions, onSelectRegion]);

  return null;
}

/**
 * MapContainer reads maxZoom once, at construction. When a failing tile endpoint drops the
 * map back to the bundled outline the ceiling has to be lowered on the live instance, or
 * the operator can keep zooming into blank land the outline cannot fill.
 */
function MaxZoomSync({ maxZoom }: { maxZoom: number }) {
  const map = useMap();

  React.useEffect(() => {
    if (!map) return;
    map.setMaxZoom(maxZoom);
    if (map.getZoom() > maxZoom) map.setZoom(maxZoom);
  }, [map, maxZoom]);

  return null;
}

interface BasemapLayerProps {
  basemap: BasemapConfig;
  onFallback: () => void;
}

/**
 * Draws whichever basemap the deployment resolved to.
 *
 * A configured tile endpoint that keeps failing falls back to the bundled outline rather
 * than leaving a grid of broken image icons, which is what the page used to degrade into
 * once the previous hardcoded CDN started refusing anonymous requests.
 */
function BasemapLayer({ basemap, onFallback }: BasemapLayerProps) {
  const { resolvedTheme } = useTheme();
  const errorCount = React.useRef(0);

  if (basemap.mode === "offline") {
    return <OfflineBasemap onLoadError={onFallback} />;
  }

  const url =
    resolvedTheme === "dark" && basemap.darkUrl !== null ? basemap.darkUrl : basemap.url;

  return (
    <TileLayer
      // Remounts the layer on a theme swap so Leaflet refetches from the other endpoint
      // instead of keeping the tiles it already had.
      key={url}
      url={url}
      attribution={basemap.attribution}
      maxZoom={basemap.maxZoom}
      {...(basemap.subdomains !== null ? { subdomains: basemap.subdomains } : {})}
      eventHandlers={{
        tileerror: () => {
          errorCount.current += 1;
          if (errorCount.current === TILE_ERROR_LIMIT) onFallback();
        },
        load: () => {
          errorCount.current = 0;
        },
      }}
    />
  );
}

export interface RegionsMapProps {
  regions: RegionSummary[];
  // outlines for the whole-country regions; null until loaded, or when they failed
  countryAreas: CountryAreas | null;
  areasSettled: boolean;
  // bumped by the card's "Fit to clusters" button
  fitSignal?: number;
  focusSignal?: number;
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
  basemap: BasemapConfig;
  onBasemapFallback?: () => void;
}

export default function RegionsMap({
  regions,
  countryAreas,
  areasSettled,
  fitSignal = 0,
  focusSignal = 0,
  selectedRegion,
  onSelectRegion,
  basemap,
  onBasemapFallback,
}: RegionsMapProps) {
  // A tile endpoint that fails at runtime drops this map back to the bundled outline for
  // the rest of the session.
  const [tilesFailed, setTilesFailed] = React.useState(false);
  const effectiveBasemap: BasemapConfig = React.useMemo(
    () => (tilesFailed || basemap.mode === "offline" ? OFFLINE_BASEMAP : basemap),
    [tilesFailed, basemap]
  );

  const handleFallback = React.useCallback(() => {
    setTilesFailed(true);
    onBasemapFallback?.();
  }, [onBasemapFallback]);

  // The bundled outline carries no streets or labels, so zooming past its useful level
  // only magnifies the same coastline. A configured tile provider keeps its own ceiling.
  const maxZoom =
    effectiveBasemap.mode === "offline" ? OFFLINE_MAX_ZOOM : effectiveBasemap.maxZoom;
  const focusZoom = Math.min(10, maxZoom);
  const countryRegions = React.useMemo(() => regions.filter(isWholeCountry), [regions]);

  return (
    <MapContainer
      center={WORLD_CENTER}
      zoom={WORLD_ZOOM}
      minZoom={1}
      maxZoom={maxZoom}
      maxBounds={WORLD_BOUNDS}
      maxBoundsViscosity={1.0}
      className="h-[400px] w-full rounded-lg z-0"
      attributionControl={false}
      zoomControl={false}
      scrollWheelZoom={true}
    >
      <MaxZoomSync maxZoom={maxZoom} />

      {/* important: before the basemap, not after. Leaflet only wires an attribution's
          removal through its `layeradd` handler; layers already present when the control
          mounts are scanned and added directly, with no cleanup hook (leaflet-src.js, the
          block its own author marked "TODO ugly, refactor"). Mounting the control second
          left a failed tile provider credited underneath the basemap that replaced it. */}
      <AttributionControl position="bottomright" prefix={false} />

      <BasemapLayer basemap={effectiveBasemap} onFallback={handleFallback} />

      <ZoomControl position="topright" />

      <FitToRegions
        regions={regions}
        areas={countryAreas}
        areasSettled={areasSettled}
        fitSignal={fitSignal}
        maxZoom={maxZoom}
      />

      <FocusSelectedRegion
        regions={regions}
        areas={countryAreas}
        selectedRegion={selectedRegion}
        focusSignal={focusSignal}
        focusZoom={focusZoom}
      />

      <CountryAreasLayer regions={countryRegions} areas={countryAreas} onSelectRegion={onSelectRegion} />

      <MarkerClusterLayer regions={regions} onSelectRegion={onSelectRegion} />
    </MapContainer>
  );
}
