"use client";

import { cn } from "@/lib/utils";
import { UsageBar } from "@/components/ui/usage-bar";
import { usagePercent } from "@/lib/usage-tone";

export interface UsageMeterProps {
  label: string;
  /** Right-hand value, ex; "30.84 GB / 66.00 GB (46.7%)". */
  value: string;
  used: number;
  total: number;
  className?: string;
}

/**
 * One labelled capacity bar: the reading on the right, the bar underneath.
 *
 * Lives in ui/ rather than beside the node detail panels because the instance detail dialog
 * needs the same label-row-over-bar shape, which is the trigger that moved UsageBar here too.
 *
 * The bar itself is components/ui/usage-bar.tsx, shared with the storage pools table, which
 * needs the same track without this label row. The four bars this replaced were hand-rolled
 * with bg-gray-200 dark:bg-gray-700 tracks under bg-blue-600, bg-green-600 and bg-orange-600
 * fills, so one modal used three different hues for one semantic and none of them told an
 * operator whether 91% full was a problem.
 */
export function UsageMeter({ label, value, used, total, className }: UsageMeterProps) {
  return (
    <div className={cn("space-y-1", className)}>
      <div className="flex justify-between text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-semibold tabular-nums">{value}</span>
      </div>
      <UsageBar percent={usagePercent(used, total)} label={label} valueText={value} />
    </div>
  );
}
