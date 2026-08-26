import { Sparkles } from "lucide-react";

import { EmptyState } from "@/components/ui/empty-state";
import {
  GPU_EMPTY_TITLE,
  GPU_NO_COMPUTE_TITLE,
  gpuCardsCaption,
  sortGpuCards,
  summarizeGpus,
  type GpuSummarySource,
} from "@/lib/gpu";
import { cn } from "@/lib/utils";

import { GpuCardDetails } from "./gpu-card-details";
import { GpuSummary } from "./gpu-summary";

interface GpuPanelProps {
  src: GpuSummarySource;
  dense?: boolean;
  className?: string;
}

/**
 * The whole GPU surface for one node or cluster member.
 *
 * important: the card list is gated on how many cards exist, never on gpu_total. Gating on
 * the compute count is what made a display-only node say "No GPU cards detected on this node"
 * while the API was returning two cards.
 */
export function GpuPanel({ src, dense = false, className }: GpuPanelProps) {
  const summary = summarizeGpus(src);
  const cards = sortGpuCards(src.gpu_cards ?? []);

  if (!summary.hasCards) {
    return (
      <EmptyState
        icon={Sparkles}
        title={GPU_EMPTY_TITLE}
        description="This node reports no GPU or display card."
      />
    );
  }

  return (
    <div className={cn("space-y-4", className)}>
      {summary.hasCompute ? (
        <GpuSummary src={src} />
      ) : (
        <div className="rounded-lg border border-dashed p-4">
          <p className="font-medium">{GPU_NO_COMPUTE_TITLE}</p>
          <p className="text-sm text-muted-foreground">
            {gpuCardsCaption(summary) ?? `${summary.cardsTotal} detected`}. None of them can be
            allocated as compute capacity.
          </p>
        </div>
      )}

      {cards.length > 0 && (
        <div className="space-y-3 border-t pt-4">
          <h3 className="text-sm font-semibold">Cards on This Node ({summary.cardsTotal})</h3>
          {cards.map((card) => (
            <GpuCardDetails key={card.id ?? card.index} card={card} dense={dense} />
          ))}
        </div>
      )}
    </div>
  );
}
