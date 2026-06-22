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

import { wsTicketAPI } from "@/lib/api";

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

// ---------------------------------------------------------------------------
// Public API
//
// Each builder is async: it first exchanges the caller's bearer token for a
// single-use, short-lived ticket (POST via the authenticated API layer), then
// puts only the opaque ?ticket= in the WebSocket URL. The JWT never appears in
// the URL (devtools / history / proxy logs), and because a fresh ticket is
// minted per (re)connect the call also refreshes an expired session token.
// ---------------------------------------------------------------------------

/**
 * Build WebSocket URL for **terminal** (text console / exec).
 */
export async function getTerminalWsUrl(
  instanceName: string,
  host: string,
  project: string,
): Promise<string> {
  const { protocol, host: wsHost } = getWsBase();
  const { ticket } = await wsTicketAPI.createInstanceTicket({ instance: instanceName, host, project });
  return (
    `${protocol}//${wsHost}/api/v1/terminal/ws/${encodeURIComponent(instanceName)}` +
    `?host=${encodeURIComponent(host)}` +
    `&project=${encodeURIComponent(project)}` +
    `&ticket=${encodeURIComponent(ticket)}`
  );
}

/**
 * Build WebSocket URL for **graphics console** (SPICE).
 */
export async function getGraphicsWsUrl(
  instanceName: string,
  host: string,
  project: string,
): Promise<string> {
  const { protocol, host: wsHost } = getWsBase();
  const { ticket } = await wsTicketAPI.createInstanceTicket({
    instance: instanceName,
    host,
    project,
    console_type: "vga",
  });
  return (
    `${protocol}//${wsHost}/api/v1/console/${encodeURIComponent(instanceName)}/ws` +
    `?host=${encodeURIComponent(host)}` +
    `&project=${encodeURIComponent(project)}` +
    `&console_type=vga` +
    `&ticket=${encodeURIComponent(ticket)}`
  );
}

/**
 * Build WebSocket URL for the **monitoring** real-time push channel (admin-only).
 */
export async function getMonitoringWsUrl(): Promise<string> {
  const { protocol, host: wsHost } = getWsBase();
  const { ticket } = await wsTicketAPI.createMonitoringTicket();
  return `${protocol}//${wsHost}/api/v1/monitoring/ws?ticket=${encodeURIComponent(ticket)}`;
}
