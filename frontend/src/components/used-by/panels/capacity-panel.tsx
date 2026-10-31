"use client";

import * as React from "react";
import { Progress } from "@/components/ui/progress";
import { EmptyState } from "@/components/ui/empty-state";
import { formatBytes, formatCount } from "@/lib/utils";
import { capacityReading } from "@/lib/capacity-reading";
import { Database } from "lucide-react";
import type { UsageDict } from "../types";

// Space AND inodes. The storage-pools page already fetches both from /resources-all and has
// only ever rendered `space.used / space.total`, so the inode figures were being paid for on
// every page load and thrown away. A pool can be perfectly healthy on bytes and full on inodes.
function Meter({
  title,
  usage,
  format,
}: {
  title: string;
  usage?: UsageDict;
  format: (value: number) => string;
}) {
  const reading = capacityReading(usage);
  if (!reading) return null;
  const { used, total } = reading;
  const percent = Math.round(reading.percent);

  return (
    <div className="space-y-2 rounded-lg border p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-medium">{title}</span>
        <span className="text-sm text-muted-foreground">{percent}%</span>
      </div>
      <Progress value={percent} />
      <dl className="grid grid-cols-3 gap-2 text-sm">
        <div>
          <dt className="text-xs text-muted-foreground">Used</dt>
          <dd className="font-medium">{format(used)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Free</dt>
          <dd className="font-medium">{format(Math.max(0, total - used))}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Total</dt>
          <dd className="font-medium">{format(total)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function CapacityPanel({ space, inodes }: { space?: UsageDict; inodes?: UsageDict }) {
  const hasAny = capacityReading(space) !== null || capacityReading(inodes) !== null;
  if (!hasAny) {
    return (
      <EmptyState
        icon={Database}
        title="No capacity reported"
        description="The cluster did not report usage figures for this pool."
      />
    );
  }
  return (
    <div className="space-y-3">
      <Meter title="Space" usage={space} format={formatBytes} />
      <Meter title="Inodes" usage={inodes} format={(value) => formatCount(value)} />
    </div>
  );
}
