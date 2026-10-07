import L from "leaflet";
import { formatBytes, formatMemory } from "@/lib/utils";
import { escapeHtml } from "@/lib/map-basemap";
import { RegionSummary } from "@/types/api";

/**
 * Presentation helpers for the regions map: marker and cluster icons, popup markup, and
 * the one piece of plumbing a canvas layer needs that a DOM layer does not.
 *
 * Everything here draws from the theme tokens in globals.css rather than fixed hex, so the
 * map follows the product palette and the user's personalization accent in both themes.
 */

/**
 * Resolves a theme token to a concrete colour.
 *
 * Inline styles on a DivIcon can carry `hsl(var(--token))` directly, because the element
 * lands inside the themed tree and the browser resolves it. A Canvas layer cannot: Leaflet
 * hands the string to `ctx.fillStyle`, which knows nothing about custom properties and
 * silently paints nothing. So canvas colours have to be read out first.
 */
export function themeColor(token: string, alpha?: number): string {
  if (typeof window === "undefined") return "transparent";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  if (raw === "") return "transparent";
  return alpha === undefined ? `hsl(${raw})` : `hsl(${raw} / ${alpha})`;
}

function createStatusIcon(color: string): L.DivIcon {
  return L.divIcon({
    className: "custom-marker",
    // note: the border is --background rather than white so the pin reads as a cut-out of
    // the map in both themes, the same trick the status chips use.
    html: `
      <div style="
        background-color: ${color};
        width: 24px;
        height: 24px;
        border-radius: 50%;
        border: 3px solid hsl(var(--background));
        box-shadow: 0 2px 5px hsl(var(--foreground) / 0.25);
        display: flex;
        align-items: center;
        justify-content: center;
      ">
        <div style="
          width: 8px;
          height: 8px;
          background-color: hsl(var(--background));
          border-radius: 50%;
        "></div>
      </div>
    `,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    popupAnchor: [0, -12],
  });
}

export function getRegionIcon(region: RegionSummary): L.DivIcon {
  const onlineCount = region.nodes.filter((n) => n.status === "online").length;
  const totalCount = region.nodes.length;

  // note: the markers are injected as raw HTML into a Leaflet DivIcon, so they take a CSS
  // value rather than a Tailwind class. Reading the same --status-* custom properties is what
  // keeps the map on the product's status palette in both themes instead of three hex
  // literals that drift the first time a token is retuned.
  if (totalCount > 0 && onlineCount === totalCount) {
    return createStatusIcon("hsl(var(--status-online))");
  }
  if (onlineCount > 0) {
    return createStatusIcon("hsl(var(--status-pending))");
  }
  return createStatusIcon("hsl(var(--status-offline))");
}

/**
 * Cluster bubbles carry their count twice already, as a number and as a diameter, so a
 * third encoding in hue bought nothing and cost three hardcoded purples that ignored both
 * the theme and the personalization accent.
 */
export function createClusterIcon(cluster: L.MarkerCluster): L.DivIcon {
  const count = cluster.getChildCount();
  const dimension = count >= 10 ? 50 : count >= 5 ? 40 : 30;
  const fontSize = count >= 10 ? "16px" : count >= 5 ? "14px" : "12px";

  return L.divIcon({
    html: `
      <div style="
        background-color: hsl(var(--primary));
        width: ${dimension}px;
        height: ${dimension}px;
        border-radius: 50%;
        border: 3px solid hsl(var(--background));
        box-shadow: 0 2px 8px hsl(var(--foreground) / 0.25);
        display: flex;
        align-items: center;
        justify-content: center;
        color: hsl(var(--primary-foreground));
        font-weight: 600;
        font-size: ${fontSize};
      ">
        ${count}
      </div>
    `,
    className: "custom-cluster-icon",
    iconSize: L.point(dimension, dimension),
  });
}

/**
 * Popup markup for one region.
 *
 * important: every interpolated value is escaped. Region, city and facility strings come
 * from cluster metadata written through PATCH /clusters/{id}/location, which the backend
 * stores as unconstrained text, and Leaflet renders a popup string as HTML. Interpolating
 * them raw let anyone able to register or relocate a cluster store markup that executes in
 * the browser of every operator who later opened this page.
 */
export function createPopupContent(region: RegionSummary): string {
  const cardsTotal = region.hardware.gpu_cards_total ?? region.hardware.gpu_total;

  const row = (label: string, value: string | number) =>
    `<div style="display: flex; justify-content: space-between; gap: 12px;">
         <span>${escapeHtml(label)}:</span>
         <span style="font-weight: 500;">${escapeHtml(String(value))}</span>
       </div>`;

  const gpuSection =
    region.hardware.gpu_total > 0
      ? row("Compute GPUs", region.hardware.gpu_total) +
        (cardsTotal > region.hardware.gpu_total ? row("Cards detected", cardsTotal) : "")
      : "";

  const facilityLabel = region.facility_provider
    ? `${region.facility_name} (${region.facility_provider})`
    : region.facility_name;

  const facilitySection = region.facility_name
    ? `<p style="font-size: 12px; color: hsl(var(--muted-foreground)); margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
        <span style="display: inline-block; width: 8px; height: 8px; background: hsl(var(--primary)); border-radius: 2px; flex-shrink: 0;"></span>
        ${escapeHtml(facilityLabel ?? "")}
      </p>`
    : "";

  return `
    <div style="min-width: 200px; padding: 4px;">
      <h3 style="font-weight: 600; font-size: 14px; margin-bottom: 4px; color: hsl(var(--foreground));">
        ${escapeHtml(region.region_name)}
      </h3>
      <p style="font-size: 12px; color: hsl(var(--muted-foreground)); margin-bottom: 4px;">
        ${escapeHtml(region.city)}, ${escapeHtml(region.province ?? region.country_name)}
      </p>
      ${facilitySection}

      <div style="font-size: 12px; display: flex; flex-direction: column; gap: 4px; margin-top: 8px; color: hsl(var(--foreground));">
        ${row("Clusters", region.node_count)}
        ${row("Instances", region.instance_count)}
        ${row("Hardware threads", region.hardware.cpu_total)}
        ${row("Memory", formatMemory(region.hardware.memory_total))}
        ${gpuSection}
      </div>
    </div>
  `;
}
