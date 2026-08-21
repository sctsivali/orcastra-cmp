import { Loader2, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { GpuClassBadge } from "@/components/gpu";
import {
  GPU_CLASS_LABEL,
  GPU_NOT_ALLOCATABLE,
  gpuCardLabel,
  gpuCardQualifier,
  gpuClassOf,
  gpuIdentifier,
  sortGpuCards,
} from "@/lib/gpu";
import { cn, formatBytes } from "@/lib/utils";
import type { GPUCardInfo } from "@/types/api";

interface GpuPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  nodeName: string;
  cards: GPUCardInfo[];
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  onSelect: (card: GPUCardInfo) => void;
  onAddCustom: () => void;
}

/**
 * Pick the GPU to pass through.
 *
 * Sorting is the recommendation: compute cards come first, so the top row is the right answer
 * without a "Recommended" column to read. A display controller stays selectable, because LXD
 * will pass one through and some VMs want the framebuffer, but it sorts last, reads muted and
 * says plainly that it cannot be allocated.
 *
 * The identifier is the point of the redesign. On hardware with no PCI address (a Tegra iGPU)
 * the old table showed two rows of dashes and a 0 and a 1, and picking the wrong one silently
 * attached the display controller. Every row now names the card, its class, and the exact
 * selector that will be written into the device.
 */
export function GpuPickerDialog({
  open,
  onOpenChange,
  nodeName,
  cards,
  isLoading,
  isError,
  onRetry,
  onSelect,
  onAddCustom,
}: GpuPickerDialogProps) {
  const sorted = sortGpuCards(cards);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[56rem] max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Select GPU</DialogTitle>
          <DialogDescription>
            Cards detected on {nodeName || "the node"}. Compute GPUs are listed first. A display
            controller can be attached but cannot run compute work.
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div
            role="status"
            aria-live="polite"
            className="flex items-center gap-2 py-6 text-sm text-muted-foreground"
          >
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Reading GPUs from {nodeName || "the node"}
          </div>
        ) : isError ? (
          <div className="space-y-3 py-2">
            <p className="text-sm text-destructive">
              Could not read GPUs from {nodeName || "the node"}. This does not mean the node has
              none.
            </p>
            <div className="flex gap-2">
              <Button type="button" variant="outline" size="sm" onClick={onRetry}>
                Retry
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={onAddCustom}>
                <Plus className="h-4 w-4 mr-1" />
                Add custom GPU device
              </Button>
            </div>
          </div>
        ) : sorted.length === 0 ? (
          <div className="space-y-3 py-2">
            <p className="text-sm text-muted-foreground">No GPUs detected on this node.</p>
            <Button type="button" variant="outline" size="sm" onClick={onAddCustom}>
              <Plus className="h-4 w-4 mr-1" />
              Add custom GPU device
            </Button>
          </div>
        ) : (
          <div className="space-y-2">
            {sorted.map((card) => {
              const cls = gpuClassOf(card);
              const identifier = gpuIdentifier(card);
              const display = cls === "display";
              return (
                <div
                  key={`${identifier.by}-${identifier.value}-${card.index}`}
                  className={cn(
                    "flex items-start justify-between gap-4 rounded-lg border p-3",
                    display && "border-dashed"
                  )}
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className={cn("font-medium", display && "text-muted-foreground")}>
                        {gpuCardLabel(card)}
                      </p>
                      <GpuClassBadge card={card} />
                      {display ? (
                        <span className="text-xs text-muted-foreground">
                          {GPU_NOT_ALLOCATABLE}
                        </span>
                      ) : card.in_use ? (
                        <span className="text-xs text-muted-foreground">
                          In use{card.used_by ? ` by ${card.used_by}` : ""}
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">Available</span>
                      )}
                    </div>
                    <p className="truncate text-xs text-muted-foreground">
                      {gpuCardQualifier(card)}
                    </p>
                    <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                      <span className="font-mono">{identifier.display}</span>
                      {card.memory_total ? (
                        <span>
                          {formatBytes(card.memory_total)}
                          {card.memory_shared ? " unified" : ""}
                        </span>
                      ) : null}
                    </div>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    onClick={() => onSelect(card)}
                    aria-label={`Attach ${gpuCardLabel(card)} (${GPU_CLASS_LABEL[cls]})`}
                  >
                    Select
                  </Button>
                </div>
              );
            })}
            <div className="flex justify-end pt-1">
              <Button type="button" variant="ghost" size="sm" onClick={onAddCustom}>
                <Plus className="h-4 w-4 mr-1" />
                Add custom GPU device
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
