"use client";

import { useCallback, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useActorQueryScope } from "@/hooks/use-query-scope";
import { usePendingCreateJobs } from "@/hooks/use-pending-create-jobs";
import { getPendingSnapshot, subscribePending } from "@/lib/pending-creates";

/**
 * Follows every durable create job to its verdict on whichever page is open, so a create left
 * running when the operator navigates away still reconciles its new row.
 */
export function PendingCreateFollower(): null {
  const { scopeKey } = useActorQueryScope();
  const queryClient = useQueryClient();
  const subscribe = useCallback(
    (listener: () => void) => subscribePending(scopeKey, listener),
    [scopeKey],
  );
  const getSnapshot = useCallback(() => getPendingSnapshot(scopeKey), [scopeKey]);
  const pending = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  usePendingCreateJobs(pending, scopeKey, queryClient);
  return null;
}
