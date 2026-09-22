/**
 * LXD limit values as the console reads them: `limits.cpu` counts and sets, size strings, and
 * the project keys the create form compares a request against. Mirrors the backend's
 * cpu_allocation.py and project_usage.py so both ends count the same way.
 */

import { formatLxdLimit } from "@/lib/utils";

const SIZE_UNITS: Record<string, number> = {
  "": 1,
  b: 1,
  kb: 1e3,
  mb: 1e6,
  gb: 1e9,
  tb: 1e12,
  pb: 1e15,
  eb: 1e18,
  kib: 1024,
  mib: 1024 ** 2,
  gib: 1024 ** 3,
  tib: 1024 ** 4,
  pib: 1024 ** 5,
  eib: 1024 ** 6,
};

export type LxdLimitUnit = "GiB" | "MiB";
export type StorageLimitUnit = "GB" | "MB";

/**
 * Convert any absolute LXD size spelling into the edit control's binary units.
 * `preservedRaw` remains set until the operator changes the control, so a valid SI/raw-byte
 * limit is not rounded or cleared by an unrelated project edit. Unsupported values such as a
 * percentage are also preserved and identified instead of being misreported as unlimited.
 */
export function editableLxdLimit(raw: string | undefined): {
  value: number;
  unit: LxdLimitUnit;
  preservedRaw: string | null;
} {
  const text = raw?.trim() ?? "";
  if (!text || text === "0") return { value: 0, unit: "GiB", preservedRaw: null };
  const bytes = parseLxdSize(text);
  if (bytes === null || bytes <= 0) {
    return { value: 0, unit: "GiB", preservedRaw: text };
  }
  const unit: LxdLimitUnit = bytes >= 1024 ** 3 ? "GiB" : "MiB";
  const divisor = unit === "GiB" ? 1024 ** 3 : 1024 ** 2;
  return {
    value: Number((bytes / divisor).toPrecision(12)),
    unit,
    preservedRaw: text,
  };
}

/**
 * Convert an absolute LXD size into the project storage editor's decimal units.
 * The original spelling is preserved until the operator changes this field, matching
 * `editableLxdLimit` without presenting storage capacity as binary memory.
 */
export function editableStorageLimit(raw: string | undefined): {
  value: number;
  unit: StorageLimitUnit;
  preservedRaw: string | null;
} {
  const text = raw?.trim() ?? "";
  if (!text || text === "0") return { value: 0, unit: "GB", preservedRaw: null };
  const bytes = parseLxdSize(text);
  if (bytes === null || bytes <= 0) {
    return { value: 0, unit: "GB", preservedRaw: text };
  }
  const unit: StorageLimitUnit = bytes >= 1_000_000_000 ? "GB" : "MB";
  const divisor = unit === "GB" ? 1_000_000_000 : 1_000_000;
  return {
    value: Number((bytes / divisor).toPrecision(12)),
    unit,
    preservedRaw: text,
  };
}

/** Cores a `limits.cpu` value pins or counts; `fallback` for unset or unparseable. */
export function parseLimitsCpu(value: unknown, fallback = 1): number {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  if (!text) return fallback;
  if (/^\d+$/.test(text)) {
    const count = parseInt(text, 10);
    return count > 0 ? count : fallback;
  }
  const ids = new Set<number>();
  for (const part of text.split(",")) {
    const piece = part.trim();
    if (!piece) continue;
    if (/^\d+$/.test(piece)) {
      ids.add(parseInt(piece, 10));
      continue;
    }
    const m = /^(\d+)-(\d+)$/.exec(piece);
    if (!m) return fallback;
    const lo = parseInt(m[1], 10);
    const hi = parseInt(m[2], 10);
    if (hi < lo) return fallback;
    for (let i = lo; i <= hi; i += 1) ids.add(i);
  }
  return ids.size || fallback;
}

/** Bytes for an LXD size string ("8GiB", "512MB", "1024"); null for a percentage or junk. */
export function parseLxdSize(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  const m = /^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$/.exec(text);
  if (!m) return null;
  const unit = SIZE_UNITS[m[2].toLowerCase()];
  if (unit === undefined) return null;
  return Math.round(parseFloat(m[1]) * unit);
}

/** A byte count in the unit an operator types, ex; "8 GiB", "512 MiB". */
export function formatLimitBytes(bytes: number): string {
  return formatLxdLimit(bytes, { decimals: 1 });
}

export interface ProjectLimits {
  cpu?: number;
  memoryBytes?: number;
  instances?: number;
  containers?: number;
  virtualMachines?: number;
}

function intOrUndefined(value: string | undefined): number | undefined {
  if (value === undefined || value.trim() === "") return undefined;
  const n = parseInt(value, 10);
  return Number.isFinite(n) ? n : undefined;
}

/** The limits a project's config sets; a key is absent when the project does not set it. */
export function readProjectLimits(config: Record<string, string> | null | undefined): ProjectLimits {
  const cfg = config ?? {};
  const memory = parseLxdSize(cfg["limits.memory"]);
  return {
    cpu: intOrUndefined(cfg["limits.cpu"]),
    memoryBytes: memory ?? undefined,
    instances: intOrUndefined(cfg["limits.instances"]),
    containers: intOrUndefined(cfg["limits.containers"]),
    virtualMachines: intOrUndefined(cfg["limits.virtual-machines"]),
  };
}

export function projectSetsLimits(limits: ProjectLimits): boolean {
  return Object.values(limits).some((v) => v !== undefined);
}

/** Instances a project's `used_by` names. */
export function instanceCountFromUsedBy(usedBy: string[] | undefined): number {
  return (usedBy ?? []).filter((u) => u.startsWith("/1.0/instances/")).length;
}
