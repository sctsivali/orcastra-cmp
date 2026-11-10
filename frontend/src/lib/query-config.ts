/** React Query presets, grouped by how often the data changes. */

import { APIError } from "@/types";

/**
 * Poll intervals, matched to the backend response-cache TTL of the endpoints they serve.
 * Polling faster than the TTL only returns the same cached body, and a cache miss fans out to
 * every cluster in scope.
 *
 *   live      uncached, single-instance metrics while the Metrics tab is visible
 *   watched   uncached, single-cluster reads on screen
 *   active    30s   /instances, /monitoring/*
 *   attended  60s   /nodes/availability (45), /cluster-members/{m}/resources, pool resources
 *   fleet     180s  /nodes, /clusters, /networks, /storage-*, /images, /vms
 *
 * Catalog data slower than fleet should not poll; mutations already invalidate its key.
 */
export const POLL_MS = {
  live: 5 * 1000,
  watched: 10 * 1000,
  active: 30 * 1000,
  attended: 60 * 1000,
  fleet: 180 * 1000,
} as const;

/**
 * Age after which a reading is shown as stale. Three intervals, because a hidden tab skips ticks
 * and one interval would flag every ordinary resume.
 */
export const STALE_AFTER_MS = {
  live: 3 * POLL_MS.live,
  watched: 3 * POLL_MS.watched,
  active: 3 * POLL_MS.active,
  attended: 3 * POLL_MS.attended,
  fleet: 3 * POLL_MS.fleet,
} as const;

/**
 * Spread a poll interval by +/-10% so queries created in the same render do not poll in
 * lockstep and hit an expired cache TTL together.
 *
 * Call it from a lazy useState initialiser, not the render body, or the interval changes on
 * every render.
 */
export function jitter(base: number): number {
  return Math.round(base * (0.9 + Math.random() * 0.2));
}

/** Minimal shape of the query object React Query hands a refetchInterval function. */
type PollingQuery = {
  state: {
    status: string;
    dataUpdatedAt: number;
    errorUpdatedAt: number;
    errorUpdateCount: number;
  };
};

/**
 * Poll at `base`, backing off geometrically while the query keeps failing. An unreachable
 * cluster makes every attempt wait on a connect timeout, and the last reading is already shown.
 *
 * The delay is based on how long the query has been failing. `fetchFailureCount` cannot be used:
 * React Query resets it at the start of every fetch.
 */
export function backingOffInterval(base: number, max: number = 5 * 60 * 1000) {
  return (query: PollingQuery): number => {
    const { status, dataUpdatedAt, errorUpdatedAt, errorUpdateCount } = query.state;
    if (status !== "error") return base;
    const failingFor =
      dataUpdatedAt > 0
        ? errorUpdatedAt - dataUpdatedAt
        : base * 2 ** Math.min(Math.max(errorUpdateCount, 1), 10);
    return Math.min(Math.max(2 * base, failingFor), max);
  };
}

/** No retry on 401, 403 or 429: retrying cannot fix them and makes a 429 storm worse. */
function shouldRetry(failureCount: number, error: unknown, maxRetries: number): boolean {
  if (error instanceof APIError) {
    if (error.status === 401 || error.status === 429 || error.status === 403) {
      return false;
    }
  }
  // Some callers throw a plain Error with the status in the message.
  if (error instanceof Error) {
    const msg = error.message;
    if (msg.includes('401') || msg.includes('429') || msg.includes('403')) {
      return false;
    }
  }
  return failureCount < maxRetries;
}

/** Data that rarely changes: nodes, clusters, projects. */
export const STATIC_DATA_CONFIG = {
  staleTime: 3 * 60 * 1000,
  gcTime: 10 * 60 * 1000,
  refetchOnWindowFocus: false,
  refetchOnMount: true,
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 2),
};

/** Data that changes occasionally: images, profiles, networks. */
export const SEMI_STATIC_DATA_CONFIG = {
  staleTime: 2 * 60 * 1000,
  gcTime: 5 * 60 * 1000,
  refetchOnWindowFocus: false,
  refetchOnMount: true,
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 2),
};

/** Frequently changing data: instances, status, metrics. */
export const DYNAMIC_DATA_CONFIG = {
  staleTime: 30 * 1000,
  gcTime: 2 * 60 * 1000,
  // No refetch on focus: returning to an idle tab would fire every query at once, possibly on
  // an expired token. Call sites set refetchInterval from POLL_MS instead. No interval here
  // because permission screens share this preset and must not change under an admin mid-edit.
  refetchOnWindowFocus: false,
  refetchOnMount: true,
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};

/** Data an operator is watching on screen, polled at the attended interval. */
export const ATTENDED_DATA_CONFIG = {
  staleTime: 30 * 1000,
  gcTime: 1 * 60 * 1000,
  refetchInterval: POLL_MS.attended,
  refetchIntervalInBackground: false,
  refetchOnWindowFocus: false, // see DYNAMIC_DATA_CONFIG
  refetchOnMount: true,
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};

/** On-demand data (details, operations), fetched only when the caller enables or refetches it. */
export const ON_DEMAND_CONFIG = {
  staleTime: 5 * 60 * 1000,
  gcTime: 10 * 60 * 1000,
  refetchOnWindowFocus: false,
  refetchOnMount: false,
  refetchOnReconnect: false,
  enabled: false,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};
