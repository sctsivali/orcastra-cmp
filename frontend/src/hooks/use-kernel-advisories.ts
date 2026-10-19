"use client";

import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { kernelAdvisoryAPI } from "@/lib/kernel-advisory-api";
import { APIError } from "@/types/api";

// note: kernels only change on a reboot and the server keeps each cluster's reading for ten
// minutes, so a two-minute poll is plenty. Every card shares this one query key, so a page of
// cluster cards costs one request, not one per card.
const KERNEL_ADVISORY_POLL_MS = 120_000;

function retryRead(failureCount: number, error: unknown): boolean {
  return !(error instanceof APIError && [401, 403, 404, 429].includes(error.status)) && failureCount < 2;
}

/** Host kernel advisories per cluster member. Partner and Admin only: the endpoint is require_partner(). */
export function useKernelAdvisories(options: { enabled?: boolean } = {}) {
  const { isPartner } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useRefreshableQuery({
    queryKey: ["kernelAdvisories", scopeKey],
    queryFn: (read) => kernelAdvisoryAPI.list(read),
    enabled: isAuthenticated && isPartner && (options.enabled ?? true),
    refetchInterval: KERNEL_ADVISORY_POLL_MS,
    staleTime: KERNEL_ADVISORY_POLL_MS / 2,
    refetchOnWindowFocus: false,
    retry: retryRead,
  });
}
