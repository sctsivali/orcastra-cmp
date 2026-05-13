"use client";

import * as React from "react";
import { AttributionControl, MapContainer, TileLayer, useMap, ZoomControl } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.markercluster";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import { RegionSummary } from "@/types/api";
import { INDONESIA_BOUNDS } from "@/lib/regions-data";

// Custom marker icons based on status
const createCustomIcon = (color: string): L.DivIcon => {
  return L.divIcon({
    className: "custom-marker",
    html: `
      <div style="
        background-color: ${color};
        width: 24px;
        height: 24px;
        border-radius: 50%;
        border: 3px solid white;
        box-shadow: 0 2px 5px rgba(0,0,0,0.3);
        display: flex;
        align-items: center;
        justify-content: center;
      ">
        <div style="
          width: 8px;
          height: 8px;
          background-color: white;
          border-radius: 50%;
        "></div>
      </div>
    `,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    popupAnchor: [0, -12],
  });
};

// Get icon based on region status
function getRegionIcon(region: RegionSummary): L.DivIcon {
  const onlineCount = region.nodes.filter((n) => n.status === "online").length;
  const totalCount = region.nodes.length;

  if (onlineCount === totalCount) {
    return createCustomIcon("#10b981"); // All online - green
  } else if (onlineCount > 0) {
    return createCustomIcon("#f59e0b"); // Some online - amber
  } else {
    return createCustomIcon("#ef4444"); // All offline - red
  }
}

// Create cluster icon
function createClusterIcon(cluster: L.MarkerCluster): L.DivIcon {
  const count = cluster.getChildCount();
  let size = "small";
  let color = "#6366f1"; // indigo

  if (count >= 10) {
    size = "large";
    color = "#8b5cf6"; // purple
  } else if (count >= 5) {
    size = "medium";
    color = "#a855f7"; // violet
  }

  const sizeMap = { small: 30, medium: 40, large: 50 };
  const dimension = sizeMap[size as keyof typeof sizeMap];

  return L.divIcon({
    html: `
      <div style="
        background-color: ${color};
        width: ${dimension}px;
        height: ${dimension}px;
        border-radius: 50%;
        border: 3px solid white;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-weight: bold;
        font-size: ${size === "large" ? "16px" : size === "medium" ? "14px" : "12px"};
      ">
        ${count}
      </div>
    `,
    className: "custom-cluster-icon",
    iconSize: L.point(dimension, dimension),
  });
}

// Format bytes to human readable
function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

// Create popup content for a region
function createPopupContent(region: RegionSummary): string {
  const gpuSection = region.hardware.gpu_total > 0 
    ? `<div style="display: flex; justify-content: space-between;">
         <span>GPU:</span>
         <span style="font-weight: 500;">${region.hardware.gpu_total} cards</span>
       </div>`
    : "";

  const facilitySection = region.facility_name
    ? `<p style="font-size: 11px; color: #888; margin-bottom: 4px; display: flex; align-items: center; gap: 4px;">
        <span style="display: inline-block; width: 8px; height: 8px; background: #6366f1; border-radius: 2px;"></span>
        ${region.facility_name}${region.facility_provider ? ` — ${region.facility_provider}` : ""}
      </p>`
    : "";

  return `
    <div style="min-width: 200px; padding: 4px;">
      <h3 style="font-weight: bold; font-size: 14px; margin-bottom: 4px;">${region.region_name}</h3>
      <p style="font-size: 12px; color: #666; margin-bottom: 4px;">
        ${region.city}, ${region.country_name}
      </p>
      ${facilitySection}
      
      <div style="font-size: 12px; display: flex; flex-direction: column; gap: 4px; margin-top: 8px;">
        <div style="display: flex; justify-content: space-between;">
          <span>Nodes:</span>
          <span style="font-weight: 500;">${region.node_count}</span>
        </div>
        <div style="display: flex; justify-content: space-between;">
          <span>Instances:</span>
          <span style="font-weight: 500;">${region.instance_count}</span>
        </div>
        <div style="display: flex; justify-content: space-between;">
          <span>CPU:</span>
          <span style="font-weight: 500;">${region.hardware.cpu_total} cores</span>
        </div>
        <div style="display: flex; justify-content: space-between;">
          <span>Memory:</span>
          <span style="font-weight: 500;">${formatBytes(region.hardware.memory_total)}</span>
        </div>
        ${gpuSection}
      </div>
    </div>
  `;
}

interface MarkerClusterLayerProps {
  regions: RegionSummary[];
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
}

// Component to manage marker cluster layer
function MarkerClusterLayer({ regions, selectedRegion, onSelectRegion }: MarkerClusterLayerProps) {
  const map = useMap();
  const clusterGroupRef = React.useRef<L.MarkerClusterGroup | null>(null);

  // Handle fly to selected region
  React.useEffect(() => {
    if (selectedRegion && map) {
      const region = regions.find((r) => r.region_id === selectedRegion);
      if (region) {
        map.flyTo([region.latitude, region.longitude], 10, { duration: 1.5 });
      }
    }
  }, [selectedRegion, regions, map]);

  // Create and manage cluster group
  React.useEffect(() => {
    if (!map) return;

    // Remove existing cluster group
    if (clusterGroupRef.current) {
      map.removeLayer(clusterGroupRef.current);
    }

    // Create new cluster group
    const clusterGroup = L.markerClusterGroup({
      iconCreateFunction: createClusterIcon,
      maxClusterRadius: 60,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      chunkedLoading: true,
      spiderLegPolylineOptions: {
        weight: 1.5,
        color: "#6366f1",
        opacity: 0.5,
      },
    });

    // Add markers for each region
    regions.forEach((region) => {
      const marker = L.marker([region.latitude, region.longitude], {
        icon: getRegionIcon(region),
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

interface RegionsMapProps {
  regions: RegionSummary[];
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
}

export default function RegionsMap({
  regions,
  selectedRegion,
  onSelectRegion,
}: RegionsMapProps) {
  // Create max bounds for Indonesia to restrict panning
  const maxBounds = L.latLngBounds(
    [INDONESIA_BOUNDS.maxBounds.south, INDONESIA_BOUNDS.maxBounds.west],  // Southwest corner
    [INDONESIA_BOUNDS.maxBounds.north, INDONESIA_BOUNDS.maxBounds.east]   // Northeast corner
  );

  return (
    <MapContainer
      center={[INDONESIA_BOUNDS.center.lat, INDONESIA_BOUNDS.center.lng]}
      zoom={INDONESIA_BOUNDS.zoom}
      minZoom={INDONESIA_BOUNDS.minZoom}
      maxZoom={INDONESIA_BOUNDS.maxZoom}
      maxBounds={maxBounds}
      maxBoundsViscosity={1.0}  // Fully restrict to bounds (no rubber-banding)
      className="h-[400px] w-full rounded-lg z-0"
      attributionControl={false}
      zoomControl={false}
      scrollWheelZoom={true}
    >
      <TileLayer
        attribution='<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
      />
      <AttributionControl position="bottomright" prefix={false} />
      
      <ZoomControl position="topright" />
      
      <MarkerClusterLayer
        regions={regions}
        selectedRegion={selectedRegion}
        onSelectRegion={onSelectRegion}
      />
    </MapContainer>
  );
}
