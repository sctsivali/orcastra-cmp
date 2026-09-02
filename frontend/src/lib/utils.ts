import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// note: the placeholder glyph, kept in step with components/ui/table-text.tsx. Importing
// it from there would point lib/ at components/, which nothing else in lib/ does.
const EMPTY_VALUE = "\u2014";

export function formatBytes(bytes: number, decimals = 2): string {
  if (bytes === 0) return "0 Bytes";

  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["Bytes", "KB", "MB", "GB", "TB", "PB"];

  const i = Math.floor(Math.log(bytes) / Math.log(k));

  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
}

/**
 * CPU or link frequency, given in MHz.
 *
 * Lives here rather than in a component because formatBytes taught this codebase the lesson
 * the expensive way: thirteen private copies exist and they disagree on whether 8 GiB reads
 * as "8 GB" or "8.0 GB".
 */
export function formatFrequency(mhz: number | null | undefined): string {
  if (mhz === null || mhz === undefined || mhz <= 0) return EMPTY_VALUE;
  if (mhz < 1000) return `${Math.round(mhz)} MHz`;
  return `${parseFloat((mhz / 1000).toFixed(2))} GHz`;
}

/** Link speed, given in Mbit/s. Same reasoning as formatFrequency. */
export function formatLinkSpeed(mbit: number | null | undefined): string {
  if (mbit === null || mbit === undefined || mbit <= 0) return EMPTY_VALUE;
  if (mbit < 1000) return `${mbit} Mb/s`;
  return `${parseFloat((mbit / 1000).toFixed(1))} Gb/s`;
}

export function formatDate(dateString: string | null | undefined): string {
  if (!dateString) return "N/A";
  
  try {
    const date = new Date(dateString);
    return new Intl.DateTimeFormat("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  } catch {
    return "N/A";
  }
}

// Format datetime to DD/MM/YYYY, HH:MM:SS
export function formatDateTime(dateString: string | null | undefined): string {
  if (!dateString) return "N/A";
  try {
    const date = new Date(dateString);
    if (isNaN(date.getTime())) return "N/A";
    const day = String(date.getDate()).padStart(2, '0');
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const year = date.getFullYear();
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const seconds = String(date.getSeconds()).padStart(2, '0');
    return `${day}/${month}/${year}, ${hours}:${minutes}:${seconds}`;
  } catch {
    return "N/A";
  }
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
// lib/regions-data.ts, which used to carry its own copy of the same table.
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

// Wall-clock time only (HH:MM:SS, 24h), for the precise value behind a
// relative label. Replaces four hand-rolled copies of this formatter.
export function formatClockTime(timestamp: number | null | undefined): string {
  if (!timestamp || timestamp <= 0) return "N/A";
  try {
    const date = new Date(timestamp);
    if (isNaN(date.getTime())) return "N/A";
    return date.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return "N/A";
  }
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
  spice: "SPICE",
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
  if (value === null || value === undefined || value === "") return "—";
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
export function formatInstanceType(type: string | null | undefined): string {
  if (!type) return "Unknown";
  const t = type.trim().toLowerCase();
  if (t === "virtual-machine" || t === "virtualmachine" || t === "vm") return "Virtual Machine";
  if (t === "container" || t === "ct") return "Container";
  return formatLabel(type);
}
