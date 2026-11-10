"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useRefreshableQuery } from "@/hooks/use-refreshable-query";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { hostHealthAPI } from "@/lib/host-health-api";
import { vaultStatusAPI } from "@/lib/vault-status-api";
import { jitter, POLL_MS } from "@/lib/query-config";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { APIError } from "@/types/api";
import type { VaultSealStatus } from "@/types/host-health";

export const HOST_HEALTH_KEY = "host-health";
export const SEAL_STATUS_KEY = "vault-seal-status";
// While the dialog is open several key holders may be entering shares at once; two seconds is the
// backend's own floor for re-asking Vault.
const SEAL_POLL_MS = 2_000;

function retryRead(failureCount: number, error: unknown): boolean {
  return !(error instanceof APIError && [401, 403, 404, 429].includes(error.status)) && failureCount < 2;
}

/**
 * The Host Health report. Administrators only: the endpoint is require_admin(), and a refused read
 * is an audit row (rbac.access_denied), so no other role ever asks.
 *
 * `poll` is the page's 30 s tier; the sidebar count rides the 60 s tier on the same key.
 */
export function useHostHealth(options: { poll?: "page" | "badge" } = {}) {
  const { isAdmin } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [interval] = useState(() => jitter(options.poll === "badge" ? POLL_MS.attended : POLL_MS.active));
  return useRefreshableQuery({
    queryKey: [HOST_HEALTH_KEY, scopeKey],
    queryFn: (read) => hostHealthAPI.report(read),
    enabled: isAuthenticated && isAdmin,
    refetchInterval: interval,
    staleTime: POLL_MS.active / 2,
    retry: retryRead,
  });
}

/**
 * The newer of two seal statuses. Answers arrive out of order (a poll, a refetch after a submit, the
 * submit's own answer), and an older one must never replace a newer one: the dialog sends the
 * attempt's nonce with the next key, and a stale nonce is refused as "the attempt changed".
 */
export function newerSealStatus(
  current: VaultSealStatus | undefined,
  incoming: VaultSealStatus,
): VaultSealStatus {
  return current && current.checked_at > incoming.checked_at ? current : incoming;
}

/** Vault's seal state and unseal progress, polled only while the unseal dialog is open. */
export function useVaultSealStatus(open: boolean) {
  const { isAdmin } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: [SEAL_STATUS_KEY, scopeKey],
    queryFn: async ({ signal }) => {
      const incoming = await hostHealthAPI.sealStatus({ signal });
      return newerSealStatus(queryClient.getQueryData<VaultSealStatus>([SEAL_STATUS_KEY, scopeKey]), incoming);
    },
    enabled: open && isAuthenticated && isAdmin,
    refetchInterval: open ? SEAL_POLL_MS : false,
    staleTime: 0,
    retry: retryRead,
  });
}

/**
 * After Vault unseals: ask for the notice's status with `fresh`, so the worker that answers
 * re-probes instead of serving its cached "sealed" for up to ten seconds, and hand the answer to
 * the notice's own query, whose recovery path then re-reads every page's data.
 */
export async function announceUnsealed(queryClient: QueryClient): Promise<void> {
  try {
    let fresh = await vaultStatusAPI.get({ fresh: true });
    if (fresh.state !== "ok") {
      // Another worker can still hold a "sealed" answer younger than its two-second floor; one
      // more ask after that floor reaches a worker that has to re-probe.
      await new Promise((resolve) => setTimeout(resolve, 2_500));
      fresh = await vaultStatusAPI.get({ fresh: true });
    }
    queryClient.setQueryData(["vault-status"], fresh);
  } catch {
    // The notice's own poll still runs; a failed nudge changes nothing on screen.
  }
}

/**
 * Submit one key share or reset the attempt. The answer is Vault's own status, written straight
 * into the dialog's query so the progress moves without waiting for the next poll.
 */
export function useUnsealVault() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useMutation({
    mutationFn: (body: Parameters<typeof hostHealthAPI.unseal>[0]) => hostHealthAPI.unseal(body),
    // important: a mutation keeps its variables, and here the variables are a key share. Nothing
    // keeps the finished mutation around; the dialog also resets it after every submit.
    gcTime: 0,
    onSuccess: async (result) => {
      // A result is a status with an outcome on top; the dialog reads only the status fields.
      queryClient.setQueryData<VaultSealStatus>([SEAL_STATUS_KEY, scopeKey], (current) => newerSealStatus(current, result));
      await reconcileMutationImpact(queryClient, "vault.unseal");
      if (result.outcome === "unsealed") await announceUnsealed(queryClient);
    },
  });
}
