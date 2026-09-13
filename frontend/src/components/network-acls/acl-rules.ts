/**
 * The vocabulary and the shape of a network ACL rule, shared by the dialog and its rule row.
 *
 * LXD validates rule bodies itself and its errors are specific, so this holds only what the
 * operator can fix without a round trip: the closed vocabularies, the name rule, and the
 * cleaning that keeps an untouched field absent rather than "".
 */

// LXD's own vocabulary for a rule. Anything outside these it rejects.
export const ACTIONS = ["allow", "drop", "reject"] as const;
export const PROTOCOLS = ["", "tcp", "udp", "icmp4", "icmp6"] as const;
export const STATES = ["enabled", "disabled", "logged"] as const;

// Same rule LXD applies: start with a letter, letters, numbers and hyphens after it.
export const NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]*$/;

export interface AclRule {
  action: string;
  protocol?: string;
  source?: string;
  destination?: string;
  source_port?: string;
  destination_port?: string;
  description?: string;
  state?: string;
}

export type Direction = "ingress" | "egress";

export function emptyRule(): AclRule {
  return { action: "allow", state: "enabled" };
}

/** Strip the keys LXD has no use for, so an untouched field is absent rather than "". */
export function cleanRule(rule: AclRule): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(rule)) {
    const trimmed = (value ?? "").trim();
    if (trimmed) out[key] = trimmed;
  }
  return out;
}
