/**
 * The dashboard's Content Security Policy, built for each page response with a fresh nonce
 * (middleware.ts). Edge-safe: no Node APIs.
 *
 * What the browser enforces is script-src: only scripts carrying this response's nonce run, plus
 * the scripts they load themselves ('strict-dynamic': Next's chunks, the vendored spice-html5 the
 * console injects). A script an attacker gets into the page (a reflected string, a guest's text in
 * a terminal or file view that escapes encoding) carries no nonce and does not run. The other
 * directives that cannot break a working page are enforced too. A wider policy (styles, images,
 * fonts, connections, media) is report-only, so whatever tightening it further would break is
 * reported to /api/csp-report first.
 *
 * important: with 'strict-dynamic' a CSP3 browser ignores 'self' and host sources in script-src. A
 * parser-inserted <script> without the nonce never runs, so every inline or external script the
 * server renders must carry it (app/layout.tsx), and Next adds it to its own scripts by reading
 * the nonce from the request's Content-Security-Policy header.
 */

export const CSP_REPORT_PATH = "/api/csp-report";

/** "enforce" (the default) or "report-only": the switch back if an enforced script-src breaks a page. */
export type ScriptPolicyMode = "enforce" | "report-only";

export function scriptPolicyMode(value: string | undefined): ScriptPolicyMode {
  return value?.trim().toLowerCase() === "report-only" ? "report-only" : "enforce";
}

/** 128 random bits, base64: a new one for every response. */
export function newNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/**
 * Image origins of an operator's map tiles (MAP_TILE_URL, MAP_TILE_URL_DARK), for img-src. A
 * template's `{s}` subdomain becomes a wildcard; anything that does not parse as https is left out.
 */
export function tileImageSources(env: Record<string, string | undefined>): string[] {
  const sources = new Set<string>();
  for (const template of [env.MAP_TILE_URL, env.MAP_TILE_URL_DARK]) {
    const value = template?.trim();
    if (!value) continue;
    try {
      const url = new URL(value.replace(/\{s\}/g, "subdomain-wildcard").replace(/\{[a-z]+\}/gi, "0"));
      if (url.protocol !== "https:") continue;
      sources.add(`https://${url.host.replace(/^subdomain-wildcard\./, "*.")}`);
    } catch {
      // An unusable template is already refused where the basemap is resolved.
    }
  }
  return [...sources];
}

export interface PolicyOptions {
  nonce: string;
  /** `next dev` only: React rebuilds server error stacks with eval. */
  dev: boolean;
  mode: ScriptPolicyMode;
  imageSources?: string[];
}

/** The enforced and the report-only policy for one page response. */
export function contentSecurityPolicies({ nonce, dev, mode, imageSources = [] }: PolicyOptions): {
  enforced: string;
  reportOnly: string;
} {
  const script = `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`;
  // important: no default-src in the enforced policy. It would become style-src too and block the
  // inline styles Radix and Tailwind emit.
  const always = ["object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'self'"];
  const report = `report-uri ${CSP_REPORT_PATH}`;
  const enforced = mode === "enforce" ? [script, ...always, report] : always;
  const reportOnly = [
    "default-src 'self'",
    script,
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    ["img-src 'self' data: blob: https://*.cloudinary.com", ...imageSources].join(" "),
    "font-src 'self' data:",
    // The same-origin API, and the WebSockets of the console, terminal and event feeds.
    "connect-src 'self' ws: wss:",
    // SPICE plays guest audio and video from blob: URLs.
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    ...always,
    report,
  ];
  return { enforced: enforced.join("; "), reportOnly: reportOnly.join("; ") };
}
