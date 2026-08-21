import { Info } from "lucide-react";

import {
  Tooltip as UITooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { GPU_UTILIZATION_NOTE } from "@/lib/gpu";

/**
 * Says plainly that the console reports GPU allocation, not utilization.
 *
 * A real button rather than a hover-only div: the sentence has to be reachable by keyboard
 * and on touch, and the accessible name carries the meaning even if the tooltip never opens.
 */
export function GpuUtilizationInfo({ className }: { className?: string }) {
  return (
    <UITooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={
            "inline-flex items-center rounded-sm text-muted-foreground transition-colors " +
            "hover:text-foreground focus-visible:outline-none focus-visible:ring-2 " +
            "focus-visible:ring-ring focus-visible:ring-offset-1 " +
            (className ?? "")
          }
          aria-label="Why no GPU utilization is shown"
        >
          <Info className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        <p className="text-sm">{GPU_UTILIZATION_NOTE}</p>
      </TooltipContent>
    </UITooltip>
  );
}
