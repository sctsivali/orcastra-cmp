import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// note: the placeholder glyph, kept in step with components/ui/table-text.tsx. Importing
// it from there would point lib/ at components/, which nothing else in lib/ does.
export const EMPTY_VALUE = "\u2014";

const SI_BYTE_UNITS = ["B", "kB", "MB", "GB", "TB", "PB"] as const;

export type FormattedByteValue = { value: string; unit: string };
export type FormattedByteQuota = { value: string; unit: string };

export interface ByteFormatOptions {
  /** Digits after the decimal point before insignificant trailing zeroes are removed. */
  decimals?: number;
  /** Whether a valid zero is explicit or rendered as the shared empty value. */
  zeroDisplay?: "zero" | "empty";
  /** Return value and unit separately for dense cards that style them independently. */
  split?: boolean;
}

type SplitByteFormatOptions = ByteFormatOptions & { split: true };

function scaleBytes(
  bytes: number,
  base: number,
  units: readonly string[],
  options: number | ByteFormatOptions = {},
): FormattedByteValue {
  const normalized = typeof options === "number" ? { decimals: options } : options;
  const decimals = byteDecimals(normalized.decimals ?? 2);
  if (!Number.isFinite(bytes) || bytes < 0) return { value: EMPTY_VALUE, unit: "" };
  if (bytes === 0) {
    return normalized.zeroDisplay === "empty"
      ? { value: EMPTY_VALUE, unit: "" }
      : { value: "0", unit: units[0] };
  }

  const index = Math.min(
    Math.max(0, Math.floor(Math.log(bytes) / Math.log(base))),
    units.length - 1,
  );
  return {
    value: String(parseFloat((bytes / Math.pow(base, index)).toFixed(decimals))),
    unit: units[index],
  };
}

function byteDecimals(decimals: number): number {
  return Number.isFinite(decimals) ? Math.min(20, Math.max(0, Math.floor(decimals))) : 2;
}

function scaleByteQuota(
  usedBytes: number,
  limitBytes: number,
  base: number,
  units: readonly string[],
  decimals = 1,
): FormattedByteQuota {
  const limit = scaleBytes(limitBytes, base, units, { decimals, split: true });
  if (!limit.unit) return { value: EMPTY_VALUE, unit: "" };
  const index = Math.max(0, units.indexOf(limit.unit));
  const divisor = Math.pow(base, index);
  const usedValue = Number.isFinite(usedBytes) && usedBytes >= 0
    ? String(parseFloat((usedBytes / divisor).toFixed(byteDecimals(decimals))))
    : EMPTY_VALUE;

  return {
    value: `${usedValue} of ${limit.value}`,
    unit: limit.unit,
  };
}

function joinByteValue(value: FormattedByteValue): string {
  return value.unit ? `${value.value} ${value.unit}` : value.value;
}

/**
 * Every size in the console: memory, disks, pools, images, files and transfers.
 *
 * important: decimal, and labelled decimal, on every screen. One disk used to read 10GiB in a
 * column and 10.74 GB on its usage meter, because memory and limits were binary and storage was
 * decimal. documentation/ui/byte-units.md has the decision and what it costs.
 */
export function formatBytes(bytes: number, options: SplitByteFormatOptions): FormattedByteValue;
export function formatBytes(bytes: number, options?: number | ByteFormatOptions): string;
export function formatBytes(
  bytes: number,
  options: number | ByteFormatOptions = {},
): string | FormattedByteValue {
  const formatted = scaleBytes(bytes, 1000, SI_BYTE_UNITS, options);
  return typeof options === "object" && options.split ? formatted : joinByteValue(formatted);
}

/** Usage and its limit on one scale, for compact quota cards. */
export function formatByteQuota(
  usedBytes: number,
  limitBytes: number,
  decimals = 1,
): FormattedByteQuota {
  return scaleByteQuota(usedBytes, limitBytes, 1000, SI_BYTE_UNITS, decimals);
}

/** Network throughput in bytes per second, scaled with decimal SI units. */
export function formatNetworkRate(bytesPerSecond: number, decimals = 2): string {
  const formatted = scaleBytes(bytesPerSecond, 1000, SI_BYTE_UNITS, { decimals });
  return formatted.unit ? `${formatted.value} ${formatted.unit}/s` : formatted.value;
}

/**
 * A span of CPU time, from LXD's cumulative nanosecond counter.
 *
 * important: zero is a real reading, not an absent one, so it formats as "0s" rather than the
 * placeholder. An instance whose guest reports nothing genuinely has no CPU time to show, and
 * that is the signal worth seeing. This is why formatUptime in the metrics tab is not reused:
 * it returns undefined for 0 and floors at whole minutes, so 45 seconds of CPU time would read
 * "0m" and a genuine zero would vanish behind the missing-value dash.
 */
export function formatCpuTime(nanoseconds: number | null | undefined): string {
  if (nanoseconds === null || nanoseconds === undefined) return EMPTY_VALUE;
  if (!Number.isFinite(nanoseconds) || nanoseconds < 0) return EMPTY_VALUE;

  const seconds = Math.floor(nanoseconds / 1e9);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

/**
 * CPU or link frequency, given in MHz.
 *
 * Lives here rather than in a component because formatBytes taught this codebase the lesson
 * the expensive way: thirteen private copies exist and they disagree on whether 8 GiB reads
 * as "8 GB" or "8.0 GB".
 */
export function formatFrequency(mhz: number | null | undefined): string {
  if (mhz === null || mhz === undefined || !Number.isFinite(mhz) || mhz <= 0) return EMPTY_VALUE;
  if (mhz < 1000) return `${Math.round(mhz)} MHz`;
  return `${parseFloat((mhz / 1000).toFixed(2))} GHz`;
}

/** Link speed, given in Mbit/s. Same reasoning as formatFrequency. */
export function formatLinkSpeed(mbit: number | null | undefined): string {
  if (mbit === null || mbit === undefined || !Number.isFinite(mbit) || mbit <= 0) return EMPTY_VALUE;
  if (mbit < 1000) return `${mbit} Mb/s`;
  return `${parseFloat((mbit / 1000).toFixed(1))} Gb/s`;
}

// An API timestamp with no offset is UTC, and new Date() would read it as local. That was a small
// error before the zone label; with one it is a wrong time stated with confidence.
const OFFSETLESS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(OFFSETLESS.test(value) ? `${value}Z` : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

// en-US names Indonesia's zones GMT+7, GMT+8 and GMT+9, which no operator here says.
const ZONE_NAMES: Record<string, string> = {
  "Asia/Jakarta": "WIB",
  "Asia/Pontianak": "WIB",
  "Asia/Makassar": "WITA",
  "Asia/Jayapura": "WIT",
};

function zoneLabel(date: Date): string {
  const named = ZONE_NAMES[Intl.DateTimeFormat().resolvedOptions().timeZone];
  if (named) return named;
  const parts = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(date);
  return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
}

function dateAndTime(value: string | null | undefined, withSeconds: boolean): string {
  const date = parseTimestamp(value);
  if (!date) return EMPTY_VALUE;
  const text = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
  }).format(date);
  return `${text} ${zoneLabel(date)}`;
}

/** "Oct 30, 2026", for a day rather than a moment, so it carries no zone. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return EMPTY_VALUE;
  // important: a bare date is that day everywhere. Read as UTC midnight and shown in a zone west of
  // Greenwich it would print the day before.
  const date = DATE_ONLY.test(value) ? new Date(`${value}T00:00:00Z`) : parseTimestamp(value);
  if (!date || Number.isNaN(date.getTime())) return EMPTY_VALUE;
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(DATE_ONLY.test(value) ? { timeZone: "UTC" } : {}),
  }).format(date);
}

/** "Oct 30, 2026, 10:55 PM WIB": the one format for a moment, in the operator's own time. */
export function formatDateTime(value: string | null | undefined): string {
  return dateAndTime(value, false);
}

/** "Oct 30, 2026, 10:55:04 PM WIB", where the order of events matters: logs, tasks, sessions. */
export function formatDateTimeSeconds(value: string | null | undefined): string {
  return dateAndTime(value, true);
}

// Generate a UUID that also works in non-secure contexts. `crypto.randomUUID`
// only exists over HTTPS or on localhost, so on a plain-HTTP LAN deployment
// (e.g. http://10.0.1.5) it is undefined and calling it throws
// "crypto.randomUUID is not a function". These IDs are only used as local React
// keys for form rows, so a non-cryptographic fallback is fine.
export function safeRandomUUID(): string {
  const c = typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  // RFC 4122 v4 shape, filled from getRandomValues when available (present over
  // plain HTTP too) and Math.random only as a last resort.
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") {
    c.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex
    .slice(6, 8)
    .join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10, 16).join("")}`;
}

// The `--name` an operator pastes into `lxc config trust add`, identifying the certificate on
// the hypervisor's trust list.
//
// important: this is generated in the browser and never sent to the backend. Node registration
// labels the certificate this way, and the string has to stay stable because it is what an
// operator matches against when auditing or removing trust later. The random suffix only breaks
// ties between two registrations inside the same millisecond, so a non-cryptographic source is
// the right one; nothing authenticates on this value.
//
// note: renew and regenerate do NOT use this. The backend builds their identity in this same
// shape (`trust_identity_name` in services/lxd_certs/rotation.py) and returns the whole command
// ready to paste, so every entry in a trust list reads alike.
export function generateIdentityName(clusterId: string): string {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  const hours = String(now.getHours()).padStart(2, "0");
  const minutes = String(now.getMinutes()).padStart(2, "0");
  const seconds = String(now.getSeconds()).padStart(2, "0");
  const millis = String(now.getMilliseconds()).padStart(3, "0");
  const randomSuffix = Math.random().toString(36).substring(2, 6);
  const datetime = `${year}${month}${day}-${hours}${minutes}${seconds}${millis}-${randomSuffix}`;
  return `orca-${clusterId}-${datetime}`;
}

// note: after trust is added on the host, nothing proves it until a check runs. The backend keeps a
// refused cluster tripped for 60 s per worker and the healer passes every 30 s, so on its own the
// cluster can stay Pending Trust for a minute or more. Check Authentication or Verify proves it at
// once and releases every worker within seconds.
export function trustPickupHint(action: string): string {
  return `Once the command has run, press ${action}. On its own, the dashboard can take a minute or more to notice the new trust.`;
}

// standardize known hypervisor type display while preserving future providers
export function formatHypervisorType(type: string | null | undefined): string {
  if (!type) return "Hypervisor";
  const trimmed = type.trim();
  const t = trimmed.toLowerCase();
  if (t === "incus") return "Incus";
  if (t === "microcloud") return "MicroCloud";
  if (t === "lxd") return "LXD";
  return trimmed || "Hypervisor";
}

// The seven continent codes the region catalogue uses. Also the single source of truth for
// the region catalogue (lib/regions/catalog.json), whose entries carry these codes.
export const CONTINENT_NAMES: Record<string, string> = {
  AF: "Africa",
  AN: "Antarctica",
  AS: "Asia",
  EU: "Europe",
  NA: "North America",
  OC: "Oceania",
  SA: "South America",
};

// important: the API stores and filters on the code, so nothing here may reach a request or a
// URL. A code is also actively misleading on screen: an operator reading the console in
// Indonesian reads "AS" (Asia) as "Amerika Serikat", and formatLabel turns it into "As".
//
// note: returns undefined rather than a placeholder string for a missing or unknown-sentinel
// value, so the caller's own empty rendering applies. "XX" is what the backend writes for a
// cluster with no recorded location (routers/regions.py).
export function formatContinent(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  const code = trimmed.toUpperCase();
  if (!code || code === "XX") return undefined;
  return CONTINENT_NAMES[code] ?? trimmed;
}

/**
 * A server collection time (ISO string) as epoch milliseconds, or 0 when absent or unreadable.
 *
 * Lists served from the backend cache carry the time their read finished (`collected_at`). A
 * "last updated" label must show that, not when the browser received the body, or a cached
 * answer reads as current. Callers fall back to React Query's dataUpdatedAt on 0.
 */
export function collectedAtMs(collectedAt: string | null | undefined): number {
  const parsed = collectedAt ? Date.parse(collectedAt) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

// Relative age of a timestamp, for "last updated" labels in dense toolbars.
// Kept local rather than pulling in date-fns' formatDistanceToNow, whose
// "less than a minute ago" phrasing is too long for a table header.
export function formatRelativeTime(timestamp: number | null | undefined): string {
  if (!timestamp || timestamp <= 0) return "never";

  const diffMs = Date.now() - timestamp;
  // a clock skew between server and browser can push this negative
  if (diffMs < 5_000) return "just now";

  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return `${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  return `${Math.floor(hours / 24)}d ago`;
}

// Wall-clock time only ("10:55:04 PM"), for the precise value behind a relative label.
export function formatClockTime(timestamp: number | null | undefined): string {
  if (!timestamp || timestamp <= 0) return EMPTY_VALUE;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return EMPTY_VALUE;
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

// Words that keep a fixed casing instead of plain title-case (acronyms, brands, and the
// storage/network driver names LXD reports in lowercase).
const LABEL_ACRONYMS: Record<string, string> = {
  zfs: "ZFS",
  btrfs: "Btrfs",
  ceph: "Ceph",
  cephfs: "CephFS",
  lvm: "LVM",
  ovn: "OVN",
  ovs: "OVS",
  sriov: "SR-IOV",
  oidc: "OIDC",
  tls: "TLS",
  ssh: "SSH",
  api: "API",
  acl: "ACL",
  dns: "DNS",
  ip: "IP",
  ipam: "IPAM",
  url: "URL",
  iso: "ISO",
  vm: "VM",
  ct: "CT",
  id: "ID",
  cpu: "CPU",
  gpu: "GPU",
  nic: "NIC",
  pci: "PCI",
  usb: "USB",
  tpm: "TPM",
  vga: "VGA",
  os: "OS",
};

// Title-case a raw enum for display ("virtual-machine" -> "Virtual Machine", "zfs" -> "ZFS").
// Splits on -, _ and whitespace, then applies the acronym map per word. Null-safe.
//
// important: only for values that arrive lowercase from an API. Never pass a name a user or
// a registry authored (instance, profile, project, image alias, cluster) through this, and
// never pass `architecture`: "x86_64" comes back as "X86 64".
export function formatLabel(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_VALUE;
  return String(value)
    .trim()
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((word) => {
      const lower = word.toLowerCase();
      if (LABEL_ACRONYMS[lower]) return LABEL_ACRONYMS[lower];
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(" ");
}

// Instance type display. One vocabulary for an enum the app previously spelled four ways
// ("VM"/"Container", "VM"/"CT", "Container"/"VM", and the raw "virtual-machine").
//
// important: instance type always goes through this, never formatLabel, because
// formatLabel("vm") returns "VM" via the acronym map and that is the fifth vocabulary.
/** Lower-cases a leading word for use mid-sentence, leaving an initialism such as "CLI command" alone. */
export function lowerFirst(text: string): string {
  return /^[A-Z]{2}/.test(text) ? text : text.charAt(0).toLowerCase() + text.slice(1);
}

export function formatInstanceType(type: string | null | undefined): string {
  if (!type) return "Unknown";
  const t = type.trim().toLowerCase();
  if (t === "virtual-machine" || t === "virtualmachine" || t === "vm") return "Virtual Machine";
  if (t === "container" || t === "ct") return "Container";
  return formatLabel(type);
}

/**
 * An ISO timestamp as epoch milliseconds, or null when there is nothing to read.
 *
 * important: an unparseable or absent stamp must stay null. LiveTimestamp renders null as
 * "Age unknown", and coercing it to 0 or to Date.now() would report a body of unknown age as
 * current, which is the one thing that component exists to prevent.
 */
export function toMillis(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/** "1 cluster", "3 clusters". Name the plural when it is not an added s: plural(2, "entry", "entries"). */
export function plural(count: number, word: string, pluralWord = `${word}s`): string {
  return `${count} ${count === 1 ? word : pluralWord}`;
}

/** "clusters" to "cluster", "entries" to "entry", for a noun prop that names the plural. Not for "aliases". */
export function singular(word: string): string {
  if (word.endsWith("ies")) return `${word.slice(0, -3)}y`;
  if (word.endsWith("s")) return word.slice(0, -1);
  return word;
}

/** "1 vCPU", "4 vCPUs", "0.94 vCPUs". Used as a modifier it stays singular: "the 4 vCPU limit". */
export function formatVcpuCount(count: number): string {
  return plural(count, "vCPU");
}

/** "1,234": one grouping everywhere, not whatever the browser's locale happens to use. */
export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}
