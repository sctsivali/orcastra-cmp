/**
 * LXD limit values as the console reads them: `limits.cpu` counts and sets, size strings, and
 * the project keys the create form compares a request against. Mirrors the backend's
 * cpu_allocation.py and project_usage.py so both ends count the same way.
 */

import { formatBytes } from "@/lib/utils";

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

/** The units a size field offers. The API takes any LXD spelling; these are what the console writes. */
export const FORM_SIZE_UNITS = ["MB", "GB", "TB"] as const;
export type FormSizeUnit = (typeof FORM_SIZE_UNITS)[number];
export const FORM_UNIT_BYTES: Record<FormSizeUnit, number> = { MB: 1e6, GB: 1e9, TB: 1e12 };

/**
 * An absolute LXD size as the edit control shows it.
 *
 * important: `preservedRaw` stays set until the operator changes the control, so opening and saving
 * a project cannot rewrite a limit stored as `8GiB` into a rounded `8.59GB`. A value the control
 * cannot show, such as a percentage, is preserved and identified rather than read as unlimited.
 */
export function editableLimit(raw: string | undefined): {
  value: number;
  unit: FormSizeUnit;
  preservedRaw: string | null;
} {
  const text = raw?.trim() ?? "";
  if (!text || text === "0") return { value: 0, unit: "GB", preservedRaw: null };
  const bytes = parseLxdSize(text);
  if (bytes === null || bytes <= 0) {
    return { value: 0, unit: "GB", preservedRaw: text };
  }
  const unit: FormSizeUnit = bytes >= FORM_UNIT_BYTES.TB ? "TB" : bytes >= FORM_UNIT_BYTES.GB ? "GB" : "MB";
  return { value: Number((bytes / FORM_UNIT_BYTES[unit]).toPrecision(12)), unit, preservedRaw: text };
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

/** A raw LXD size in the console's standard ("8GiB" reads "8.59 GB"); anything else as it came. */
export function formatLxdSize(raw: unknown): string {
  const bytes = parseLxdSize(raw);
  return bytes === null ? String(raw ?? "") : formatBytes(bytes);
}

// The config and device keys that hold a byte size. An allow-list, because a value elsewhere can
// look like one: a label "2", `limits.cpu` "4", a read limit "30iops", which parseLxdSize refuses.
const SIZE_KEYS = new Set([
  "limits.memory",
  "limits.disk",
  "limits.read",
  "limits.write",
  "limits.max",
  "size",
  "size.state",
  "volume.size",
]);

/** A config or device value for display. Only a size key is converted; the stored value is untouched. */
export function formatConfigValue(key: string, value: unknown): string {
  return SIZE_KEYS.has(key) ? formatLxdSize(value) : String(value);
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
