"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { kernelAdvisoryAPI } from "@/lib/kernel-advisory-api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { jitter } from "@/lib/query-config";
import { APIError } from "@/types/api";

// Kernels only change on a reboot and the server keeps each cluster's reading for ten minutes,
// so a two-minute poll is enough. Every card shares this one query key, so a page of cluster
// cards costs one request.
const KERNEL_ADVISORY_POLL_MS = 120_000;
// The server reads Ubuntu every few hours; this only follows the time of its last read.
const UBUNTU_STATUS_POLL_MS = 10 * 60 * 1000;

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

/** When the server last read Ubuntu's kernel CVE records. Partner and Admin only, like the advisories. */
export function useKernelCveUbuntuStatus() {
  const { isPartner } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [interval] = useState(() => jitter(UBUNTU_STATUS_POLL_MS));
  return useQuery({
    queryKey: ["kernelCveFeed", scopeKey],
    queryFn: ({ signal }) => kernelAdvisoryAPI.ubuntuStatus({ signal }),
    enabled: isAuthenticated && isPartner,
    refetchInterval: interval,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: retryRead,
  });
}

/** Administrators: read Ubuntu's records now, then re-read every verdict that depends on them. */
export function useCheckKernelCvesNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => kernelAdvisoryAPI.checkNow(),
    onSuccess: async () => {
      await reconcileMutationImpact(queryClient, "kernelCve.check");
    },
  });
}
