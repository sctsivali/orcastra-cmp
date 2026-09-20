"use client";

import { useQuery } from "@tanstack/react-query";

import { useActorQueryScope } from "@/hooks/use-query-scope";
import { instanceAPI } from "@/lib/api";
import { DYNAMIC_DATA_CONFIG, POLL_MS } from "@/lib/query-config";

/**
 * The current user's safe instance specification projection.
 *
 * The actor scope belongs in the browser cache key too. A sign-out followed by a sign-in as a
 * different organization can reuse the same QueryClient, and the previous tenant's last good
 * response must never flash while the new request is in flight.
 */
export function useInstanceSpecifications() {
  const { scopeKey, isAuthenticated } = useActorQueryScope();

  return useQuery({
    queryKey: ["instanceSpecifications", scopeKey],
    queryFn: () => instanceAPI.specifications(),
    enabled: isAuthenticated,
    // The endpoint caches this scoped fan-out for 30 seconds. Polling faster cannot make it
    // newer and only produces duplicate reads.
    refetchInterval: POLL_MS.active,
    refetchIntervalInBackground: false,
    ...DYNAMIC_DATA_CONFIG,
  });
}
