"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { vaultStatusAPI, type VaultState, type VaultStatus } from "@/lib/vault-status-api";
import { hasMappedRole } from "@/types/rbac";

const VAULT_STATUS_KEY = ["vault-status"] as const;
const HEALTHY_POLL_MS = 60_000;
const DEGRADED_POLL_MS = 15_000;
// Clusters reconnect on the backend's next 30 s sweep after an unseal, so the first refetch can
// still find some of them down. A second pass after that sweep picks them up.
const RECONNECT_SWEEP_MS = 35_000;

type NoticeState = Exclude<VaultState, "ok" | "not_configured">;

const isDegraded = (state: VaultState | undefined): state is NoticeState =>
  state !== undefined && state !== "ok" && state !== "not_configured";

/**
 * Whether the dashboard should say that Vault is sealed or unavailable.
 *
 * important: shows only on an answer. Loading and a failed read of the status itself show
 * nothing, because "the status could not be read" is not "Vault is sealed". Sealed and
 * uninitialized are Vault's own words and show on the first read; unreachable and unavailable
 * can be a single dropped request, so they show on the second consecutive one.
 */
export function useVaultStatus() {
  const { data: session, status } = useSession();
  const groups = session?.user?.groups;
  // A retained session during a refresh still counts, or the notice blinks every 4 minutes.
  const enabled = Boolean(session) && status !== "unauthenticated" && Array.isArray(groups) && hasMappedRole(groups);
  const queryClient = useQueryClient();
  const [checking, setChecking] = useState(false);
  const [streak, setStreak] = useState(0);
  const degradedRef = useRef(false);
  const sweepTimerRef = useRef<number | null>(null);

  const query = useQuery({
    queryKey: VAULT_STATUS_KEY,
    queryFn: ({ signal }) => vaultStatusAPI.get({ signal }),
    enabled,
    staleTime: 10_000,
    retry: 1,
    refetchOnWindowFocus: true,
    refetchInterval: (q) => (isDegraded(q.state.data?.state) ? DEGRADED_POLL_MS : HEALTHY_POLL_MS),
  });
  const data = query.data;

  useEffect(() => {
    if (!data) return;
    if (isDegraded(data.state)) {
      setStreak((count) => count + 1);
      degradedRef.current = true;
      return;
    }
    setStreak(0);
    if (!degradedRef.current) return;
    degradedRef.current = false;
    // Recovered: every read on this page answered from a Vault that could not be read, so ask
    // them all again rather than waiting out their own intervals.
    const refresh = () =>
      queryClient.invalidateQueries({ predicate: (q) => q.queryKey[0] !== VAULT_STATUS_KEY[0] });
    void refresh();
    if (sweepTimerRef.current !== null) window.clearTimeout(sweepTimerRef.current);
    sweepTimerRef.current = window.setTimeout(() => {
      sweepTimerRef.current = null;
      void refresh();
    }, RECONNECT_SWEEP_MS);
    // Keyed on the read, not the body, so two identical answers still count twice.
  }, [query.dataUpdatedAt, data, queryClient]);

  useEffect(
    () => () => {
      if (sweepTimerRef.current !== null) window.clearTimeout(sweepTimerRef.current);
    },
    [],
  );

  const checkNow = useCallback(async () => {
    setChecking(true);
    try {
      const fresh: VaultStatus = await vaultStatusAPI.get({ fresh: true });
      queryClient.setQueryData(VAULT_STATUS_KEY, fresh);
    } catch {
      // A failed check changes nothing on screen; the poll keeps going.
    } finally {
      setChecking(false);
    }
  }, [queryClient]);

  let visible: NoticeState | null = null;
  if (enabled && data && !query.isError && isDegraded(data.state)) {
    const confirmed = data.state === "sealed" || data.state === "uninitialized" || streak >= 2;
    visible = confirmed ? data.state : null;
  }

  return { state: visible, status: data ?? null, checking, checkNow };
}
