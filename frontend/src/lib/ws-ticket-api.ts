/**
 * Single-use WebSocket tickets: POST /ws-tickets, through the app's authenticated fetch.
 *
 * important: a ticket opens one socket and lives 30 s, so mint one right before each connect,
 * including every reconnect, and never keep one for later.
 */

import { fetchAPI } from "@/lib/api";

export type WsTicketRequest =
  | { kind: "monitoring" }
  | { kind: "tasks" }
  | { kind: "events"; host: string }
  | { kind: "terminal"; host: string; project: string; instance: string }
  | {
      kind: "console";
      host: string;
      project: string;
      instance: string;
      console_type: "vga" | "console";
    };

export interface WsTicketResponse {
  ticket: string;
  /** A graphical console opens one socket per SPICE channel; one ticket each. */
  tickets: string[] | null;
  expires_in: number;
}

export function mintWsTicket(request: WsTicketRequest): Promise<WsTicketResponse> {
  return fetchAPI<WsTicketResponse>("/ws-tickets", {
    method: "POST",
    body: JSON.stringify(request),
    cache: "no-store",
  });
}
