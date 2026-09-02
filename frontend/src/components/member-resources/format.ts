/**
 * Byte formatting for the member resource dialog.
 *
 * note: deliberately not `formatBytes` from lib/utils. That one answers "0 Bytes" and "2 GB"
 * where this surface has always shown "0 B" and "2.00 GB", and the compact split into value
 * and unit has no twin there at all. Unifying them is a visible-string change on a screen the
 * browser suite asserts against, so it belongs with a copy pass rather than with this one.
 */

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

function scale(bytes: number): { value: number; unit: string } {
  if (bytes === 0) return { value: 0, unit: "B" };
  const index = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    UNITS.length - 1,
  );
  return { value: bytes / Math.pow(1024, index), unit: UNITS[index] };
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const { value, unit } = scale(bytes);
  return `${value.toFixed(2)} ${unit}`;
}

export function formatBytesCompact(bytes: number): { value: string; unit: string } {
  if (bytes === 0) return { value: "0", unit: "B" };
  const { value, unit } = scale(bytes);
  return { value: value.toFixed(2), unit };
}
