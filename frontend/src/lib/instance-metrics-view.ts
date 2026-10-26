import { formatLabel } from "@/lib/utils";
import type { InstanceMetrics } from "@/types/api";

/**
 * What one metrics sample says about CPU and memory, decided once for every surface that shows
 * it. The instance dialog and the console bar used to each read the raw fields their own way,
 * so the bar printed "0.0%" while the dialog said the second sample had not arrived.
 */
export type CpuReading =
  // `unlimited`: no limits.cpu, so `vcpus` and the percent are measured against the host
  | { state: "ready"; percent: number; busy: number | null; vcpus: number | null; unlimited: boolean }
  | { state: "calculating" }
  | { state: "unavailable"; busy: number | null };

export function cpuReading(metrics: InstanceMetrics): CpuReading {
  const unavailable = new Set(metrics.unavailable_sections);
  if (metrics.cpu_sample_status === "calculating") return { state: "calculating" };
  if (metrics.cpu_sample_status === "ready" && !unavailable.has("cpu")) {
    return {
      state: "ready",
      percent: Math.min(Math.max(metrics.cpu_usage_percent ?? 0, 0), 100),
      busy: metrics.cpu_busy_vcpus ?? null,
      vcpus: metrics.cpu_effective_vcpus ?? null,
      unlimited: metrics.cpu_limit_set === false,
    };
  }
  return { state: "unavailable", busy: metrics.cpu_busy_vcpus ?? null };
}

export interface MemoryReading {
  used: number;
  total: number;
  percent: number;
  available: number | null;
  cache: number | null;
  /** Used still counts page cache the guest can reclaim, so it overstates pressure. */
  includesCache: boolean;
  /** No limits.memory: `total` is the host's memory, not a ceiling this instance was given. */
  unlimited: boolean;
}

export function memoryReading(metrics: InstanceMetrics): MemoryReading | null {
  if (metrics.unavailable_sections.includes("memory")) return null;
  return {
    used: metrics.memory_used_bytes,
    total: metrics.memory_total_bytes,
    percent: Math.min(Math.max(metrics.memory_usage_percent ?? 0, 0), 100),
    available: metrics.memory_available_bytes ?? null,
    cache: metrics.memory_cache_bytes ?? null,
    includesCache: metrics.memory_includes_cache !== false,
    unlimited: metrics.memory_limit_set === false,
  };
}

/** "0.94", "1.5", "12": enough precision to read a fraction of a vCPU, no trailing zeros. */
export function formatVcpuAmount(value: number): string {
  return String(Number(value.toFixed(value < 10 ? 2 : 1)));
}

export function formatVcpuCount(count: number): string {
  return `${count} vCPU${count === 1 ? "" : "s"}`;
}

/** The window a rate covers, ex; "last 5 s". Null when the sample carries none. */
export function formatSampleWindow(seconds: number | null | undefined): string | null {
  if (!seconds || !Number.isFinite(seconds) || seconds <= 0) return null;
  return `last ${seconds < 10 ? Number(seconds.toFixed(1)) : Math.round(seconds)} s`;
}

/** Why disk I/O is missing, in the dialog's words. Null when the server gave no reason. */
export function diskIoUnavailableText(metrics: InstanceMetrics): string | null {
  switch (metrics.disk_io_reason) {
    case "storage_driver":
      return metrics.disk_io_storage_driver
        ? `Not measured for containers on ${formatLabel(metrics.disk_io_storage_driver)} storage`
        : "Not measured for containers on this storage";
    case "guest_agent":
      return "Needs the guest agent";
    case "not_reported":
      return "Not reported by the cluster";
    default:
      return null;
  }
}
