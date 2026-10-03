import { usagePercent } from "@/lib/usage-tone";

export interface CapacityReading {
  used: number;
  total: number;
  percent: number;
}

/** A missing counter is not an observed zero. Numeric strings are accepted at the API boundary. */
function counter(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

export function capacityReading(usage: unknown): CapacityReading | null {
  if (!usage || typeof usage !== "object") return null;
  const values = usage as { used?: unknown; total?: unknown };
  const used = counter(values.used);
  const total = counter(values.total);
  if (used === null || total === null || (total === 0 && used !== 0)) return null;
  return { used, total, percent: usagePercent(used, total) };
}
