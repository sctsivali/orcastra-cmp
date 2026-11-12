"use client";

import * as React from "react";
import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { EmptyState } from "@/components/ui/empty-state";
import { formatCount } from "@/lib/utils";
import { useByteFormat } from "@/hooks/use-byte-format";
import { capacityReading } from "@/lib/capacity-reading";
import { usageLevel } from "@/lib/usage-tone";
import { Database } from "lucide-react";
import type { UsageDict } from "../types";

// Space and inodes, because a pool can be healthy on bytes and full on inodes.
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
  const { used, total, percent } = reading;
  // One decimal, as the pools table beside this panel prints it: rounding would show 90%
  // under a bar the table calls 89.6%.
  const percentText = `${percent.toFixed(1)}%`;

  return (
    <div className="space-y-2 rounded-lg border p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-medium">{title}</span>
        <UsageCaption level={usageLevel(percent)} className="text-sm">
          {percentText}
        </UsageCaption>
      </div>
      <UsageBar
        percent={percent}
        label={title}
        valueText={`${format(used)} of ${format(total)} (${percentText})`}
      />
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
  const { formatBytes } = useByteFormat();
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
