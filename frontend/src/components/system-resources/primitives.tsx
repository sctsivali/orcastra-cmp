"use client";

import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

/** A headline figure in its own card, for the numbers an operator scans first. */
export function StatCard({
  icon: Icon,
  title,
  value,
  subtitle,
  children,
  className,
}: {
  icon: React.ElementType;
  title: string;
  value: ReactNode;
  subtitle?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <Icon className="h-4 w-4" aria-hidden="true" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
        {children}
      </CardContent>
    </Card>
  );
}
