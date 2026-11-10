import { EMPTY_FACET } from "@/lib/filter-url";
import type { CertificateWarning, WarningInfo } from "@/types/api";

// The one warnings vocabulary. The dashboard tiles, the monitoring tiles, the warnings page
// facets and the links between them all read these, so a tile's number and the list the tile
// opens are counted over the same set.
//
// important: LXD's own severities are low, moderate and high (lxd/db/warningtype). The
// dashboard's certificate and node checks add critical, high, warning, low and info. A bucket
// must list every raw value it counts, or a tile can show a count its list does not match.

export const WARNING_SEVERITY_BUCKETS = {
  critical: ["critical", "error"],
  high: ["high"],
  warning: ["warning", "medium", "moderate"],
  info: ["low", "info", EMPTY_FACET],
} as const;

export type SeverityBucket = keyof typeof WARNING_SEVERITY_BUCKETS;

/** The facet value a warning carries: the raw severity, lowercased. Empty is the empty bucket. */
export function warningSeverityValue(raw: string | null | undefined): string {
  return String(raw || "").toLowerCase();
}

/** Which tile a warning counts under, or null for a severity the tiles do not know. */
export function severityBucket(raw: string | null | undefined): SeverityBucket | null {
  const value = warningSeverityValue(raw) || EMPTY_FACET;
  for (const bucket of Object.keys(WARNING_SEVERITY_BUCKETS) as SeverityBucket[]) {
    if ((WARNING_SEVERITY_BUCKETS[bucket] as readonly string[]).includes(value)) return bucket;
  }
  return null;
}

/** Sort order of the platform severities, the tile order first. */
export const WARNING_SEVERITY_ORDER: readonly string[] = [
  "critical", "error", "high", "warning", "medium", "moderate", "low", "info",
];

/**
 * What the platform tab's Node facet compares: the registered node, never the LXD member.
 *
 * important: `location` is the cluster member that raised the warning, a name nothing else in
 * the console links by. Every other surface identifies a node by its registered id, so a link
 * into the warnings page carries this value.
 */
export function warningNodeValue(warning: Pick<WarningInfo, "source_node">): string {
  return warning.source_node || "";
}

/** What the certificates and offline-nodes tabs' Cluster facet compares. */
export function warningClusterValue(
  warning: Pick<CertificateWarning, "display_name" | "cluster_id">,
): string {
  return warning.display_name || warning.cluster_id || "";
}
