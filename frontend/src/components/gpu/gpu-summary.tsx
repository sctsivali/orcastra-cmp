import { Progress } from "@/components/ui/progress";
import {
  gpuAllocationCaption,
  gpuCardsCaption,
  summarizeGpus,
  type GpuSummarySource,
} from "@/lib/gpu";

import { GpuUtilizationInfo } from "./gpu-utilization-note";

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
    </div>
  );
}

/**
 * The three GPU numbers, identical in the node modal and the cluster member modal.
 *
 * Cards detected is a caption rather than a fourth tile on purpose: a tile would imply it is
 * the same kind of number as the other three, and a display controller cannot be allocated.
 */
export function GpuSummary({ src }: { src: GpuSummarySource }) {
  const summary = summarizeGpus(src);
  const cardsCaption = gpuCardsCaption(summary);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-4">
        <Stat label="Compute GPUs" value={summary.computeTotal} />
        <Stat label="Allocated" value={summary.used} />
        <Stat label="Available" value={summary.available} />
      </div>

      {cardsCaption && <p className="text-xs text-muted-foreground">{cardsCaption}</p>}

      {summary.hasCompute && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">Allocation</span>
            <span className="inline-flex items-center gap-1.5 font-medium">
              {gpuAllocationCaption(summary)}
              <GpuUtilizationInfo />
            </span>
          </div>
          <Progress value={summary.allocationPercent ?? 0} className="h-2" />
        </div>
      )}
    </div>
  );
}
