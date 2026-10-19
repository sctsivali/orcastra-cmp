import type {
  HostKernelRow,
  KernelAdvisoryResponse,
  KernelAdvisoryState,
} from "@/types/kernel-advisory";

export const KERNEL_STATE_DISPLAY: Record<
  KernelAdvisoryState,
  { label: string; cardLabel: string; tone: "negative" | "caution" | "positive" }
> = {
  exposed: { label: "Exposed", cardLabel: "Kernel Exposed", tone: "negative" },
  unverified: { label: "Unverified", cardLabel: "Kernel Unverified", tone: "caution" },
  patched: { label: "Patched", cardLabel: "Kernel Patched", tone: "positive" },
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
