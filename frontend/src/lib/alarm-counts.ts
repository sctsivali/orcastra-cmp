import { severityBucket, warningNodeValue } from "@/lib/warning-facets";
import type { CertificateWarning, SystemWarningsResponse, WarningInfo } from "@/types/api";

/** What each alarm tile counts. `total` is every tile added up, and is what View All shows. */
export interface AlarmCounts {
  critical: number;
  high: number;
  warning: number;
  info: number;
  certIssues: number;
  offlineClusters: number;
  kernels: number;
  total: number;
}

// note: an offline-cluster row has carried node_id on some responses and only cluster_id on
// others; the type names the second.
function offlineClusterId(warning: CertificateWarning): string {
  return (warning as CertificateWarning & { node_id?: string }).node_id || warning.cluster_id || "";
}

/**
 * The alarm tiles' numbers, for every cluster in scope or for one.
 *
 * important: the same node accessor and severity buckets the warnings page filters by, so a
 * tile's number is the count the list it opens shows. The dashboard and Monitoring each counted
 * for themselves before, and Monitoring's View All left the kernel findings out while the
 * dashboard's counted them, so one fleet had two totals.
 */
export function alarmCounts(
  warnings: readonly WarningInfo[],
  system: SystemWarningsResponse | null | undefined,
  /** A registered cluster id; undefined counts every cluster. */
  cluster?: string,
): AlarmCounts {
  const counts: AlarmCounts = {
    critical: 0, high: 0, warning: 0, info: 0, certIssues: 0, offlineClusters: 0, kernels: 0, total: 0,
  };
  const mine = <T,>(rows: readonly T[] | undefined, id: (row: T) => string) =>
    cluster === undefined ? rows ?? [] : (rows ?? []).filter((row) => id(row) === cluster);

  for (const warning of mine(warnings, warningNodeValue)) {
    counts.total += 1;
    const bucket = severityBucket(warning.severity);
    if (bucket) counts[bucket] += 1;
  }
  counts.certIssues = mine(system?.certificate_warnings, (row) => row.cluster_id || "").length;
  counts.offlineClusters = mine(system?.node_warnings, offlineClusterId).length;
  // note: its own tile, not folded into Critical or Warning. Those tiles open the platform tab,
  // which has no kernel rows, and a tile's number must be the count its list shows.
  counts.kernels = mine(system?.kernel_advisories, (row) => row.cluster_id).length;
  counts.total += counts.certIssues + counts.offlineClusters + counts.kernels;
  return counts;
}

/** The rows of a GET /warnings body, which the client reads untyped. */
export function platformWarnings(body: unknown): WarningInfo[] {
  const rows = (body as { warnings?: unknown } | null | undefined)?.warnings;
  return Array.isArray(rows) ? (rows as WarningInfo[]) : [];
}

/** The offline-cluster row for one cluster, whose own value the warnings page's nodes tab facets on. */
export function offlineClusterWarning(
  system: SystemWarningsResponse | null | undefined,
  cluster: string,
): CertificateWarning | undefined {
  return system?.node_warnings?.find((row) => offlineClusterId(row) === cluster);
}
