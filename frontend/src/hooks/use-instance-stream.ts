"use client";

/** Keeps the inventory current from daemon events while exact durable jobs own create verdicts. */
import { useCallback, useEffect, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/hooks/use-lxd";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { getEventsWsUrl } from "@/lib/ws-url";
import { toast } from "@/hooks/use-toast";
import {
  PendingCreate,
  getPendingSnapshot,
  removePendingCreate,
  resolvePendingCreate,
  subscribePending,
  updatePendingCreate,
} from "@/lib/pending-creates";
import { APIError } from "@/types/api";
import { matchesCreateOperation } from "@/lib/instance-operation-identity";

const STATUS_SUCCESS = 200;

interface Conn {
  ws: WebSocket | null;
  stop: boolean;
}

const sameInstance = (a: any, host: string, name: string, project?: string) =>
  a.host === host &&
  a.name === name &&
  (project ? (a.project || "default") === (project || "default") : true);

export function useInstanceStream(instanceHosts: string[]): PendingCreate[] {
  const { scopeKey } = useActorQueryScope();
  const subscribe = useCallback(
    (listener: () => void) => subscribePending(scopeKey, listener),
    [scopeKey],
  );
  const getSnapshot = useCallback(
    () => getPendingSnapshot(scopeKey),
    [scopeKey],
  );
  const pending = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getSnapshot,
  );
  const queryClient = useQueryClient();

  const pendingJobsKey = pending
    .filter(
      (entry) =>
        entry.jobId &&
        !entry.pollingHalted &&
        entry.jobStatus !== "success" &&
        entry.jobStatus !== "failure" &&
        entry.jobStatus !== "partial",
    )
    .map((entry) => `${entry.id}:${entry.jobId}`)
    .sort()
    .join(",");

  // The daemon event stream is still the fastest path, but it is not the source of truth for
  // a submitted create. Poll the exact durable job so a WebSocket drop, a missing project on
  // an event frame, or a page reload cannot leave the row claiming "Creating" indefinitely.
  useEffect(() => {
    if (!pendingJobsKey) return;
    let active = true;
    let polling = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const trackedJobs = new Set(pendingJobsKey.split(","));

    const poll = async () => {
      if (!active || polling) return;
      polling = true;
      let nextDelay = 2_500;
      const entries = getPendingSnapshot(scopeKey).filter(
        (entry) => entry.jobId && trackedJobs.has(`${entry.id}:${entry.jobId}`),
      );
      await Promise.all(
        entries.map(async (entry) => {
          try {
            const job = await api.instances.getOperation(entry.jobId!);
            if (!active) return;
            if (!matchesCreateOperation(entry, job)) {
              updatePendingCreate(scopeKey, entry.id, {
                status: "Error",
                jobStatus: "unknown",
                pollingHalted: true,
                error: "The operation identity did not match this pending instance. Polling stopped.",
              });
              return;
            }
            const alreadyFailed =
              entry.jobStatus === "failure" ||
              entry.jobStatus === "partial" ||
              entry.jobStatus === "unknown";
            const patch: Partial<PendingCreate> = {
              jobStatus: job.status,
              phase: job.phase,
              operationId: job.operation_id || entry.operationId,
              lastSignalAt: Date.now(),
            };
            if (job.status === "success") {
              patch.status = "Success";
              patch.error = undefined;
              patch.description = "Refreshing instance inventory";
              void queryClient.invalidateQueries({ queryKey: ["instances", scopeKey] });
            } else if (job.status === "failure") {
              patch.status = "Error";
              patch.error = job.error || "Creation failed";
            } else if (job.status === "partial") {
              patch.status = "Partial";
              patch.error = job.error || "Creation completed only partially";
              void queryClient.invalidateQueries({ queryKey: ["instances", scopeKey] });
            } else if (job.status === "unknown") {
              nextDelay = Math.max(nextDelay, 10_000);
              patch.status = "Unknown";
              patch.error = job.error || "The cluster could not confirm the final create state";
            } else {
              patch.status = job.status === "pending" ? "Pending" : "Running";
              patch.error = undefined;
            }
            updatePendingCreate(scopeKey, entry.id, patch);
            if (
              !alreadyFailed &&
              (job.status === "failure" || job.status === "partial" || job.status === "unknown")
            ) {
              toast({
                variant: "destructive",
                title: `Could not finish creating ${entry.name}`,
                description: patch.error,
              });
            }
          } catch (error) {
            if (!active) return;
            nextDelay = Math.max(nextDelay, 5_000);
            if (error instanceof APIError && (error.status === 401 || error.status === 403 || error.status === 404)) {
              updatePendingCreate(scopeKey, entry.id, {
                status: "Error",
                jobStatus: "unknown",
                pollingHalted: true,
                error:
                  error.status === 404
                    ? "The create operation is no longer available. Refresh inventory before retrying."
                    : "You no longer have permission to view this create operation.",
              });
            }
          }
        }),
      );
      polling = false;
      if (active) timer = setTimeout(() => void poll(), nextDelay);
    };

    void poll();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [pendingJobsKey, queryClient, scopeKey]);

  const hosts = Array.from(
    new Set([...instanceHosts, ...pending.map((p) => p.host)]),
  ).filter(Boolean);
  const hostsKey = hosts.slice().sort().join(",");

  useEffect(() => {
    let active = true;
    const conns = new Map<string, Conn>();
    const reconnectTimers = new Set<ReturnType<typeof setTimeout>>();
    const ipRetryTimers = new Set<ReturnType<typeof setTimeout>>();
    // A list is keyed ["instances", actor, host, project], with "*" for every host or
    // project. A row only belongs in the current actor's lists whose scope contains it.
    const scopeHolds = (key: readonly unknown[], vm: any) => {
      const [kind, actor, host, project] = key as [string, string, string, string];
      return (
        kind === "instances" &&
        actor === scopeKey &&
        (host === "*" || host === vm.host) &&
        (project === "*" || project === (vm.project || "default"))
      );
    };

    const upsertInstance = (vm: any) => {
      if (!active) return;
      queryClient.setQueriesData(
        { queryKey: ["instances", scopeKey], predicate: (q) => scopeHolds(q.queryKey, vm) },
        (old: any) => {
          if (!old || !Array.isArray(old.vms)) return old;
          const idx = old.vms.findIndex((i: any) =>
            sameInstance(i, vm.host, vm.name, vm.project),
          );
          const vms =
            idx >= 0
              ? old.vms.map((i: any, k: number) => (k === idx ? vm : i))
              : [vm, ...old.vms];
          return { ...old, vms, total: vms.length };
        },
      );
    };

    const removeInstance = (host: string, name: string, project?: string) => {
      if (!active) return;
      queryClient.setQueriesData(
        {
          queryKey: ["instances", scopeKey],
          predicate: (q) => {
            const [, actor, scopedHost, scopedProject] = q.queryKey as [
              string,
              string,
              string,
              string,
            ];
            return (
              actor === scopeKey &&
              (scopedHost === "*" || scopedHost === host) &&
              (!project || scopedProject === "*" || scopedProject === project)
            );
          },
        },
        (old: any) => {
          if (!old || !Array.isArray(old.vms)) return old;
          const vms = old.vms.filter(
            (i: any) => !sameInstance(i, host, name, project),
          );
          if (vms.length === old.vms.length) return old;
          return { ...old, vms, total: vms.length };
        },
      );
    };

    // Fetch one instance and upsert it. A just-started instance is "Running"
    // before its DHCP lease lands, so its IPs are briefly empty; in that case we
    // re-fetch a few times (bounded and event-triggered, not a return to polling)
    // until the address appears or we give up.
    // important: with the project. Two projects can each hold an instance called "test", and a
    // GET without one answers whichever the backend finds first.
    const fetchAndUpsert = async (
      host: string,
      name: string,
      project?: string,
      ipRetries = 6,
    ): Promise<boolean> => {
      try {
        const vm: any = await api.instances.get(name, host, project);
        if (!active) return false;
        upsertInstance(vm);
        const needsIp =
          vm.status === "Running" &&
          (!Array.isArray(vm.ip_addresses) || vm.ip_addresses.length === 0);
        if (needsIp && ipRetries > 0) {
          const timer = setTimeout(() => {
            ipRetryTimers.delete(timer);
            if (!active) return;
            // Only keep retrying while the row is still present.
            void fetchAndUpsert(host, name, project, ipRetries - 1);
          }, 4000);
          ipRetryTimers.add(timer);
        }
        return true;
      } catch {
        // Instance gone, or a transient race. The list never polls in realtime mode, so a
        // refetch is the one thing that can still carry the row in.
        if (active) {
          void queryClient.invalidateQueries({ queryKey: ["instances", scopeKey] });
        }
        return false;
      }
    };

    // Match project when the daemon provides it. Older frames only carry host and name.
    const findPending = (host: string, name: string, project?: string | null) =>
      getPendingSnapshot(scopeKey).find(
        (x) =>
          x.host === host &&
          x.name === name &&
          (!project || (x.project || "default") === project),
      );

    const dropPending = (host: string, name: string, project?: string | null) => {
      const p = findPending(host, name, project);
      if (!p) return;
      if (p.jobId && p.jobStatus !== "success") {
        updatePendingCreate(scopeKey, p.id, { lastSignalAt: Date.now() });
        return;
      }
      removePendingCreate(scopeKey, p.id);
    };

    // Retire only after the real row is cached. Exact jobs also require a success verdict.
    const settle = (host: string, name: string, project: string | null | undefined, ok: boolean) => {
      if (!ok) return;
      const p = findPending(host, name, project);
      if (!p) return;
      if (p.jobId && p.jobStatus !== "success") {
        updatePendingCreate(scopeKey, p.id, { lastSignalAt: Date.now() });
        return;
      }
      resolvePendingCreate(scopeKey, p.id);
    };

    const handleOperation = (host: string, msg: any) => {
      if (!active) return;
      const names: string[] = Array.isArray(msg.instances)
        ? msg.instances
        : msg.instance
          ? [msg.instance]
          : [];
      if (names.length === 0) return;
      const project: string | undefined = msg.project || undefined;
      for (const p of getPendingSnapshot(scopeKey)) {
        if (p.host !== host || !names.includes(p.name)) continue;
        if (project && (p.project || "default") !== project) continue;
        updatePendingCreate(scopeKey, p.id, { lastSignalAt: Date.now() });
        const code: number | undefined =
          typeof msg.status_code === "number" ? msg.status_code : undefined;
        if (code !== undefined && code >= STATUS_SUCCESS) {
          if (code === STATUS_SUCCESS) {
            // Created: pull the real row in, then retire the placeholder into a focused row.
            void fetchAndUpsert(host, p.name, p.project).then((ok) =>
              settle(host, p.name, p.project, ok),
            );
          } else {
            const reason = msg.err || "Creation failed";
            if (p.jobId) {
              // A daemon frame covers one leg. The exact job owns the durable verdict.
              updatePendingCreate(scopeKey, p.id, {
                lastSignalAt: Date.now(),
                statusCode: code,
                description: msg.description || "Waiting for exact create job status",
                operationId: msg.operation_id || p.operationId,
              });
              continue;
            }
            // LXD can emit more than one frame for the same failed operation, so only the
            // transition into Error raises a toast. Without this the operator gets the same
            // destructive toast several times for one create.
            const alreadyFailed = p.status === "Error";
            updatePendingCreate(scopeKey, p.id, {
              status: "Error",
              statusCode: code,
              error: reason,
            });
            // important: the failed row is NOT auto-removed. It used to disappear after 8s,
            // which meant an operator who looked away for a moment saw a create that had
            // reported success simply never arrive, with nothing left on screen to say why.
            // The row stays until it is dismissed or the page is left; it is the only record
            // of the failure the operator has, because the audit trail is not readable from
            // the dashboard.
            //
            // The toast is what covers the operator who is no longer on this page. Toaster is
            // mounted in the root layout, so it survives the navigate() the create form does
            // straight after submitting.
            if (!alreadyFailed) {
              toast({
                variant: "destructive",
                title: `Could not create ${p.name}`,
                description: reason,
              });
            }
          }
        } else {
          updatePendingCreate(scopeKey, p.id, {
            lastSignalAt: Date.now(),
            status: msg.status || "Running",
            statusCode: code,
            progress: msg.progress || undefined,
            description: msg.description || undefined,
            operationId: msg.operation_id || p.operationId,
          });
        }
      }
    };

    const handleLifecycle = (host: string, msg: any) => {
      if (!active) return;
      const name: string | undefined = msg.instance;
      if (!name) return;
      const action: string = msg.action || "";
      if (action === "instance-deleted") {
        const project: string | undefined = msg.project || undefined;
        if (project) {
          removeInstance(host, name, project);
          dropPending(host, name, project);
        } else {
          // A name is only unique inside a project. Without one, refetch instead of removing
          // every same-named instance from this actor's visible cache.
          void queryClient.invalidateQueries({ queryKey: ["instances", scopeKey] });
        }
        return;
      }
      if (action === "instance-renamed") {
        // Name changed. The cheapest correct move is a single refetch.
        void queryClient.invalidateQueries({ queryKey: ["instances", scopeKey] });
        return;
      }
      // created / started / stopped / restarted / updated / paused / resumed …
      const project: string | undefined = msg.project || undefined;
      if (!project) {
        const pendingEntry = findPending(host, name);
        if (pendingEntry) {
          updatePendingCreate(scopeKey, pendingEntry.id, { lastSignalAt: Date.now() });
        }
        void queryClient.invalidateQueries({ queryKey: ["instances", scopeKey] });
        return;
      }
      const pendingEntry = findPending(host, name, project);
      if (pendingEntry) {
        updatePendingCreate(scopeKey, pendingEntry.id, { lastSignalAt: Date.now() });
      }
      void fetchAndUpsert(host, name, project).then((ok) =>
        settle(host, name, project, ok),
      );
    };

    const handleMessage = (host: string, raw: string) => {
      if (!active) return;
      let msg: any;
      try {
        msg = JSON.parse(raw);
      } catch {
        return;
      }
      if (msg?.type === "operation") handleOperation(host, msg);
      else if (msg?.type === "lifecycle") handleLifecycle(host, msg);
    };

    const connect = (host: string) => {
      const conn = conns.get(host);
      if (!active || !conn || conn.stop) return;
      const ws = new WebSocket(getEventsWsUrl(host));
      conn.ws = ws;
      ws.onmessage = (ev) => handleMessage(host, ev.data as string);
      ws.onclose = () => {
        conn.ws = null;
        if (active && !conn.stop) {
          const timer = setTimeout(() => {
            reconnectTimers.delete(timer);
            if (active) connect(host);
          }, 2000);
          reconnectTimers.add(timer);
        }
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      };
    };

    const closeHost = (host: string) => {
      const conn = conns.get(host);
      if (!conn) return;
      conn.stop = true;
      if (conn.ws) {
        conn.ws.onmessage = null;
        conn.ws.onclose = null;
        conn.ws.onerror = null;
      }
      try {
        conn.ws?.close();
      } catch {
        /* ignore */
      }
      conns.delete(host);
    };

    for (const host of hosts) {
      if (!conns.has(host)) {
        conns.set(host, { ws: null, stop: false });
        connect(host);
      }
    }
    for (const host of Array.from(conns.keys())) {
      if (!hosts.includes(host)) closeHost(host);
    }

    return () => {
      active = false;
      reconnectTimers.forEach(clearTimeout);
      ipRetryTimers.forEach(clearTimeout);
      for (const host of Array.from(conns.keys())) closeHost(host);
    };
  }, [hostsKey, queryClient, scopeKey]); // eslint-disable-line react-hooks/exhaustive-deps

  return pending;
}
