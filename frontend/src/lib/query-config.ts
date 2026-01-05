/**
 * Optimized query configurations for different data types
 * Based on update frequency and user interaction patterns
 */

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
  retry: 2,
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
  retry: 2,
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
  retry: 1,
};

/**
 * Configuration for real-time data that needs frequent updates
 * - Very short staleTime
 * - Auto-refetch interval
 * - Useful for monitoring dashboards
 */
export const REALTIME_DATA_CONFIG = {
  staleTime: 10 * 1000, // 10 seconds
  gcTime: 1 * 60 * 1000, // 1 minute
  refetchInterval: 30 * 1000, // Auto-refetch every 30 seconds
  refetchOnWindowFocus: true,
  refetchOnMount: true,
  refetchOnReconnect: true,
  retry: 1,
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
  retry: 1,
};
