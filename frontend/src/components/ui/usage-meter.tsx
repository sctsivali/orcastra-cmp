"use client";

import { cn } from "@/lib/utils";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { usageLevel, usagePercent } from "@/lib/usage-tone";

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
 * Shared by the node detail panels and the instance detail dialog. The bar itself is
 * components/ui/usage-bar.tsx, which the storage pools table uses without this label row.
 */
export function UsageMeter({ label, value, used, total, className }: UsageMeterProps) {
  const percent = usagePercent(used, total);
  const level = usageLevel(percent);
  return (
    <div className={cn("space-y-1", className)}>
      <div className="flex justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <UsageCaption level={level} rest="foreground" className="text-right font-semibold">
          {value}
        </UsageCaption>
      </div>
      <UsageBar percent={percent} label={label} valueText={value} level={level} />
    </div>
  );
}
