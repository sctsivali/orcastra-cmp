/**
 * Basemap resolution for the regions map.
 *
 * The map ships with a vendored landmass (public/geo) and draws it locally, so a fresh
 * self-hosted install renders a working map with no outbound request, no API key and no
 * third-party terms attached. Operators who want street-level detail point MAP_TILE_URL at
 * a provider they control; that path is opt-in and validated rather than assumed.
 *
 * The values are read from the server process at request time (see app/regions/page.tsx),
 * deliberately NOT via NEXT_PUBLIC_*. Those are baked into the bundle at build time and
 * swapped on container start by `sed -i "s|placeholder|value|g"` in entrypoint.sh, which
 * corrupts the bundle for any value containing & or |, which is exactly what a tile URL
 * carrying query parameters looks like.
 */

export type OfflineBasemapConfig = {
  mode: "offline";
};

export type TileBasemapConfig = {
  mode: "tiles";
  url: string;
  darkUrl: string | null;
  attribution: string;
  subdomains: string | null;
  maxZoom: number;
};

export type BasemapConfig = OfflineBasemapConfig | TileBasemapConfig;

export const OFFLINE_BASEMAP: OfflineBasemapConfig = { mode: "offline" };

/** Versioned so the immutable cache header in next.config.js is safe to apply. */
export const OFFLINE_BASEMAP_URL = "/geo/world-land-50m.v1.json";

export const OFFLINE_BASEMAP_ATTRIBUTION =
  '<a href="https://www.naturalearthdata.com/" target="_blank" rel="noreferrer noopener">Natural Earth</a>';

/**
 * The vendored outline carries no streets or labels, so detail stops being added past
 * roughly this level and zooming further only spreads the same coastline over more pixels.
 */
export const OFFLINE_MAX_ZOOM = 7;

export const TILE_DEFAULT_MAX_ZOOM = 18;
export const TILE_DEFAULT_SUBDOMAINS = "abc";

/** Leaflet renders attribution as HTML, so every interpolated value is escaped first. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function readTrimmed(value: string | undefined): string | null {
  const trimmed = (value ?? "").trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * A usable XYZ template: an absolute http(s) URL carrying all three tile placeholders.
 * Rejecting here rather than at render time is what keeps a typo in .env from producing a
 * map of broken image icons with no explanation.
 */
function isValidTileTemplate(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  return url.includes("{z}") && url.includes("{x}") && url.includes("{y}");
}

function resolveMaxZoom(raw: string | null): number {
  if (raw === null) return TILE_DEFAULT_MAX_ZOOM;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return TILE_DEFAULT_MAX_ZOOM;
  return Math.min(22, Math.max(1, parsed));
}

/**
 * Builds the attribution markup from plain-text configuration.
 *
 * The text is escaped and the optional link is validated rather than passing operator
 * input through as raw HTML. An operator who can set environment variables is already
 * trusted, but config that reaches innerHTML untouched is the kind of shortcut that
 * outlives the person who understood why it was safe.
 */
function buildAttribution(text: string | null, href: string | null): string {
  if (text === null) return "";
  const label = escapeHtml(text);
  if (href === null) return label;

  let parsed: URL;
  try {
    parsed = new URL(href);
  } catch {
    return label;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return label;

  return `<a href="${escapeHtml(parsed.toString())}" target="_blank" rel="noreferrer noopener">${label}</a>`;
}

export function resolveBasemapConfig(env: Record<string, string | undefined>): BasemapConfig {
  const url = readTrimmed(env.MAP_TILE_URL);
  if (url === null) return OFFLINE_BASEMAP;

  if (!isValidTileTemplate(url)) {
    console.warn(
      "[basemap] MAP_TILE_URL is not a usable XYZ tile template " +
        "(needs an http(s) URL containing {z}, {x} and {y}); falling back to the built-in basemap"
    );
    return OFFLINE_BASEMAP;
  }

  const darkCandidate = readTrimmed(env.MAP_TILE_URL_DARK);
  let darkUrl: string | null = null;
  if (darkCandidate !== null) {
    if (isValidTileTemplate(darkCandidate)) {
      darkUrl = darkCandidate;
    } else {
      console.warn(
        "[basemap] MAP_TILE_URL_DARK is not a usable XYZ tile template; " +
          "using MAP_TILE_URL in both themes"
      );
    }
  }

  const subdomains = readTrimmed(env.MAP_TILE_SUBDOMAINS);

  return {
    mode: "tiles",
    url,
    darkUrl,
    attribution: buildAttribution(
      readTrimmed(env.MAP_TILE_ATTRIBUTION),
      readTrimmed(env.MAP_TILE_ATTRIBUTION_URL)
    ),
    // {s} without subdomains makes Leaflet request the literal host "{s}.example.com".
    subdomains: url.includes("{s}") ? (subdomains ?? TILE_DEFAULT_SUBDOMAINS) : subdomains,
    maxZoom: resolveMaxZoom(readTrimmed(env.MAP_TILE_MAX_ZOOM)),
  };
}
