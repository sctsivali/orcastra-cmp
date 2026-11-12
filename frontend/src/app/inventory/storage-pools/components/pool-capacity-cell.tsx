"use client";

import { UsageBar } from "@/components/ui/usage-bar";
import { UsageCaption } from "@/components/ui/usage-caption";
import { TableEmptyValue } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { useByteFormat } from "@/hooks/use-byte-format";
import { usageLevel } from "@/lib/usage-tone";
import { capacityReading } from "@/lib/capacity-reading";

export interface PoolCapacity {
  used: number;
  total: number;
  /** Null when the cluster reported no space figures for this pool. */
  percent: number | null;
}

/**
 * What a pool's `space` block is worth to a table row.
 *
 * important: an absent `space` is not a zero-byte pool. LXD omits the block for object storage
 * drivers, and backend/app/routers/storage_pools.py serialises that as `{}`, so `resources` is
 * truthy while `used` and `total` are undefined. Formatting them directly prints "NaN undefined".
 */
export function poolCapacity(space: unknown): PoolCapacity {
  return capacityReading(space) ?? { used: 0, total: 0, percent: null };
}

/**
 * Used of total, the bar, and the percentage.
 *
 * The bar has a fixed width, so a reader compares fill against fill down the column.
 *
 * The percentage follows the shared caption rule (components/ui/usage-caption.tsx): muted while
 * Normal, because across up to a hundred rows a green number on every one teaches an operator to
 * stop reading the colour, then toned and named. The bar fill keeps the full ramp, because that
 * is the part being scanned.
 */
export function PoolCapacityCell({
  capacity,
  poolName,
  onOpen,
}: {
  capacity: PoolCapacity;
  poolName: string;
  onOpen: () => void;
}) {
  const { formatBytes } = useByteFormat();
  if (capacity.percent === null) {
    return <TableEmptyValue label="Usage unavailable" />;
  }

  const reading = `${formatBytes(capacity.used)} / ${formatBytes(capacity.total)}`;
  const percentText = `${capacity.percent.toFixed(1)}%`;

  return (
    <div className="space-y-1.5">
      {/* important: the accessible name carries the reading as well as the purpose. An
          aria-label replaces the text it sits on, so naming only the action hides the reading
          and fails WCAG 2.5.3. */}
      <TableLink
        onClick={onOpen}
        aria-label={`Show the capacity breakdown for ${poolName}, ${reading}`}
      >
        {reading}
      </TableLink>
      {/* The meter announces the percentage only; the link above already carries the
          used-of-total reading. */}
      <div className="flex items-center gap-2">
        <UsageBar
          className="h-1.5 w-24"
          label={`${poolName} usage`}
          percent={capacity.percent}
          valueText={percentText}
        />
        <UsageCaption level={usageLevel(capacity.percent)} className="text-xs">
          {percentText}
        </UsageCaption>
      </div>
    </div>
  );
}
