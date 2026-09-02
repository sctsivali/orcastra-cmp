/**
 * Byte formatting for the member resource dialog.
 *
 * note: deliberately not `formatBytes` from lib/utils. That one answers "0 Bytes" where this
 * surface has always shown "0 B", and the compact split into value and unit has no twin there
 * at all. What it does share is the rule those two functions now encode: a size on disk is
 * decimal because its label is decimal, and memory is binary because a DIMM is a power of two
 * and every other tool an operator has open says GiB.
 */

const SI_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;
const IEC_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"] as const;

function scale(
  bytes: number,
  base: number,
  units: readonly string[],
): { value: number; unit: string } {
  if (!Number.isFinite(bytes) || bytes <= 0) return { value: 0, unit: units[0] };
  const index = Math.min(
    Math.floor(Math.log(bytes) / Math.log(base)),
    units.length - 1,
  );
  return { value: bytes / Math.pow(base, index), unit: units[index] };
}

export function formatBytes(bytes: number): string {
  const { value, unit } = scale(bytes, 1000, SI_UNITS);
  return bytes > 0 ? `${value.toFixed(2)} ${unit}` : "0 B";
}

export function formatBytesCompact(bytes: number): { value: string; unit: string } {
  const { value, unit } = scale(bytes, 1000, SI_UNITS);
  return bytes > 0 ? { value: value.toFixed(2), unit } : { value: "0", unit: "B" };
}

export function formatMemoryCompact(bytes: number): { value: string; unit: string } {
  const { value, unit } = scale(bytes, 1024, IEC_UNITS);
  return bytes > 0 ? { value: value.toFixed(2), unit } : { value: "0", unit: "B" };
}
