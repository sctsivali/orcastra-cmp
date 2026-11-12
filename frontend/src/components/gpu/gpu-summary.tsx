import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { usageLevel } from "@/lib/usage-tone";
import {
  gpuAllocationCaption,
  gpuCardsCaption,
  summarizeGpus,
  type GpuSummarySource,
} from "@/lib/gpu";

import { GpuUtilizationInfo } from "./gpu-utilization-note";

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
    </div>
  );
}

/**
 * The three GPU numbers, shared by the node and cluster member modals. Cards detected is a
 * caption rather than a fourth tile because a display controller cannot be allocated.
 */
export function GpuSummary({ src, allocationUnknown = false }: { src: GpuSummarySource; allocationUnknown?: boolean }) {
  const summary = summarizeGpus(src);
  const cardsCaption = gpuCardsCaption(summary);

  // Allocation is counted from the instance list. When that list could not be read the two
  // tiles show unknown, because zero would read as free capacity.
  return (
    <div className="space-y-3" data-partial={allocationUnknown ? "true" : undefined}>
      <div className="grid grid-cols-3 gap-4">
        <Stat label="Compute GPUs" value={summary.computeTotal} />
        <Stat label="Allocated" value={allocationUnknown ? "?" : summary.used} />
        <Stat label="Available" value={allocationUnknown ? "?" : summary.available} />
      </div>

      {cardsCaption && <p className="text-xs text-muted-foreground">{cardsCaption}</p>}

      {/* A fully allocated GPU leaves no room for another GPU instance, so the bar is on the
          same ramp as every other capacity bar. */}
      {summary.hasCompute && !allocationUnknown && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="text-muted-foreground">Allocation</span>
            <span className="inline-flex items-center gap-1.5 font-medium">
              <UsageCaption level={usageLevel(summary.allocationPercent ?? 0)} rest="foreground">
                {gpuAllocationCaption(summary)}
              </UsageCaption>
              <GpuUtilizationInfo />
            </span>
          </div>
          <UsageBar
            percent={summary.allocationPercent ?? 0}
            label="GPU allocation"
            valueText={gpuAllocationCaption(summary)}
            className="h-2"
          />
        </div>
      )}
    </div>
  );
}
