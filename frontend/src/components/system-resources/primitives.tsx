"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { usageLevel, usagePercent } from "@/lib/usage-tone";

export function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  return parts.length > 0 ? parts.join(" ") : "< 1m";
}

/** "80.5% · High" through the shared caption rule: neutral while Normal, toned and named above. */
export function StatusPercent({ percent }: { percent: number }) {
  return (
    <UsageCaption level={usageLevel(percent)} rest="foreground" className="font-medium">
      {percent.toFixed(1)}%
    </UsageCaption>
  );
}

/**
 * One utilisation reading: label and status on top, the shared meter bar, then a caption that
 * says what the number covers. A null percent draws an empty track rather than a green bar at
 * zero, and says whether a reading is on its way (`measuring`) or was never reported.
 */
export function MeterRow({
  label,
  percent,
  caption,
  measuring = false,
}: {
  label: string;
  percent: number | null;
  caption?: ReactNode;
  measuring?: boolean;
}) {
  const clamped = percent === null ? 0 : usagePercent(percent, 100);
  const pending = measuring ? "Measuring…" : EMPTY_VALUE;
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        {percent === null ? (
          <span className="text-muted-foreground">{pending}</span>
        ) : (
          <StatusPercent percent={percent} />
        )}
      </div>
      <UsageBar
        percent={clamped}
        label={label}
        valueText={percent === null ? (measuring ? "measuring" : "not reported") : `${percent.toFixed(1)}%`}
      />
      {caption && <p className="text-xs text-muted-foreground">{caption}</p>}
    </div>
  );
}

/** A labelled figure with an optional second line, laid out as a row of tiles. */
export function Tile({
  icon: Icon,
  label,
  value,
  sub,
  className,
}: {
  icon?: React.ElementType;
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("text-center p-2 bg-muted/50 rounded-lg", className)}>
      {Icon && <Icon className="h-4 w-4 mx-auto mb-1 text-muted-foreground" aria-hidden="true" />}
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm font-medium tabular-nums">{value}</p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

/** One label and value pair inside a FactList. */
export function Fact({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("p-2 bg-muted/30 rounded", className)}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium">{children}</dd>
    </div>
  );
}

export function FactList({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={cn("grid grid-cols-1 sm:grid-cols-2 gap-2", className)}>{children}</dl>;
}
