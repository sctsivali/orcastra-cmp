import type { SystemResourceUsage } from "@/types";

/**
 * Readings derived from two consecutive answers of GET /system/resource-usage.
 *
 * Every counter used here is machine-wide or container-wide, so the delta between two answers
 * is honest whichever uvicorn worker produced each one. The server's own CPU percents are not:
 * each worker keeps its own previous sample, so with four workers a poll can land on one that
 * last answered an hour ago and report an hour-long average as "now". The browser measures its
 * own poll-to-poll window instead, and falls back to the server figure only when that figure's
 * window is short enough to mean the same thing.
 */

/** Longest gap still read as "now". A tab that was hidden for minutes starts measuring again. */
export const MAX_WINDOW_SECONDS = 30;
const MIN_WINDOW_SECONDS = 0.5;

export interface WindowedReading {
  percent: number;
  windowSeconds: number;
}

export interface ThrottleReading {
  count: number;
  windowSeconds: number;
}

/** Seconds between two server timestamps, or null when too short or too long to mean anything. */
export function sampleWindow(prevAt: string | undefined, currAt: string): number | null {
  if (!prevAt) return null;
  const seconds = (Date.parse(currAt) - Date.parse(prevAt)) / 1000;
  if (!Number.isFinite(seconds) || seconds < MIN_WINDOW_SECONDS || seconds > MAX_WINDOW_SECONDS) return null;
  return seconds;
}

/**
 * Bytes per second between two samples, from the server's own timestamps.
 *
 * The panel used to divide a counter delta by a hard-coded five seconds, which was only right
 * while the poll was exactly five seconds apart: a manual refresh a second after a poll
 * reported a rate five times too small.
 */
export function ratePerSecond(
  prev: number | null | undefined,
  curr: number,
  prevAt: string | undefined,
  currAt: string,
): number | null {
  if (prev === null || prev === undefined) return null;
  const seconds = sampleWindow(prevAt, currAt);
  if (seconds === null) return null;
  const delta = curr - prev;
  // whole bytes: formatBytes picks its unit from log1024 and has none for a fraction of a byte
  return delta > 0 ? Math.floor(delta / seconds) : 0;
}

function clampPercent(value: number): number {
  return Math.round(Math.min(100, Math.max(0, value)) * 10) / 10;
}

/** A server percent is only shown when it covers a window as short as the panel's own. */
function serverReading(percent: number | null | undefined, windowSeconds: number | null | undefined): WindowedReading | null {
  if (percent === null || percent === undefined) return null;
  if (windowSeconds === null || windowSeconds === undefined || windowSeconds > MAX_WINDOW_SECONDS) return null;
  return { percent, windowSeconds };
}

/** Host busy share: delta of busy CPU seconds over delta of all CPU seconds. */
export function hostCpuReading(prev: SystemResourceUsage | null, curr: SystemResourceUsage): WindowedReading | null {
  const a = prev?.host?.cpu_times;
  const b = curr.host?.cpu_times;
  const window = sampleWindow(prev?.timestamp, curr.timestamp);
  if (a && b && window !== null) {
    const busy = b.busy_seconds - a.busy_seconds;
    const total = b.total_seconds - a.total_seconds;
    // a counter that went backwards means the machine rebooted between the two answers
    if (busy >= 0 && total > 0) return { percent: clampPercent((busy / total) * 100), windowSeconds: window };
  }
  return serverReading(curr.host?.cpu.percent, curr.host?.cpu_window_seconds);
}

/**
 * Container CPU as a share of what it may use: the quota, or every host CPU when unlimited.
 * 100% means the container is at its cap.
 */
export function containerCpuReading(prev: SystemResourceUsage | null, curr: SystemResourceUsage): WindowedReading | null {
  const cpu = curr.container?.cpu;
  const capacity = cpu?.limit ?? curr.host?.cpu.count_logical ?? null;
  const a = prev?.container?.cpu?.usage_usec;
  const b = cpu?.usage_usec;
  const window = sampleWindow(prev?.timestamp, curr.timestamp);
  if (a !== null && a !== undefined && b !== null && b !== undefined && window !== null && capacity) {
    const used = b - a;
    // a counter that went backwards means the container restarted between the two answers
    if (used >= 0) return { percent: clampPercent((used / (window * 1_000_000 * capacity)) * 100), windowSeconds: window };
  }
  return serverReading(cpu?.percent, cpu?.window_seconds);
}

/** Throttled CFS periods between two answers: whether the container is at its CPU cap right now. */
export function throttleReading(prev: SystemResourceUsage | null, curr: SystemResourceUsage): ThrottleReading | null {
  const a = prev?.container?.cpu?.nr_throttled;
  const b = curr.container?.cpu?.nr_throttled;
  const window = sampleWindow(prev?.timestamp, curr.timestamp);
  if (a === null || a === undefined || b === null || b === undefined || window === null || b < a) return null;
  return { count: b - a, windowSeconds: window };
}

/** "5.0 s", the window a reading covers. */
export function formatWindow(seconds: number): string {
  return `${seconds.toFixed(1)} s`;
}
