"use client";

import { cn } from "@/lib/utils";
import { usageTone } from "@/lib/usage-tone";

export interface UsageBarProps {
  /** Already clamped to 0-100 by usagePercent. */
  percent: number;
  /** What the bar measures, ex; "Memory". Announced with the value. */
  label: string;
  /** The reading in the caller's own words, ex; "30.84 GB / 66.00 GB (46.7%)". */
  valueText: string;
  className?: string;
}

/**
 * The capacity bar itself: track, fill, and the meter semantics.
 *
 * important: the fill is a severity signal, not decoration, so the colour comes from
 * usageTone and nothing else. No animation either: a bar that is not moving must not look
 * like it is.
 *
 * Lives in ui/ rather than beside UsageMeter because the storage pools table needs the bar
 * without the label row above it, and a second hand-rolled track is exactly how this console
 * ended up with three hues for one semantic.
 */
export function UsageBar({ percent, label, valueText, className }: UsageBarProps) {
  const { fill } = usageTone(percent);

  return (
    <div
      aria-label={`${label}: ${valueText}`}
      aria-valuemax={100}
      aria-valuemin={0}
      aria-valuenow={Math.round(percent)}
      className={cn("h-2.5 w-full overflow-hidden rounded-full bg-secondary", className)}
      role="meter"
    >
      <div className={cn("h-full rounded-full", fill)} style={{ width: `${percent}%` }} />
    </div>
  );
}
