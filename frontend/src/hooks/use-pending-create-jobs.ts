"use client";

import { useEffect } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { toast } from "@/hooks/use-toast";
import { api, isAuthActorSynchronized } from "@/lib/api";
import { reconcileInstanceFleet } from "@/lib/instance-reconciliation";
import { matchesCreateOperation } from "@/lib/instance-operation-identity";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import {
  getPendingSnapshot,
  type PendingCreate,
  updatePendingCreate,
} from "@/lib/pending-creates";
import { APIError } from "@/types/api";

const TERMINAL = new Set(["success", "failure", "partial"]);

export function usePendingCreateJobs(
  pending: PendingCreate[],
  scopeKey: string,
  queryClient: QueryClient,
): void {
  const pendingJobsKey = pending
    .filter(
      (entry) =>
        entry.jobId &&
        !entry.pollingHalted &&
        !TERMINAL.has(entry.jobStatus ?? ""),
    )
    .map((entry) => `${entry.id}:${entry.jobId}`)
    .sort()
    .join(",");
  // A dependency of the effect below and nothing else: changing it restarts the poller, which
  // reads at once. An unknown verdict otherwise waits out its 10 s delay after a manual recheck.
  const latestRecheck = pending.reduce((latest, entry) => Math.max(latest, entry.recheckAt ?? 0), 0);

  // A durable create job owns the verdict. WebSocket frames are only acceleration signals.
  useEffect(() => {
    if (!pendingJobsKey) return;
    let active = true;
    let polling = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const trackedJobs = new Set(pendingJobsKey.split(","));

    const poll = async () => {
      if (!active || polling) return;
      polling = true;
      let nextDelay = 2_500;
      const entries = getPendingSnapshot(scopeKey).filter(
        (entry) => entry.jobId && trackedJobs.has(`${entry.id}:${entry.jobId}`),
      );
      await Promise.all(entries.map(async (entry) => {
        try {
          const job = await api.instances.getOperation(entry.jobId!, {
            signal: controller.signal,
          });
          if (!active || !isAuthActorSynchronized(scopeKey)) return;
          if (!matchesCreateOperation(entry, job)) {
            updatePendingCreate(scopeKey, entry.id, {
              status: "Error",
              jobStatus: "unknown",
              pollingHalted: true,
              error: "The operation identity did not match this pending instance. Polling stopped.",
            });
            return;
          }
          const wasNonSuccess = ["failure", "partial", "unknown"].includes(
            entry.jobStatus ?? "",
          );
          const patch: Partial<PendingCreate> = {
            jobStatus: job.status,
            phase: job.phase,
            operationId: job.operation_id || entry.operationId,
            lastSignalAt: Date.now(),
          };
          if (job.status === "success") {
            Object.assign(patch, {
              status: "Success",
              error: undefined,
              description: "Refreshing instance inventory",
            });
          } else if (job.status === "failure") {
            Object.assign(patch, { status: "Error", error: job.error || "Creation failed" });
          } else if (job.status === "partial") {
            Object.assign(patch, {
              status: "Partial",
              error: job.error || "Creation completed only partially",
            });
          } else if (job.status === "unknown") {
            nextDelay = Math.max(nextDelay, 10_000);
            Object.assign(patch, {
              status: "Unknown",
              error: job.error || "The cluster could not confirm the final create state",
            });
          } else {
            Object.assign(patch, {
              status: job.status === "pending" ? "Pending" : "Running",
              error: undefined,
            });
          }
          updatePendingCreate(scopeKey, entry.id, patch);
          if (TERMINAL.has(job.status)) {
            // Reconcile every terminal verdict. Tag assignments and statistics materialize in the
            // same terminal transaction as the durable job, while a partial or failed create may
            // still have left an instance behind. Invalidate the complete declared impact first,
            // then make the authoritative fleet snapshot the last instance-list write.
            void (async () => {
              if (!isAuthActorSynchronized(scopeKey)) return;
              await reconcileMutationImpact(queryClient, "instance.create");
              if (!isAuthActorSynchronized(scopeKey)) return;
              await reconcileInstanceFleet(queryClient, scopeKey, { fresh: true });
              if (!isAuthActorSynchronized(scopeKey)) return;
            })().catch(() => {});
          }
          if (!wasNonSuccess && ["failure", "partial", "unknown"].includes(job.status)) {
            toast({
              variant: "destructive",
              title: `Could not finish creating ${entry.name}`,
              description: patch.error,
            });
          }
        } catch (error) {
          if (!active) return;
          nextDelay = Math.max(
            nextDelay,
            error instanceof APIError && error.status === 429
              ? (error.retryAfter ?? 30) * 1_000
              : 5_000,
          );
          if (
            error instanceof APIError &&
            (error.status === 401 || error.status === 403 || error.status === 404)
          ) {
            updatePendingCreate(scopeKey, entry.id, {
              status: "Error",
              jobStatus: "unknown",
              pollingHalted: true,
              error: error.status === 404
                ? "The create operation is no longer available. Refresh inventory before retrying."
                : "You no longer have permission to view this create operation.",
            });
          }
        }
      }));
      polling = false;
      if (active) timer = setTimeout(() => void poll(), nextDelay);
    };

    void poll();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [pendingJobsKey, latestRecheck, queryClient, scopeKey]);
}
