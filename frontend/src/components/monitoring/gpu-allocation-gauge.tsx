import { Sparkles } from "lucide-react";

import { GpuUtilizationInfo } from "@/components/gpu";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuAllocationCaption,
  gpuCardsCaption,
  gpuNoComputeCaption,
  summarizeGpus,
} from "@/lib/gpu";

import { GaugeChart } from "./gauge-chart";

interface GpuAllocationGaugeProps {
  stats: {
    total_gpu: number;
    used_gpu: number;
    total_gpu_cards?: number;
    total_gpu_display_cards?: number;
    total_gpu_integrated_cards?: number;
  };
  /** What the empty copy calls the scope: a cluster's name, or "all" for the whole fleet. */
  scope: string;
}

/**
 * The fleet (or single node) GPU allocation gauge.
 *
 * important: with no compute GPU there is no arc. A 0.0% arc over a denominator of zero reads
 * as a failed probe, which is exactly how "0 / 1 GPUs" came to be reported as "the GPU is not
 * detected". State the fact instead, and always show the cards count beside the ratio.
 */
export function GpuAllocationGauge({ stats, scope }: GpuAllocationGaugeProps) {
  const gpu = summarizeGpus({
    gpu_total: stats.total_gpu,
    gpu_used: stats.used_gpu,
    gpu_cards_total: stats.total_gpu_cards,
    // With the per-class counts the fleet says "2 display cards only", as each cluster row does.
    gpu_display_total: stats.total_gpu_display_cards,
    gpu_integrated_total: stats.total_gpu_integrated_cards,
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
            ? gpuNoComputeCaption(gpu)
            : scope === "all"
              ? "No GPU on any cluster"
              : `No GPU on ${scope}`}
        </p>
      </div>
    );
  }

  return (
    <>
      <GaugeChart value={gpu.allocationPercent ?? 0} label="GPU Allocation" kind="allocation" />
      <p className="mt-2 text-sm text-muted-foreground">{gpuAllocationCaption(gpu)}</p>
      <p className="mt-0.5 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        {cardsCaption}
        <GpuUtilizationInfo />
      </p>
    </>
  );
}
