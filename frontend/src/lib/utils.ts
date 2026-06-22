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

// Words that should keep a fixed casing instead of plain title-case (acronyms, brands).
const LABEL_ACRONYMS: Record<string, string> = {
  zfs: "ZFS",
  btrfs: "Btrfs",
  ceph: "Ceph",
  lvm: "LVM",
  ovn: "OVN",
  ovs: "OVS",
  oidc: "OIDC",
  tls: "TLS",
  ssh: "SSH",
  api: "API",
  ip: "IP",
  ipam: "IPAM",
  url: "URL",
  vm: "VM",
  id: "ID",
  cpu: "CPU",
  gpu: "GPU",
  os: "OS",
};

// Title-case a raw enum/label ("virtual-machine" -> "Virtual Machine", "zfs" -> "ZFS").
// Splits on -, _ and whitespace; applies the acronym map per word. Null-safe.
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

// Instance/VM type display: "container" -> "Container", "virtual-machine" -> "Virtual Machine".
export function formatInstanceType(type: string | null | undefined): string {
  if (!type) return "Unknown";
  const t = type.trim().toLowerCase();
  if (t === "virtual-machine" || t === "virtualmachine" || t === "vm") return "Virtual Machine";
  if (t === "container" || t === "ct") return "Container";
  return formatLabel(type);
}
