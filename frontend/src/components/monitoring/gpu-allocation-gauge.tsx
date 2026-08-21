import { Sparkles } from "lucide-react";

import { GpuUtilizationInfo } from "@/components/gpu";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuAllocationCaption,
  gpuCardsCaption,
  summarizeGpus,
} from "@/lib/gpu";

import { GaugeChart } from "./gauge-chart";

interface GpuAllocationGaugeProps {
  stats: { total_gpu: number; used_gpu: number; total_gpu_cards?: number };
  /** Node name, or "all" for the whole fleet. Used only in the empty copy. */
  scope: string;
  color: string;
}

/**
 * The fleet (or single node) GPU allocation gauge.
 *
 * important: with no compute GPU there is no arc. A 0.0% arc over a denominator of zero reads
 * as a failed probe, which is exactly how "0 / 1 GPUs" came to be reported as "the GPU is not
 * detected". State the fact instead, and always show the cards count beside the ratio.
 */
export function GpuAllocationGauge({ stats, scope, color }: GpuAllocationGaugeProps) {
  const gpu = summarizeGpus({
    gpu_total: stats.total_gpu,
    gpu_used: stats.used_gpu,
    gpu_cards_total: stats.total_gpu_cards,
  });
  const cardsCaption = gpuCardsCaption(gpu);

  if (!gpu.hasCompute) {
    return (
      <div className="flex h-[140px] flex-col items-center justify-center gap-1.5 text-center">
        <Sparkles className="h-8 w-8 text-muted-foreground/70" aria-hidden="true" />
        <p className="text-sm font-medium">
          {gpu.hasCards ? GPU_NO_COMPUTE_TITLE : GPU_EMPTY_TITLE}
        </p>
        <p className="text-xs text-muted-foreground">
          {gpu.hasCards
            ? `${cardsCaption ?? `${gpu.cardsTotal} detected`}, none allocatable`
            : scope === "all"
              ? "No GPU on any node"
              : `No GPU on ${scope}`}
        </p>
      </div>
    );
  }

  return (
    <>
      <GaugeChart value={gpu.allocationPercent ?? 0} label="GPU Allocation" color={color} />
      <p className="mt-2 text-sm text-muted-foreground">{gpuAllocationCaption(gpu)}</p>
      <p className="mt-0.5 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        {cardsCaption}
        <GpuUtilizationInfo />
      </p>
    </>
  );
}
