import type { Query } from "@tanstack/react-query";
import { backingOffInterval, POLL_MS } from "@/lib/query-config";
import { APIError, type InstanceMetrics } from "@/types/api";

/** The second sample a fresh baseline needs. Short, so the first CPU figure is not 5 s away. */
export const METRICS_SECOND_SAMPLE_MS = 2_000;

const failingInterval = backingOffInterval(POLL_MS.live, 60 * 1000);

function statusOf(error: unknown): number | null {
  if (error instanceof APIError) return error.status;
  if (error instanceof Error) {
    const match = /\b(401|403|429)\b/.exec(error.message);
    return match ? Number(match[1]) : null;
  }
  return null;
}

/** 401 and 403 do not resolve by waiting, so they end the automatic loop. */
export function isMetricsAccessError(error: unknown): boolean {
  const status = statusOf(error);
  return status === 401 || status === 403;
}

export function isMetricsRateLimited(error: unknown): boolean {
  return statusOf(error) === 429;
}

/**
 * When the instance metrics query polls next.
 *
 * - access refused: stop; the panel says why and a retry would be refused again
 * - rate limited: wait the server's Retry-After, then resume on its own
 * - failing: back off from 10 s to one minute while the last reading stays on screen
 * - not running: keep checking at the list cadence, so a start shows up without a reopen
 * - waiting for a second sample: come back in 2 s rather than 5
 */
export function metricsRefetchInterval(
  query: Query<InstanceMetrics, Error, InstanceMetrics, readonly unknown[]>,
): number | false {
  const { data, error, status } = query.state;
  if (isMetricsAccessError(error)) return false;
  if (status === "error" && isMetricsRateLimited(error)) {
    const retryAfter = error instanceof APIError ? error.retryAfter : undefined;
    return Math.max(POLL_MS.live, (retryAfter ?? 30) * 1_000);
  }
  if (status === "error") return failingInterval(query);
  if (data && data.status !== "Running") return POLL_MS.active;
  if (data?.cpu_sample_status === "calculating") return METRICS_SECOND_SAMPLE_MS;
  return POLL_MS.live;
}

/** Two quick retries for a dropped request; never for a refusal or a rate limit. */
export function shouldRetryMetrics(failureCount: number, error: unknown): boolean {
  return !isMetricsAccessError(error) && !isMetricsRateLimited(error) && failureCount < 2;
}
