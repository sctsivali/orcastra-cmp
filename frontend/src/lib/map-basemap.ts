/**
 * Basemap resolution for the regions map.
 *
 * The map draws a vendored landmass (public/geo) locally, so a fresh self-hosted install
 * renders with no outbound request, API key or third-party terms. For street-level detail
 * an operator points MAP_TILE_URL at a provider they control; that is opt-in and validated.
 *
 * The values are read from the server process at request time (see app/regions/page.tsx),
 * not via NEXT_PUBLIC_*. Those are baked into the bundle at build time and only replaced on
 * container start for the placeholders entrypoint.sh knows about.
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

const geoAssets = new Map<string, Promise<unknown>>();

/**
 * Fetches a bundled geometry file from public/geo once per page load.
 *
 * Cached at module scope, so client-side navigation does not refetch and two mounted maps
 * share one request. A failure clears the entry, so a later mount retries.
 */
export function loadGeoAsset<T>(url: string, parse: (data: unknown) => T): Promise<T> {
  let request = geoAssets.get(url) as Promise<T> | undefined;
  if (request === undefined) {
    request = fetch(url)
      .then((response) => {
        if (!response.ok) throw new Error(`${url} request failed with HTTP ${response.status}`);
        return response.json() as Promise<unknown>;
      })
      .then(parse)
      .catch((error: unknown) => {
        geoAssets.delete(url);
        throw error;
      });
    geoAssets.set(url, request);
  }
  return request;
}

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
 * Rejecting here, with a warning, keeps a typo in .env from rendering a map of broken
 * images.
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
 * The text is escaped and the optional link must be http(s), so operator configuration
 * never reaches Leaflet's innerHTML as raw HTML.
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
