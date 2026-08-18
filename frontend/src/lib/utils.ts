import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatBytes(bytes: number, decimals = 2): string {
  if (bytes === 0) return "0 Bytes";

  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["Bytes", "KB", "MB", "GB", "TB", "PB"];

  const i = Math.floor(Math.log(bytes) / Math.log(k));

  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
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

export function truncateFingerprint(fingerprint: string, length = 12): string {
  if (fingerprint.length <= length) return fingerprint;
  return fingerprint.substring(0, length) + "...";
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
