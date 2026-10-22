import { ShieldAlert, ShieldCheck, ShieldQuestion, type LucideIcon } from "lucide-react";
import type {
  HostKernelRow,
  KernelAdvisoryInfo,
  KernelAdvisoryResponse,
  KernelAdvisoryScore,
  KernelAdvisoryState,
} from "@/types/kernel-advisory";

// note: the wire values stay exposed/unverified/patched (backend, URL facets, the warning feed).
// Only what an operator reads is CVE vocabulary. The icon and its token mirror the shape of
// lib/cluster-status.ts ClusterStatusMeta, so the state never rides on colour alone.
export const KERNEL_STATE_DISPLAY: Record<
  KernelAdvisoryState,
  { label: string; tone: "negative" | "caution" | "positive"; icon: LucideIcon; iconClassName: string }
> = {
  exposed: { label: "Affected", tone: "negative", icon: ShieldAlert, iconClassName: "text-status-offline" },
  unverified: { label: "Unconfirmed", tone: "caution", icon: ShieldQuestion, iconClassName: "text-status-pending" },
  patched: { label: "Patched", tone: "positive", icon: ShieldCheck, iconClassName: "text-status-online" },
};

const STATE_RANK: Record<KernelAdvisoryState, number> = { exposed: 0, unverified: 1, patched: 2 };

/** The worst state across a member's findings; a member with none is unverified. */
export function rowState(row: HostKernelRow): KernelAdvisoryState {
  return worstOf(row.findings.map((f) => f.state)) ?? "unverified";
}

export function worstOf(states: readonly KernelAdvisoryState[]): KernelAdvisoryState | null {
  if (states.length === 0) return null;
  return states.reduce((worst, s) => (STATE_RANK[s] < STATE_RANK[worst] ? s : worst));
}

export function hostsForCluster(
  data: KernelAdvisoryResponse | undefined,
  clusterId: string,
): HostKernelRow[] {
  return (data?.hosts ?? []).filter((h) => h.cluster_id === clusterId);
}

/** "Ubuntu 22.04", "Ubuntu", or null when the member reported neither. */
export function osLabel(row: { os_name: string | null; os_version: string | null }): string | null {
  if (!row.os_name) return null;
  return row.os_version ? `${row.os_name} ${row.os_version}` : row.os_name;
}

export type OpenAdvisoryState = Exclude<KernelAdvisoryState, "patched">;

/** One CVE that is not patched on every member of a cluster. */
export interface OpenAdvisory {
  id: string;
  /** null when a finding names a CVE the response carries no record for. */
  advisory: KernelAdvisoryInfo | null;
  state: OpenAdvisoryState;
  affected: number;
  total: number;
  /** Some member list could not be read, so `total` is the members read, not the cluster's size. */
  unlisted: boolean;
}

function topScore(advisory: KernelAdvisoryInfo | null): number {
  return Math.max(-1, ...(advisory?.scores ?? []).map((s) => s.score ?? -1));
}

/**
 * A cluster's kernel CVE verdict: the worst state and every CVE still open on any member.
 *
 * A member with no finding for a CVE counts as unconfirmed for it, the same rule rowState applies
 * to a member with no findings at all. A cluster with no member read, or no CVE judged, has no
 * verdict (state null): calling that patched would turn "not read" into an all-clear.
 */
export function clusterVerdict(
  rows: readonly HostKernelRow[],
  advisories: readonly KernelAdvisoryInfo[],
): { state: KernelAdvisoryState | null; open: OpenAdvisory[] } {
  const ids = Array.from(
    new Set([...advisories.map((a) => a.id), ...rows.flatMap((r) => r.findings.map((f) => f.advisory_id))]),
  );
  if (rows.length === 0 || ids.length === 0) return { state: null, open: [] };

  const unlisted = rows.some((r) => r.clustered && !r.members_listed);
  const open: OpenAdvisory[] = [];
  for (const id of ids) {
    const states = rows.map((r) => r.findings.find((f) => f.advisory_id === id)?.state ?? "unverified");
    const notPatched = states.filter((s): s is OpenAdvisoryState => s !== "patched");
    if (notPatched.length === 0) continue;
    open.push({
      id,
      advisory: advisories.find((a) => a.id === id) ?? null,
      state: worstOf(notPatched) as OpenAdvisoryState,
      affected: notPatched.length,
      total: rows.length,
      unlisted,
    });
  }
  open.sort(
    (a, b) =>
      STATE_RANK[a.state] - STATE_RANK[b.state] ||
      b.affected - a.affected ||
      topScore(b.advisory) - topScore(a.advisory) ||
      a.id.localeCompare(b.id),
  );
  return { state: open[0]?.state ?? "patched", open };
}

/** "CVE-2026-99999 (PwnEscape)", or the bare id when the CVE has no nickname or no record. */
export function advisoryTitle(id: string, name: string | null | undefined): string {
  return name ? `${id} (${name})` : id;
}

/** "Linux CNA: 9.3 Critical", "Ubuntu: Medium". */
export function scoreText(score: KernelAdvisoryScore): string {
  const parts = [score.score !== null ? score.score.toFixed(1) : null, score.rating].filter(Boolean);
  return `${score.source}: ${parts.join(" ")}`;
}

/** "CVSS 9.3 Critical" from the highest numeric score, else the first rating, else null. */
export function topScoreText(advisory: KernelAdvisoryInfo | null): string | null {
  const scores = advisory?.scores ?? [];
  const numeric = scores.filter((s) => s.score !== null);
  if (numeric.length > 0) {
    const top = numeric.reduce((best, s) => ((s.score as number) > (best.score as number) ? s : best));
    return ["CVSS", (top.score as number).toFixed(1), top.rating].filter(Boolean).join(" ");
  }
  const rated = scores.find((s) => s.rating);
  return rated ? scoreText(rated) : null;
}

export function cveCountText(count: number): string {
  return count === 1 ? "1 CVE" : `${count} CVEs`;
}

/** "1 of 2 members", or "1 of 1 member read" when the member list itself could not be read. */
export function membersText(item: Pick<OpenAdvisory, "affected" | "total" | "unlisted">): string {
  const noun = item.total === 1 ? "member" : "members";
  return `${item.affected} of ${item.total} ${noun}${item.unlisted ? " read" : ""}`;
}
