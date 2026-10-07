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
import { OfflineBasemap } from "./offline-basemap";
import { createClusterIcon, createPopupContent, getRegionIcon } from "./map-icons";

/** Repeated failures mean the endpoint is wrong or gone; one is a hiccup on a single tile. */
const TILE_ERROR_LIMIT = 4;

const WORLD_CENTER: L.LatLngTuple = [15, 20];
const WORLD_ZOOM = 2;
// note: one copy of the world, a little past the antimeridian. The bundled outline is drawn
// once, so panning further sideways only showed empty sea.
const WORLD_BOUNDS = L.latLngBounds([-85, -200], [85, 200]);
// Fitting never zooms in further than this, so one cluster still shows its surroundings.
const FIT_MAX_ZOOM = 6;

/**
 * Frames every plotted region when the set of regions changes or the operator asks for it.
 * Keyed on ids and coordinates rather than the array, which the overview poll replaces every
 * cycle: re-fitting on each poll would yank the map away from wherever the operator panned.
 */
function FitToRegions({ regions, fitSignal, maxZoom }: { regions: RegionSummary[]; fitSignal: number; maxZoom: number }) {
  const map = useMap();
  const latest = React.useRef({ regions, maxZoom });
  latest.current = { regions, maxZoom };
  const signature = regions
    .map((r) => `${r.region_id}@${r.latitude},${r.longitude}`)
    .sort()
    .join("|");

  React.useEffect(() => {
    if (!map) return;
    const points = latest.current.regions.map((r) => [r.latitude, r.longitude] as L.LatLngTuple);
    const ceiling = Math.min(FIT_MAX_ZOOM, latest.current.maxZoom);
    if (points.length === 0) map.setView(WORLD_CENTER, WORLD_ZOOM);
    else if (points.length === 1) map.setView(points[0], ceiling);
    else map.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: ceiling });
  }, [map, signature, fitSignal]);

  return null;
}

interface MarkerClusterLayerProps {
  regions: RegionSummary[];
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
  focusZoom: number;
}

function MarkerClusterLayer({
  regions,
  selectedRegion,
  onSelectRegion,
  focusZoom,
}: MarkerClusterLayerProps) {
  const map = useMap();
  const clusterGroupRef = React.useRef<L.MarkerClusterGroup | null>(null);

  React.useEffect(() => {
    if (selectedRegion && map) {
      const region = regions.find((r) => r.region_id === selectedRegion);
      if (region) {
        map.flyTo([region.latitude, region.longitude], focusZoom, { duration: 1.5 });
      }
    }
  }, [selectedRegion, regions, map, focusZoom]);

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
      const marker = L.marker([region.latitude, region.longitude], {
        icon: getRegionIcon(region),
        title: region.region_name,
      });

      marker.bindPopup(createPopupContent(region), {
        className: "region-popup",
        maxWidth: 300,
      });

      marker.on("click", () => {
        onSelectRegion(region.region_id);
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
  // bumped by the card's "Fit to clusters" button
  fitSignal?: number;
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
  basemap: BasemapConfig;
  onBasemapFallback?: () => void;
}

export default function RegionsMap({
  regions,
  fitSignal = 0,
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

      <FitToRegions regions={regions} fitSignal={fitSignal} maxZoom={maxZoom} />

      <MarkerClusterLayer
        regions={regions}
        selectedRegion={selectedRegion}
        onSelectRegion={onSelectRegion}
        focusZoom={focusZoom}
      />
    </MapContainer>
  );
}
