/** Compatibility wrappers for existing member-resource imports. */
import {
  formatBytes as formatStorageBytes,
  formatMemory,
  type FormattedByteValue,
} from "@/lib/utils";

export function formatBytes(bytes: number): string {
  return formatStorageBytes(bytes, { decimals: 2 });
}

export function formatBytesCompact(bytes: number): FormattedByteValue {
  return formatStorageBytes(bytes, { decimals: 2, split: true });
}

export function formatMemoryCompact(bytes: number): FormattedByteValue {
  return formatMemory(bytes, { decimals: 2, split: true });
}
