import { formatVcpuCount, plural } from "@/lib/utils";
import { USAGE_ELEVATED_PERCENT, type UsageLevel } from "@/lib/usage-tone";

/**
 * CPU allocation as the console presents it. The API's cpu_used is the sum of the running
 * instances' CPU limits, which KVM lets exceed the host's CPU thread count. Over-commit is
 * normal, so the figure is a ratio instead of a percentage past 100.
 *
 * The meter is measured against the over-commit ceiling, the way a scheduler sizes vCPU
 * capacity (threads x allocation ratio, as in OpenStack Placement), so it fills at 4x and lands
 * on the same colours as every other capacity bar: amber from 3x, red only past the ceiling.
 * Against the thread count alone every over-committed cluster would draw the same full bar.
 */

// 4:1 is the conventional ceiling for general workloads (the KVM and OpenStack default
// cpu_allocation_ratio); latency-sensitive fleets tune lower.
export const CPU_OVERCOMMIT_WARN_RATIO = 4;

// The amber band starts where it does on every other meter, three quarters of the scale: 3x.
export const CPU_ELEVATED_RATIO = (CPU_OVERCOMMIT_WARN_RATIO * USAGE_ELEVATED_PERCENT) / 100;

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
  /** Allocation as a share of the over-commit ceiling (threads x 4), held inside [0, 100]. */
  meterPercent: number;
  /** vCPUs beyond the thread count, 0 when within capacity. */
  over: number;
  /** Threads not yet allocated, 0 when over-committed. */
  unallocated: number;
  /** Which words the ratio takes: "allocated", "over-committed", "heavily over-committed". */
  tone: CpuTone;
  /** The meter's band: High from 3x, Critical past 4x. */
  level: UsageLevel;
}

/** A counter as a non-negative finite number; anything else is no reading, which is 0 here. */
function count(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

export function cpuLevel(ratio: number): UsageLevel {
  if (ratio > CPU_OVERCOMMIT_WARN_RATIO) return "Critical";
  if (ratio >= CPU_ELEVATED_RATIO) return "High";
  return "Normal";
}

export function cpuAllocation(src: CpuAllocationSource): CpuAllocationView {
  const allocated = count(src.cpu_allocated ?? src.cpu_used);
  const threads = count(src.cpu_total);
  const ratio =
    typeof src.cpu_allocation_ratio === "number" && Number.isFinite(src.cpu_allocation_ratio)
      ? Math.max(0, src.cpu_allocation_ratio)
      : threads > 0
        ? allocated / threads
        : 0;
  const tone: CpuTone = ratio <= 1 ? "within" : ratio <= CPU_OVERCOMMIT_WARN_RATIO ? "overcommitted" : "warning";
  return {
    allocated,
    threads,
    ratio,
    ratioLabel: `${ratio.toFixed(1)}x`,
    meterPercent:
      threads > 0 ? Math.min(100, Math.max(0, (ratio / CPU_OVERCOMMIT_WARN_RATIO) * 100)) : 0,
    over: Math.max(0, allocated - threads),
    unallocated: Math.max(0, threads - allocated),
    tone,
    level: cpuLevel(ratio),
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
