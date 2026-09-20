/**
 * Optimized query configurations for different data types
 * Based on update frequency and user interaction patterns
 */

import { APIError } from "@/types";

/**
 * Poll intervals, matched to the response-cache TTL each endpoint carries on the backend.
 *
 * important: polling faster than the server's TTL cannot produce a newer number. It returns
 * the identical cached body and a log line, and on these endpoints a miss is a fan-out across
 * every cluster in scope. Each value below is the TTL of the endpoints it serves:
 *
 *   live      uncached, single-instance metrics while its Metrics tab is visible
 *   watched   uncached, single-cluster reads a person is looking at right now
 *   active    30s   /instances, /monitoring/*
 *   attended  60s   /nodes/availability (45), /cluster-members/{m}/resources, pool resources
 *   fleet     180s  /nodes, /clusters, /networks, /storage-*, /images, /vms
 *
 * Anything slower than fleet is catalog data and should not poll at all: it changes only when
 * an operator acts in this console, and every mutation already invalidates its key.
 */
export const POLL_MS = {
  live: 5 * 1000,
  watched: 10 * 1000,
  active: 30 * 1000,
  attended: 60 * 1000,
  fleet: 180 * 1000,
} as const;

/**
 * Age past which a reading stops being a claim about now.
 *
 * Three intervals, not one: a tab that was hidden misses its ticks, so a single interval would
 * cry wolf on every ordinary resume and operators would learn to ignore the marker.
 */
export const STALE_AFTER_MS = {
  live: 3 * POLL_MS.live,
  watched: 3 * POLL_MS.watched,
  active: 3 * POLL_MS.active,
  attended: 3 * POLL_MS.attended,
  fleet: 3 * POLL_MS.fleet,
} as const;

/**
 * Spread a poll interval by +/-10%, resolved once per hook instance.
 *
 * important: every query created in the same render tick shares a deadline for the life of the
 * page, so a page of twelve cards produces one synchronised burst per cycle rather than a
 * trickle. On the fleet tier those bursts land on an expired TTL together.
 *
 * note: call this from a lazy useState initialiser, never in the render body, or the cadence
 * wanders on every re-render.
 */
export function jitter(base: number): number {
  return Math.round(base * (0.9 + Math.random() * 0.2));
}

/** Minimal shape of the query object React Query hands a refetchInterval function. */
type PollingQuery = { state: { fetchFailureCount: number } };

/**
 * Poll at `base`, backing off geometrically while the query is failing.
 *
 * A cluster that is not answering is where polling costs most and buys least: every attempt
 * sits on a connect timeout, and the operator already has the last reading on screen with its
 * age beside it, which is worth more than a fifth identical failure.
 */
export function backingOffInterval(base: number, max: number = 5 * 60 * 1000) {
  return (query: PollingQuery): number => {
    const failures = query.state.fetchFailureCount;
    return failures <= 0 ? base : Math.min(base * 2 ** failures, max);
  };
}

/**
 * Smart retry: skip retries for auth errors (401) and rate limits (429).
 * These won't resolve by retrying and would worsen a 429 storm.
 */
function shouldRetry(failureCount: number, error: unknown, maxRetries: number): boolean {
  if (error instanceof APIError) {
    if (error.status === 401 || error.status === 429 || error.status === 403) {
      return false;
    }
  }
  // still check the raw Error message for status codes
  if (error instanceof Error) {
    const msg = error.message;
    if (msg.includes('401') || msg.includes('429') || msg.includes('403')) {
      return false;
    }
  }
  return failureCount < maxRetries;
}

/**
 * Configuration for data that rarely changes (nodes, clusters, projects)
 * - Longer staleTime to reduce unnecessary refetches
 * - Longer cache duration
 * - No window focus refetch
 */
export const STATIC_DATA_CONFIG = {
  staleTime: 3 * 60 * 1000, // 3 minutes
  gcTime: 10 * 60 * 1000, // 10 minutes
  refetchOnWindowFocus: false,
  refetchOnMount: true, // Fetch on mount to ensure data is loaded
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 2),
};

/**
 * Configuration for semi-static data (images, profiles, networks)
 * - Moderate staleTime
 * - Good cache duration
 */
export const SEMI_STATIC_DATA_CONFIG = {
  staleTime: 2 * 60 * 1000, // 2 minutes
  gcTime: 5 * 60 * 1000, // 5 minutes
  refetchOnWindowFocus: false,
  refetchOnMount: true, // Fetch on mount to ensure data is loaded
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 2),
};

/**
 * Configuration for frequently changing data (instances, status, metrics)
 * - Shorter staleTime for fresher data
 * - Still cache to avoid excessive requests
 */
export const DYNAMIC_DATA_CONFIG = {
  staleTime: 30 * 1000, // 30 seconds
  gcTime: 2 * 60 * 1000, // 2 minutes
  // note: no refetch on tab focus. Returning from AFK would fire every query at
  // once under a possibly-expired token. Freshness comes from the refetchInterval
  // the call site sets from POLL_MS below, which resumes on its own once the tab
  // is visible again. This preset deliberately sets no interval of its own: it is
  // shared with permission surfaces (identities, groups, ACLs) where a value
  // changing under an admin mid-edit is worse than a stale one.
  refetchOnWindowFocus: false,
  refetchOnMount: true, // Fetch on mount to ensure data is loaded
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};

/**
 * Configuration for data an operator is watching while it is on screen.
 *
 * note: this was called REALTIME_DATA_CONFIG and had no call sites at all, which is how a
 * 60-second interval came to be the only one in this file while every list on the console
 * sat still. Nobody reaches for a constant called REALTIME when they want a minute.
 */
export const ATTENDED_DATA_CONFIG = {
  staleTime: 30 * 1000,
  gcTime: 1 * 60 * 1000,
  refetchInterval: POLL_MS.attended,
  refetchIntervalInBackground: false, // Stop polling when tab is hidden
  refetchOnWindowFocus: false, // see DYNAMIC_DATA_CONFIG
  refetchOnMount: true,
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};

/**
 * Configuration for on-demand data (details, operations)
 * - Cache but don't auto-refetch
 * - User-initiated refreshes only
 */
export const ON_DEMAND_CONFIG = {
  staleTime: 5 * 60 * 1000, // 5 minutes
  gcTime: 10 * 60 * 1000, // 10 minutes
  refetchOnWindowFocus: false,
  refetchOnMount: false,
  refetchOnReconnect: false,
  enabled: false, // Typically used with manual trigger
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};
