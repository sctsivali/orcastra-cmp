"use client";

import { cn } from "@/lib/utils";

export interface UsageMeterProps {
  label: string;
  /** Right-hand value, ex; "30.84 GB / 66.00 GB (46.7%)". */
  value: string;
  used: number;
  total: number;
  className?: string;
}

export function usagePercent(used: number, total: number): number {
  if (!total || total <= 0) return 0;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

/**
 * One capacity bar.
 *
 * important: the fill is a severity signal, not decoration. The four bars this replaces were
 * hand-rolled with bg-gray-200 dark:bg-gray-700 tracks under bg-blue-600, bg-green-600 and
 * bg-orange-600 fills, so one modal used three different hues for one semantic and none of
 * them told an operator whether 91% full was a problem. Semantic tokens only, and no
 * animation: a bar that is not moving must not look like it is.
 */
export function UsageMeter({ label, value, used, total, className }: UsageMeterProps) {
  const percent = usagePercent(used, total);
  const fill =
    percent >= 90 ? "bg-destructive" : percent >= 75 ? "bg-status-pending" : "bg-success";

  return (
    <div className={cn("space-y-1", className)}>
      <div className="flex justify-between text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-semibold tabular-nums">{value}</span>
      </div>
      <div
        aria-label={`${label}: ${value}`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.round(percent)}
        className="h-2.5 w-full overflow-hidden rounded-full bg-secondary"
        role="meter"
      >
        <div className={cn("h-full rounded-full", fill)} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
