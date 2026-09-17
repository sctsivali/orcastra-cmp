"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

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

/** A percentage bar whose fill steps from calm to alarming as it fills. */
export function UsageProgress({ value, className }: { value: number; className?: string }) {
  const getColor = (val: number) => {
    if (val < 50) return "bg-green-500";
    if (val < 75) return "bg-yellow-500";
    if (val < 90) return "bg-orange-500";
    return "bg-red-500";
  };
  return (
    <div className={cn("relative h-3 w-full overflow-hidden rounded-full bg-secondary", className)}>
      <div
        className={cn("h-full transition-all duration-300", getColor(value))}
        style={{ width: `${Math.min(Math.max(value, 0), 100)}%` }}
      />
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
      <p className="text-sm font-medium">{value}</p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}
