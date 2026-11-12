import { capacityReading, type CapacityReading } from "@/lib/capacity-reading";

export interface StorageSource {
  storage_used?: number | null;
  storage_total?: number | null;
  storage_pools?: ReadonlyArray<unknown> | null;
  degraded_parts?: ReadonlyArray<string> | null;
}

/**
 * A cluster's storage as one card can state it.
 *
 * - `reading`: used of usable total, or null when there is nothing honest to draw.
 * - `state`: "measured"; "partial" when some pools did not answer, so the figure is a floor;
 *   "unknown" when the read failed; "none" when the cluster answered with no pool at all.
 *
 * important: a failed storage read arrives as zeroes. 0 B / 0 B with an empty bar reads as an
 * empty disk, so a lost read is "unknown" and is never drawn (backend `degraded_parts`).
 */
export interface StorageReading {
  reading: CapacityReading | null;
  state: "measured" | "partial" | "unknown" | "none";
}

export function storageReading(src: StorageSource | null | undefined): StorageReading {
  if (!src) return { reading: null, state: "unknown" };
  const lost = (src.degraded_parts ?? []).some((part) => part.startsWith("storage"));
  const pools = src.storage_pools ?? [];
  if (lost && pools.length === 0) return { reading: null, state: "unknown" };
  const reading = capacityReading({ used: src.storage_used, total: src.storage_total });
  if (!reading) return { reading: null, state: "unknown" };
  if (reading.total === 0) {
    // A pool list with pools in it but no space figures (object storage only) is not "none".
    return { reading: null, state: pools.length === 0 ? "none" : "unknown" };
  }
  return { reading, state: lost ? "partial" : "measured" };
}
