/**
 * Optimized query configurations for different data types
 * Based on update frequency and user interaction patterns
 */

import { APIError } from "@/types";

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
  refetchOnWindowFocus: true, // Refetch when tab becomes active
  refetchOnMount: true, // Fetch on mount to ensure data is loaded
  refetchOnReconnect: true,
  retry: (failureCount: number, error: unknown) => shouldRetry(failureCount, error, 1),
};

/**
 * Configuration for real-time data that needs frequent updates
 * - Very short staleTime
 * - Auto-refetch interval
 * - Useful for monitoring dashboards
 */
export const REALTIME_DATA_CONFIG = {
  staleTime: 15 * 1000, // 15 seconds
  gcTime: 1 * 60 * 1000, // 1 minute
  refetchInterval: 60 * 1000, // Auto-refetch every 60 seconds (reduced from 30s)
  refetchIntervalInBackground: false, // Stop polling when tab is hidden
  refetchOnWindowFocus: true,
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
