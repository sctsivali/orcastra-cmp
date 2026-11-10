import { ShieldAlert, ShieldCheck, ShieldQuestion, type LucideIcon } from "lucide-react";
import type {
  HostKernelRow,
  KernelAdvisoryInfo,
  KernelAdvisoryResponse,
  KernelAdvisoryScore,
  KernelAdvisoryState,
  KernelCveCheck,
} from "@/types/kernel-advisory";
import { plural } from "@/lib/utils";

// The wire values stay exposed/unverified/patched (backend, URL facets, the warning feed); only
// the labels use CVE vocabulary. The icon and its token mirror lib/cluster-status.ts
// ClusterStatusMeta, so the state never rides on colour alone.
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
  /** Some member list could not be read, so `total` counts only the members read. */
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
 * verdict (state null), so "not read" never reads as patched.
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
  return plural(count, "CVE");
}

/** "1 of 2 members", or "1 of 1 member read" when the member list itself could not be read. */
export function membersText(item: Pick<OpenAdvisory, "affected" | "total" | "unlisted">): string {
  return `${item.affected} of ${plural(item.total, "member")}${item.unlisted ? " read" : ""}`;
}

/**
 * How current the Ubuntu statuses behind a verdict are. `fresh` and `stale` carry the last
 * successful read; the other three say why the built-in data decided instead.
 */
export type UbuntuCheck =
  | { kind: "fresh" | "stale"; checkedAt: string }
  | { kind: "off" | "unreachable" | "not-yet"; dataAsOf: string };

const UBUNTU_CHECK_RANK: Record<UbuntuCheck["kind"], number> = {
  off: 0,
  unreachable: 1,
  "not-yet": 2,
  stale: 3,
  fresh: 4,
};

type UbuntuCheckFields = Partial<Pick<KernelCveCheck, "source" | "checked_at" | "stale" | "check_error">> &
  Pick<KernelCveCheck, "data_as_of">;

export function ubuntuCheck(entry: UbuntuCheckFields, enabled = true): UbuntuCheck {
  if (!enabled) return { kind: "off", dataAsOf: entry.data_as_of };
  if (entry.source === "ubuntu" && entry.checked_at) {
    return { kind: entry.stale ? "stale" : "fresh", checkedAt: entry.checked_at };
  }
  return { kind: entry.check_error ? "unreachable" : "not-yet", dataAsOf: entry.data_as_of };
}

/** The least current of several CVEs' checks: the line has to hold for every one of them. */
export function leastCurrentUbuntuCheck(
  entries: readonly UbuntuCheckFields[],
  enabled = true,
): UbuntuCheck | null {
  let worst: UbuntuCheck | null = null;
  for (const entry of entries) {
    const check = ubuntuCheck(entry, enabled);
    if (
      !worst ||
      UBUNTU_CHECK_RANK[check.kind] < UBUNTU_CHECK_RANK[worst.kind] ||
      ("checkedAt" in check && "checkedAt" in worst && check.kind === worst.kind && check.checkedAt < worst.checkedAt)
    ) {
      worst = check;
    }
  }
  return worst;
}

/** "Ubuntu status checked 2h ago", or why the built-in data decided. `relative` is the live age. */
export function ubuntuCheckText(check: UbuntuCheck, relative: string, formatWhen: (iso: string) => string): string {
  switch (check.kind) {
    case "fresh":
      return `Ubuntu status checked ${relative}`;
    case "stale":
      return `Could not check Ubuntu since ${formatWhen(check.checkedAt)}`;
    case "off":
      return `Using built-in data from ${check.dataAsOf}; the Ubuntu check is turned off`;
    case "unreachable":
      return `Using built-in data from ${check.dataAsOf}; Ubuntu could not be reached`;
    case "not-yet":
      return `Using built-in data from ${check.dataAsOf}; Ubuntu has not been checked yet`;
  }
}
