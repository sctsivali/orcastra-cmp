/**
 * WebSocket URLs: one table for every socket this app opens.
 *
 * important: a socket proves who opened it with a single-use ticket minted right before it
 * connects (lib/ws-ticket-api.ts), never with the access token. A browser cannot set headers on
 * an upgrade, and a URL is what proxies, access logs and crash reports keep.
 *
 * NEXT_PUBLIC_API_URL gives the backend host, so sockets reach the same reverse proxy or tunnel
 * as HTTP calls; the Next proxy does not carry upgrades.
 */

import { mintWsTicket, type WsTicketRequest } from "@/lib/ws-ticket-api";
import { APIError } from "@/types/api";

export type { WsTicketRequest } from "@/lib/ws-ticket-api";

/** Close codes the backend sends beside the standard ones. */
export const WS_CLOSE = {
  /** Refused at connect: a spent, expired or foreign ticket, or no access. */
  POLICY_VIOLATION: 1008,
  /** The session the ticket was minted under ended. A feed mints a new one and reconnects. */
  SESSION_ENDED: 4401,
  /** A shell or console lost its access while open. It must not reconnect on its own. */
  ACCESS_REVOKED: 4403,
  /** The shell, console or display ended on the instance side (exit, power off, taken over). */
  NORMAL: 1000,
  /** The connection to the instance broke; reconnecting may work. */
  UPSTREAM_FAILED: 1011,
  /** The session cannot start (not running, not found, no agent); reconnecting will not help. */
  UNAVAILABLE: 4404,
} as const;

export const ACCESS_REVOKED_MESSAGE = "Access to this instance was revoked.";

/**
 * Derive WebSocket protocol + host from NEXT_PUBLIC_API_URL.
 *
 *   "https://api.orcastra.io"   -> { protocol: "wss:", host: "api.orcastra.io" }
 *   "http://192.168.1.5:4050"   -> { protocol: "ws:",  host: "192.168.1.5:4050" }
 *   (unset / "")                -> { protocol: "ws:",  host: "<page host>:4050" }
 */
function getWsBase(): { protocol: string; host: string } {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
  if (apiUrl) {
    const protocol = apiUrl.startsWith("https://") ? "wss:" : "ws:";
    const host = apiUrl.replace(/^https?:\/\//, "").replace(/\/+$/, "");
    return { protocol, host };
  }
  const isHttps = typeof window !== "undefined" && window.location.protocol === "https:";
  const hostname = typeof window !== "undefined" ? window.location.hostname : "localhost";
  return { protocol: isHttps ? "wss:" : "ws:", host: `${hostname}:4050` };
}

function socketPath(request: WsTicketRequest): string {
  const enc = encodeURIComponent;
  switch (request.kind) {
    case "monitoring":
      return "/api/v1/monitoring/ws";
    case "tasks":
      return "/api/v1/tasks/ws";
    case "events":
      return `/api/v1/events/ws?host=${enc(request.host)}`;
    case "terminal":
      return (
        `/api/v1/terminal/ws/${enc(request.instance)}` +
        `?host=${enc(request.host)}&project=${enc(request.project)}`
      );
    case "console":
      return (
        `/api/v1/console/${enc(request.instance)}/ws` +
        `?host=${enc(request.host)}&project=${enc(request.project)}&console_type=${request.console_type}`
      );
  }
}

function withTicket(request: WsTicketRequest, ticket: string): string {
  const { protocol, host } = getWsBase();
  const path = socketPath(request);
  return `${protocol}//${host}${path}${path.includes("?") ? "&" : "?"}ticket=${encodeURIComponent(ticket)}`;
}

/** A socket URL carrying a freshly minted ticket. A failed mint rejects with the APIError. */
export async function ticketedSocketUrl(request: WsTicketRequest): Promise<string> {
  const { ticket } = await mintWsTicket(request);
  return withTicket(request, ticket);
}

/** A SPICE session's URLs: the main channel's, then one per further channel (display, inputs, cursor). */
export async function spiceSocketUrls(
  instance: string,
  host: string,
  project: string,
): Promise<{ main: string; channels: string[] }> {
  const request: WsTicketRequest = { kind: "console", host, project, instance, console_type: "vga" };
  const { ticket, tickets } = await mintWsTicket(request);
  const all = tickets && tickets.length > 0 ? tickets : [ticket];
  return { main: withTicket(request, all[0]), channels: all.slice(1).map((t) => withTicket(request, t)) };
}

/** What to tell the operator when a ticket could not be minted (the break-glass sentence included). */
export function mintFailureMessage(error: unknown): string {
  if (error instanceof APIError && error.message) return error.message;
  return "Could not open a live connection. Try again.";
}
