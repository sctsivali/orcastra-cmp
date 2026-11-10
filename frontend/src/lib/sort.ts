// Shared client-side table sorting primitives.
// All dashboard tables fetch full datasets and sort in the browser, so a single
// type-aware comparator backs every sortable column.

export type SortDirection = "asc" | "desc";

// Severity rank for enum columns (warnings). Lower ranks sort first ascending.
export const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  error: 0,
  high: 1,
  warning: 2,
  medium: 2,
  moderate: 2,
  low: 3,
  info: 3,
};

// Parse a dotted-quad IPv4 string into a comparable integer. Returns null for
// anything that is not a full 4-octet address so non-IP strings fall back to text.
export function ipToNumber(value: string): number | null {
  const parts = value.trim().split(".");
  if (parts.length !== 4) return null;
  let result = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    result = result * 256 + octet;
  }
  return result;
}

function isEmpty(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}

// Type-aware comparator. Empty values always sort last regardless of direction.
// Dates must be normalized to a number (getTime()) by the column accessor. Strings
// are never sniffed as dates, which would mis-parse enum values like "tls".
export function compareValues(a: unknown, b: unknown, direction: SortDirection): number {
  const aEmpty = isEmpty(a);
  const bEmpty = isEmpty(b);
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1; // Empties last in either direction.
  if (bEmpty) return -1;

  const sign = direction === "asc" ? 1 : -1;

  // Arrays compare by length (count of items).
  if (Array.isArray(a) && Array.isArray(b)) {
    return (a.length - b.length) * sign;
  }

  if (typeof a === "number" && typeof b === "number") {
    return (a - b) * sign;
  }

  if (typeof a === "string" && typeof b === "string") {
    const aIp = ipToNumber(a);
    const bIp = ipToNumber(b);
    if (aIp !== null && bIp !== null) {
      return (aIp - bIp) * sign;
    }
    return a.localeCompare(b, undefined, { sensitivity: "base", numeric: true }) * sign;
  }

  return String(a).localeCompare(String(b), undefined, { sensitivity: "base", numeric: true }) * sign;
}
