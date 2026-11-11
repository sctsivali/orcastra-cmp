/** Compatibility wrappers for existing member-resource imports. */
import type { ByteUnitSystem } from "@/lib/byte-units";
import { formatBytes as formatStorageBytes, type FormattedByteValue } from "@/lib/utils";

export function formatBytes(bytes: number, units: ByteUnitSystem): string {
  return formatStorageBytes(bytes, { decimals: 2, units });
}

export function formatBytesCompact(bytes: number, units: ByteUnitSystem): FormattedByteValue {
  return formatStorageBytes(bytes, { decimals: 2, split: true, units });
}
