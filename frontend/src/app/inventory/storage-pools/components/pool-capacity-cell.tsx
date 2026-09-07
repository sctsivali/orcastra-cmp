"use client";

import { UsageBar } from "@/components/ui/usage-bar";
import { TableEmptyValue } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { cn, formatBytes } from "@/lib/utils";
import { USAGE_ELEVATED_PERCENT, usagePercent, usageTone } from "@/lib/usage-tone";

export interface PoolCapacity {
  used: number;
  total: number;
  /** Null when the cluster reported no space figures for this pool. */
  percent: number | null;
}

/**
 * What a pool's `space` block is worth to a table row.
 *
 * important: an absent `space` is not a zero-byte pool. LXD omits the block entirely for
 * object storage drivers, and backend/app/routers/storage_pools.py serialises that as `{}`
 * rather than dropping the key, so `resources` is truthy while `used` and `total` are
 * undefined. Reading them straight through a formatter is how this cell used to print
 * "NaN undefined / NaN undefined".
 */
export function poolCapacity(space: unknown): PoolCapacity {
  const dict = (space ?? {}) as { used?: unknown; total?: unknown };
  const used = Number(dict.used ?? 0);
  const total = Number(dict.total ?? 0);
  const reported = Number.isFinite(used) && Number.isFinite(total) && total > 0;
  return {
    used: reported ? used : 0,
    total: reported ? total : 0,
    percent: reported ? usagePercent(used, total) : null,
  };
}

/**
 * Used of total, the bar, and the percentage.
 *
 * note: the bar is a fixed width rather than the column's, so a reader compares fill against
 * fill down the column instead of against a track that changes length every row.
 *
 * note: the percentage stays muted until it means something. The dialogs that colour it
 * unconditionally show two or three bars at a time; this table shows up to a hundred rows, and
 * a green number on every one of them teaches an operator to stop reading the colour. The bar
 * fill keeps the full ramp, because that is the part being scanned.
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
  if (capacity.percent === null) {
    return <TableEmptyValue label="Usage unavailable" />;
  }

  const reading = `${formatBytes(capacity.used)} / ${formatBytes(capacity.total)}`;
  const percentText = `${capacity.percent.toFixed(1)}%`;

  return (
    <div className="space-y-1.5">
      {/* important: the accessible name carries the reading as well as the purpose. An
          aria-label replaces the text it sits on, so naming only the action left the one thing
          the cell exists to state unreachable, and put the control outside WCAG 2.5.3. */}
      <TableLink
        onClick={onOpen}
        aria-label={`Show the capacity breakdown for ${poolName}, ${reading}`}
      >
        {reading}
      </TableLink>
      {/* note: the meter announces the percentage only. The link above it already carries the
          used-of-total reading, and repeating it here made the cell read three times over. */}
      <div className="flex items-center gap-2">
        <UsageBar
          className="h-1.5 w-24"
          label={`${poolName} usage`}
          percent={capacity.percent}
          valueText={percentText}
        />
        <span
          className={cn(
            "text-xs tabular-nums",
            capacity.percent >= USAGE_ELEVATED_PERCENT
              ? usageTone(capacity.percent).text
              : "text-muted-foreground",
          )}
        >
          {percentText}
        </span>
      </div>
    </div>
  );
}
