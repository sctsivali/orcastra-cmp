/**
 * WebSocket URL Utilities
 *
 * Centralised helpers for building WebSocket URLs that work in both:
 *  - Local development  (http://localhost:4050)
 *  - Production / public URL / tunnelling  (https://api.orcastra.io)
 *
 * Uses NEXT_PUBLIC_API_URL to derive the backend host so connections
 * go through the same reverse proxy / tunnel as regular HTTP API calls.
 */

import { getAccessToken } from "@/lib/api";

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Derive WebSocket protocol + host from NEXT_PUBLIC_API_URL.
 *
 * Examples:
 *   "https://api.orcastra.io"   → { protocol: "wss:", host: "api.orcastra.io" }
 *   "http://192.168.1.5:4050"   → { protocol: "ws:",  host: "192.168.1.5:4050" }
 *   (unset / "")                → { protocol: "ws:",  host: "localhost:4050" }
 */
function getWsBase(): { protocol: string; host: string } {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

  if (apiUrl) {
    const isSecure = apiUrl.startsWith("https://");
    const protocol = isSecure ? "wss:" : "ws:";
    // strip scheme and trailing slashes
    const host = apiUrl.replace(/^https?:\/\//, "").replace(/\/+$/, "");
    return { protocol, host };
  }

  // Fallback for local dev without NEXT_PUBLIC_API_URL
  const isHttps =
    typeof window !== "undefined" && window.location.protocol === "https:";
  const protocol = isHttps ? "wss:" : "ws:";
  const hostname =
    typeof window !== "undefined" ? window.location.hostname : "localhost";
  return { protocol, host: `${hostname}:4050` };
}

/** Return `&token=...` query fragment (or empty string). */
function getTokenParam(): string {
  try {
    const token = getAccessToken();
    return token ? `&token=${encodeURIComponent(token)}` : "";
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Build WebSocket URL for **terminal** (text console / exec).
 *
 * @param instanceName  LXD instance name
 * @param host          Target host node
 * @param project       LXD project (default: "default")
 * @param includeToken  Append Bearer token as query param (default: true)
 */
export function getTerminalWsUrl(
  instanceName: string,
  host: string,
  project: string,
  includeToken = true,
): string {
  const { protocol, host: wsHost } = getWsBase();
  const tokenParam = includeToken ? getTokenParam() : "";
  const url =
    `${protocol}//${wsHost}/api/v1/terminal/ws/${encodeURIComponent(instanceName)}` +
    `?host=${encodeURIComponent(host)}` +
    `&project=${encodeURIComponent(project)}` +
    tokenParam;
  return url;
}

/**
 * Build WebSocket URL for **graphics console** (SPICE).
 *
 * @param instanceName  LXD instance name
 * @param host          Target host node
 * @param project       LXD project (default: "default")
 * @param includeToken  Append Bearer token as query param (default: true)
 */
export function getGraphicsWsUrl(
  instanceName: string,
  host: string,
  project: string,
  includeToken = true,
): string {
  const { protocol, host: wsHost } = getWsBase();
  const tokenParam = includeToken ? getTokenParam() : "";
  const url =
    `${protocol}//${wsHost}/api/v1/console/${encodeURIComponent(instanceName)}/ws` +
    `?host=${encodeURIComponent(host)}` +
    `&project=${encodeURIComponent(project)}` +
    `&console_type=vga` +
    tokenParam;
  return url;
}
