import { formatVcpuCount, plural } from "@/lib/utils";

/**
 * CPU allocation as the console presents it. The API's cpu_used is the sum of the running
 * instances' CPU limits, which KVM lets exceed the host's CPU thread count. Over-commit is
 * normal, so it is shown as a ratio instead of a percentage past 100, with a warning tone only
 * past the point a general-purpose fleet is normally sized to.
 */

// 4:1 is the conventional ceiling for general workloads (the KVM and OpenStack default
// cpu_allocation_ratio); latency-sensitive fleets tune lower.
export const CPU_OVERCOMMIT_WARN_RATIO = 4;

export type CpuTone = "within" | "overcommitted" | "warning";

export interface CpuAllocationSource {
  cpu_total: number;
  cpu_used: number;
  cpu_allocated?: number;
  cpu_allocation_ratio?: number;
}

export interface CpuAllocationView {
  allocated: number;
  threads: number;
  ratio: number;
  /** "3.8x" */
  ratioLabel: string;
  /** Allocation as a percentage of threads, held inside [0, 100] for a meter. */
  percentClamped: number;
  /** vCPUs beyond the thread count, 0 when within capacity. */
  over: number;
  /** Threads not yet allocated, 0 when over-committed. */
  unallocated: number;
  tone: CpuTone;
  /** Utility for a Progress track's fill, ex; "[&>div]:bg-success". Literal so Tailwind emits it. */
  meterClassName: string;
  textClassName: string;
}

export function cpuAllocation(src: CpuAllocationSource): CpuAllocationView {
  const allocated = src.cpu_allocated ?? src.cpu_used ?? 0;
  const threads = src.cpu_total ?? 0;
  const ratio = src.cpu_allocation_ratio ?? (threads > 0 ? allocated / threads : 0);
  const tone: CpuTone = ratio <= 1 ? "within" : ratio <= CPU_OVERCOMMIT_WARN_RATIO ? "overcommitted" : "warning";
  return {
    allocated,
    threads,
    ratio,
    ratioLabel: `${ratio.toFixed(1)}x`,
    percentClamped: threads > 0 ? Math.min(100, Math.max(0, (allocated / threads) * 100)) : 0,
    over: Math.max(0, allocated - threads),
    unallocated: Math.max(0, threads - allocated),
    tone,
    meterClassName:
      tone === "within"
        ? "[&>div]:bg-success"
        : tone === "overcommitted"
          ? "[&>div]:bg-status-unknown"
          : "[&>div]:bg-status-pending",
    textClassName: tone === "warning" ? "text-status-pending" : "text-muted-foreground",
  };
}

/**
 * The host capacity unit. "CPU thread" keeps the word a non-specialist knows (CPU) and the exact
 * kind (a thread, which is what LXD counts: cores x threads per core).
 */
export function formatCpuThreads(count: number): string {
  return plural(count, "CPU thread");
}

/**
 * "25 vCPUs / 12 CPU threads": the one way the allocation figure is written. On a narrow card
 * render it with CpuAllocationFigure (components/cpu), which keeps each side whole.
 */
export function cpuAllocationFigure(view: Pick<CpuAllocationView, "allocated" | "threads">): string {
  return `${formatVcpuCount(view.allocated)} / ${formatCpuThreads(view.threads)}`;
}

/** "0.4x allocated", "2.1x over-committed", or "5.2x, heavily over-committed" past 4x. */
export function cpuAllocationText(view: CpuAllocationView): string {
  if (view.tone === "within") return `${view.ratioLabel} allocated`;
  if (view.tone === "overcommitted") return `${view.ratioLabel} over-committed`;
  return `${view.ratioLabel}, heavily over-committed`;
}
