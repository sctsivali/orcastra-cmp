import {
  FORM_SIZE_UNITS,
  defaultFormUnit,
  toByteUnitSystem,
  type ByteUnitSystem,
  type FormSizeUnit,
} from "@/lib/byte-units";
import { editableLimit, formatConfigValue, formatLxdSize } from "@/lib/lxd-limits";
import {
  formatByteQuota,
  formatBytes,
  formatNetworkRate,
  type ByteFormatOptions,
  type FormattedByteQuota,
  type FormattedByteValue,
  type SplitByteFormatOptions,
} from "@/lib/utils";

type BoundOptions = Omit<ByteFormatOptions, "units">;

type BoundFormatBytes = {
  (bytes: number, options: Omit<SplitByteFormatOptions, "units">): FormattedByteValue;
  (bytes: number, options?: number | BoundOptions): string;
};

/** Every byte formatter, bound to one unit system. What useByteFormat() hands a component. */
export interface ByteFormat {
  units: ByteUnitSystem;
  formatBytes: BoundFormatBytes;
  formatByteQuota: (usedBytes: number, limitBytes: number, decimals?: number) => FormattedByteQuota;
  formatNetworkRate: (bytesPerSecond: number, decimals?: number) => string;
  formatLxdSize: (raw: unknown) => string;
  formatConfigValue: (key: string, value: unknown) => string;
  editableLimit: (raw: string | undefined) => ReturnType<typeof editableLimit>;
  /** The units a size field offers in this system, smallest first. */
  formUnits: readonly FormSizeUnit[];
  /** The unit a new size field starts in: GB or GiB. */
  defaultFormUnit: FormSizeUnit;
}

function bind(units: ByteUnitSystem): ByteFormat {
  const boundBytes = ((bytes: number, options: number | BoundOptions = {}) =>
    formatBytes(bytes, typeof options === "number" ? { decimals: options, units } : { ...options, units })) as BoundFormatBytes;
  return {
    units,
    formatBytes: boundBytes,
    formatByteQuota: (usedBytes, limitBytes, decimals = 1) => formatByteQuota(usedBytes, limitBytes, decimals, units),
    formatNetworkRate: (bytesPerSecond, decimals = 2) => formatNetworkRate(bytesPerSecond, decimals, units),
    formatLxdSize: (raw) => formatLxdSize(raw, units),
    formatConfigValue: (key, value) => formatConfigValue(key, value, units),
    editableLimit: (raw) => editableLimit(raw, units),
    formUnits: FORM_SIZE_UNITS[units],
    defaultFormUnit: defaultFormUnit(units),
  };
}

// One object per system, so a component can list the formatters as effect or memo dependencies
// and they only change when the account's system does.
const BOUND: Record<ByteUnitSystem, ByteFormat> = {
  decimal: bind("decimal"),
  binary: bind("binary"),
};

export function byteFormat(units: unknown): ByteFormat {
  return BOUND[toByteUnitSystem(units)];
}
