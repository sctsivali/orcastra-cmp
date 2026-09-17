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

// LXD takes a port only on a tcp or udp rule; icmp rules and rules with no protocol refuse it.
export const PORT_PROTOCOLS = ["tcp", "udp"] as const;
const PORT_LIST_RE = /^\d{1,5}(-\d{1,5})?(\s*,\s*\d{1,5}(-\d{1,5})?)*$/;

export function portsAllowed(rule: AclRule): boolean {
  return (PORT_PROTOCOLS as readonly string[]).includes(rule.protocol ?? "");
}

/** Why a port list is not one LXD would take (same rule as the backend), or null when it is. */
export function portListError(value: string | undefined): string | null {
  const text = (value ?? "").trim();
  if (!text) return null;
  if (!PORT_LIST_RE.test(text)) return "Ports are numbers, comma lists or ranges like 8000-8100.";
  for (const item of text.split(",")) {
    const bounds = item.trim().split("-").map(Number);
    if (bounds.some((p) => p < 1 || p > 65535)) return "Port must be between 1 and 65535.";
    if (bounds.length === 2 && bounds[0] >= bounds[1]) return "A range must run low to high.";
  }
  return null;
}

/** Every port problem in a rule, keyed by field; empty when the rule is fine. */
export function rulePortErrors(rule: AclRule): Partial<Record<"source_port" | "destination_port", string>> {
  const out: Partial<Record<"source_port" | "destination_port", string>> = {};
  for (const field of ["source_port", "destination_port"] as const) {
    const problem = portListError(rule[field]);
    if (problem) out[field] = problem;
  }
  return out;
}
