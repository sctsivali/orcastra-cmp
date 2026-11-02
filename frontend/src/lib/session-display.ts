/**
 * How a console session's protocol is spelled. Sibling of `integration-display.ts`: the Sessions
 * tables, their filter menus, the session detail and the console's resource bar all name the same
 * session, so they read one word from here rather than each inventing theirs.
 */

import { formatLabel } from "@/lib/utils";

/** The stored protocol of a VM's display session. A value in the audit trail, never shown. */
export const GRAPHICS_SESSION_PROTOCOL = "spice-graphics";

// important: a lookup rather than `isGraphics ? <graphics> : "Terminal"`, which was written when
// there were exactly two protocols and quietly labelled anything else as a human terminal
// session. An agent driving a desktop through the MCP surface is neither, and reading it as a
// terminal is worse than reading it as unknown: it attributes machine activity to a person.
//
// note: the display is named for what the operator gets, not for the wire protocol under it. The
// stored value keeps its old spelling so existing audit rows still map without a migration.
const SESSION_PROTOCOL_LABELS: Record<string, string> = {
  [GRAPHICS_SESSION_PROTOCOL]: "Graphical Console",
  "websocket-terminal": "Terminal",
  "mcp-desktop": "Agent (desktop)",
};

export function sessionProtocolLabel(protocol: string | null | undefined): string {
  return SESSION_PROTOCOL_LABELS[protocol ?? ""] ?? formatLabel(protocol ?? "unknown");
}
