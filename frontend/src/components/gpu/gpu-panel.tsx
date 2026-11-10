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
  /** The instance list behind the allocation could not be read; show the allocation as unknown. */
  allocationUnknown?: boolean;
  /** Whose cards these are, for the copy: a whole cluster, or one member of it. */
  scope?: "cluster" | "member";
  className?: string;
}

/**
 * The whole GPU surface for one cluster or cluster member.
 *
 * important: the card list is gated on how many cards exist, never on gpu_total. gpu_total
 * counts compute cards only, so gating on it would hide every card of a display-only node.
 */
export function GpuPanel({
  src,
  dense = false,
  allocationUnknown = false,
  scope = "cluster",
  className,
}: GpuPanelProps) {
  const summary = summarizeGpus(src);
  const scopeNoun = scope === "member" ? "member" : "cluster";
  const cards = sortGpuCards(src.gpu_cards ?? []);

  if (!summary.hasCards) {
    return (
      <EmptyState
        icon={Sparkles}
        title={GPU_EMPTY_TITLE}
        description={`This ${scopeNoun} reports no GPU or display card.`}
      />
    );
  }

  return (
    <div className={cn("space-y-4", className)}>
      {summary.hasCompute ? (
        <GpuSummary src={src} allocationUnknown={allocationUnknown} />
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
          <h3 className="text-sm font-semibold">
            Cards on This {scope === "member" ? "Member" : "Cluster"} ({summary.cardsTotal})
          </h3>
          {cards.map((card) => (
            <GpuCardDetails key={card.id ?? card.index} card={card} dense={dense} />
          ))}
        </div>
      )}
    </div>
  );
}
