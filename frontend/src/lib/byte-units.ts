/**
 * The two byte unit systems a person can read the console in, and the units a size field offers in
 * each. Binary is the default: 1 GiB is 1,073,741,824 bytes, as LXD and free -h count. Decimal:
 * 1 GB is 1,000,000,000 bytes.
 *
 * important: this is the only display module allowed to name a binary unit
 * (scripts/checks/check-byte-units.mjs). Every screen reads one system at a time, the one the
 * account chose, so a disk never reads GiB in a column and GB on its meter.
 */

export type ByteUnitSystem = "decimal" | "binary";

// The default first: Settings lists them in this order.
export const BYTE_UNIT_SYSTEMS: readonly ByteUnitSystem[] = ["binary", "decimal"];

export const DEFAULT_BYTE_UNITS: ByteUnitSystem = "binary";

export function isByteUnitSystem(value: unknown): value is ByteUnitSystem {
  return value === "decimal" || value === "binary";
}

/** A stored or received value as a system; anything unrecognised reads as the default. */
export function toByteUnitSystem(value: unknown): ByteUnitSystem {
  return isByteUnitSystem(value) ? value : DEFAULT_BYTE_UNITS;
}

export interface ByteScale {
  base: number;
  units: readonly string[];
}

export const BYTE_SCALES: Record<ByteUnitSystem, ByteScale> = {
  decimal: { base: 1000, units: ["B", "kB", "MB", "GB", "TB", "PB"] },
  binary: { base: 1024, units: ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] },
};

/** The units a size field offers. LXD accepts every one of them as written. */
export const FORM_SIZE_UNITS = {
  decimal: ["MB", "GB", "TB"],
  binary: ["MiB", "GiB", "TiB"],
} as const satisfies Record<ByteUnitSystem, readonly string[]>;

export type FormSizeUnit = (typeof FORM_SIZE_UNITS)[ByteUnitSystem][number];

export const FORM_UNIT_BYTES: Record<FormSizeUnit, number> = {
  MB: 1e6,
  GB: 1e9,
  TB: 1e12,
  MiB: 1024 ** 2,
  GiB: 1024 ** 3,
  TiB: 1024 ** 4,
};

/** The field unit a new size starts in: GB or GiB. */
export function defaultFormUnit(units: ByteUnitSystem): FormSizeUnit {
  return FORM_SIZE_UNITS[units][1];
}

export function isFormSizeUnit(value: unknown): value is FormSizeUnit {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(FORM_UNIT_BYTES, value);
}

/** The system a field unit belongs to, so an open form keeps offering the units it started with. */
export function formUnitSystem(unit: string): ByteUnitSystem {
  return (FORM_SIZE_UNITS.binary as readonly string[]).includes(unit) ? "binary" : "decimal";
}

/** The largest field unit of a system that keeps a value at 1 or more. */
export function formUnitForBytes(bytes: number, units: ByteUnitSystem): FormSizeUnit {
  const [small, middle, large] = FORM_SIZE_UNITS[units];
  if (bytes >= FORM_UNIT_BYTES[large]) return large;
  if (bytes >= FORM_UNIT_BYTES[middle]) return middle;
  return small;
}
