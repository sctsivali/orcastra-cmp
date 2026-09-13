import type { IPAMAllocation, IPAMAllocationKind } from "@/types/api";

/**
 * The words the IPAM page uses for how an address came to be held.
 *
 * "Static" and "DHCP" are the pair the instances list already prints under an address, so a
 * reconciliation against an IPAM catalogue reads the same word on both pages. The rest name
 * the network's own rows. "Unknown" is what an unread instance list answers, never a guess.
 */
export const ALLOCATION_LABEL: Record<IPAMAllocationKind, string> = {
  static: "Static",
  dhcp: "DHCP",
  gateway: "Gateway",
  forward: "Forward",
  load_balancer: "Load balancer",
  unknown: "Unknown",
};

/** The order the filter lists them in: the two an operator reconciles first. */
export const ALLOCATION_ORDER: readonly string[] = [
  "static",
  "dhcp",
  "gateway",
  "forward",
  "load_balancer",
  "unknown",
];

/** Tolerates a body cached by a build that had no allocation field. */
export function allocationLabel(kind: string | undefined): string {
  return ALLOCATION_LABEL[(kind ?? "unknown") as IPAMAllocationKind] ?? ALLOCATION_LABEL.unknown;
}

export const UNKNOWN_ALLOCATION_REASON =
  "The instances of this node could not be read, so whether this address is pinned is not known.";

/** Which subnet to print first, and which underneath. */
export function subnetText(row: IPAMAllocation): { primary: string; secondary?: string } {
  const v4 = row.subnet_ipv4 || "";
  const v6 = row.subnet_ipv6 || "";
  if (v4) return { primary: v4, secondary: v6 || undefined };
  return { primary: v6 };
}
