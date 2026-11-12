"use client";

import { cn } from "@/lib/utils";
import { toneForLevel, usageLevel, type UsageLevel } from "@/lib/usage-tone";

export interface UsageBarProps {
  /** Already clamped to 0-100 by usagePercent. */
  percent: number;
  /** What the bar measures, ex; "Memory". Announced with the value. */
  label: string;
  /** The reading in the caller's own words, ex; "30.84 GB / 66.00 GB (46.7%)". */
  valueText: string;
  /**
   * The band, when the reading has its own scale. Defaults to the usage ramp on `percent`.
   * vCPU allocation passes its own, because its bar is measured against the over-commit
   * ceiling and turns red only past it (lib/cpu-allocation.ts).
   */
  level?: UsageLevel;
  className?: string;
}

/**
 * The capacity bar itself: track, fill, and the meter semantics.
 *
 * The fill is a severity signal, so its colour comes from the usage ramp and nothing else. No
 * animation either: a bar that is not moving must not look like it is.
 *
 * Separate from UsageMeter because the storage pools table needs the bar without the label row.
 */
export function UsageBar({ percent, label, valueText, level, className }: UsageBarProps) {
  const safe = Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : 0;
  const band = level ?? usageLevel(safe);
  const { fill } = toneForLevel(band);
  // The band is named as well as painted, so a screen reader hears what the colour says.
  const name = band === "Normal" ? `${label}: ${valueText}` : `${label}: ${valueText}, ${band.toLowerCase()}`;

  return (
    <div
      aria-label={name}
      aria-valuemax={100}
      aria-valuemin={0}
      aria-valuenow={Math.round(safe)}
      className={cn("h-2.5 w-full overflow-hidden rounded-full bg-secondary", className)}
      data-level={band.toLowerCase()}
      role="meter"
    >
      <div className={cn("h-full rounded-full", fill)} style={{ width: `${safe}%` }} />
    </div>
  );
}
