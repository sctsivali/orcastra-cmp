"use client";

/** Keeps inventory current from daemon events, durable jobs, and a bounded safety read. */
import { useCallback, useEffect, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useActorQueryScope } from "@/hooks/use-query-scope";
import { usePendingCreateJobs } from "@/hooks/use-pending-create-jobs";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import {
  createInstanceFreshnessCoordinator,
  getInstanceStreamHosts,
} from "@/lib/instance-freshness";
import {
  createExactInstanceReadFence,
  reconcileInstanceFleet,
  removeExactInstance,
  retainInstanceReconcileRateLimit,
  upsertExactInstance,
} from "@/lib/instance-reconciliation";
import { getEventsWsUrl } from "@/lib/ws-url";
import {
  type PendingCreate,
  getPendingSnapshot,
  removePendingCreate,
  resolvePendingCreate,
  subscribePending,
  updatePendingCreate,
} from "@/lib/pending-creates";
import { APIError } from "@/types/api";

const STATUS_SUCCESS = 200;
const RECONNECT_MS = 2_000;

interface Conn {
  ws: WebSocket | null;
  stop: boolean;
  healthy: boolean;
}

export function useInstanceStream(authorizedHosts: string[]): PendingCreate[] {
  const { scopeKey } = useActorQueryScope();
  const subscribe = useCallback(
    (listener: () => void) => subscribePending(scopeKey, listener),
    [scopeKey],
  );
  const getSnapshot = useCallback(() => getPendingSnapshot(scopeKey), [scopeKey]);
  const pending = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const queryClient = useQueryClient();

  usePendingCreateJobs(pending, scopeKey, queryClient);

  // Authorized clusters come from VMListResponse.nodes, including clusters with zero rows.
  // Pending hosts remain connected until their durable create verdict is known.
  const hosts = getInstanceStreamHosts(
    authorizedHosts,
    pending.map((entry) => entry.host),
  );
  const hostsKey = hosts.slice().sort().join(",");

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const readFence = createExactInstanceReadFence();
    const conns = new Map<string, Conn>();
    const reconnectTimers = new Set<ReturnType<typeof setTimeout>>();
    const ipRetryTimers = new Set<ReturnType<typeof setTimeout>>();

    const visible = () =>
      typeof document === "undefined" || document.visibilityState === "visible";
    const allStreamsHealthy = () =>
      hosts.length === 0 || hosts.every((host) => conns.get(host)?.healthy === true);
    const freshness = createInstanceFreshnessCoordinator({
      actorScope: scopeKey,
      isVisible: visible,
      areAllStreamsHealthy: allStreamsHealthy,
      reconcile: async ({ actorScope, fresh }) => {
        await reconcileInstanceFleet(queryClient, actorScope, {
          fresh,
          signal: controller.signal,
        });
      },
      classifyRateLimit: (error) =>
        error instanceof APIError && error.status === 429
          ? { retryAfterSeconds: error.retryAfter }
          : null,
      retainRateLimit: retainInstanceReconcileRateLimit,
    });
    const scheduleReconcile = freshness.requestReconcile;

    const findPending = (host: string, name: string, project?: string | null) => {
      const matches = getPendingSnapshot(scopeKey).filter(
        (entry) =>
          entry.host === host &&
          entry.name === name &&
          (!project || (entry.project || "default") === project),
      );
      return matches.length === 1 ? matches[0] : undefined;
    };

    const fetchAndUpsert = async (
      host: string,
      name: string,
      project: string,
      ipRetries = 6,
    ): Promise<boolean> => {
      const identity = { actorScope: scopeKey, host, project, name };
      const lease = readFence.start(identity);
      try {
        const vm = await api.instances.get(name, host, project, {
          signal: lease.signal,
        });
        if (!active || !readFence.isCurrent(lease)) return false;
        if (!upsertExactInstance(queryClient, identity, vm)) {
          scheduleReconcile(true);
          return false;
        }
        const needsIp =
          vm.status === "Running" &&
          (!Array.isArray(vm.ip_addresses) || vm.ip_addresses.length === 0);
        if (needsIp && ipRetries > 0) {
          const timer = setTimeout(() => {
            ipRetryTimers.delete(timer);
            if (active && readFence.isCurrent(lease)) {
              void fetchAndUpsert(host, name, project, ipRetries - 1);
            }
          }, 4_000);
          ipRetryTimers.add(timer);
        }
        return true;
      } catch {
        if (active && readFence.isCurrent(lease)) scheduleReconcile(true);
        return false;
      } finally {
        readFence.release(lease);
      }
    };

    const dropPending = (host: string, name: string, project: string) => {
      const entry = findPending(host, name, project);
      if (!entry) return;
      if (entry.jobId && entry.jobStatus !== "success") {
        updatePendingCreate(scopeKey, entry.id, { lastSignalAt: Date.now() });
      } else {
        removePendingCreate(scopeKey, entry.id);
      }
    };

    const settle = (host: string, name: string, project: string, ok: boolean) => {
      if (!ok) return;
      const entry = findPending(host, name, project);
      if (!entry) return;
      if (entry.jobId && entry.jobStatus !== "success") {
        updatePendingCreate(scopeKey, entry.id, { lastSignalAt: Date.now() });
      } else {
        resolvePendingCreate(scopeKey, entry.id);
      }
    };

    const handleOperation = (host: string, message: Record<string, unknown>) => {
      const names = Array.isArray(message.instances)
        ? message.instances.filter((name): name is string => typeof name === "string")
        : typeof message.instance === "string"
          ? [message.instance]
          : [];
      const project = typeof message.project === "string" ? message.project : undefined;
      if (names.length === 0) return;
      for (const entry of getPendingSnapshot(scopeKey)) {
        if (entry.host !== host || !names.includes(entry.name)) continue;
        if (project && (entry.project || "default") !== project) continue;
        updatePendingCreate(scopeKey, entry.id, { lastSignalAt: Date.now() });
        const code = typeof message.status_code === "number" ? message.status_code : undefined;
        if (code !== undefined && code >= STATUS_SUCCESS) {
          if (code === STATUS_SUCCESS) {
            void fetchAndUpsert(host, entry.name, entry.project || "default").then((ok) =>
              settle(host, entry.name, entry.project || "default", ok),
            );
            continue;
          }
          const reason = typeof message.err === "string" ? message.err : "Creation failed";
          if (entry.jobId) {
            updatePendingCreate(scopeKey, entry.id, {
              lastSignalAt: Date.now(),
              statusCode: code,
              description: typeof message.description === "string"
                ? message.description
                : "Waiting for exact create job status",
              operationId: typeof message.operation_id === "string"
                ? message.operation_id
                : entry.operationId,
            });
            continue;
          }
          const alreadyFailed = entry.status === "Error";
          updatePendingCreate(scopeKey, entry.id, {
            status: "Error",
            statusCode: code,
            error: reason,
          });
          if (!alreadyFailed) {
            toast({
              variant: "destructive",
              title: `Could not create ${entry.name}`,
              description: reason,
            });
          }
          continue;
        }
        updatePendingCreate(scopeKey, entry.id, {
          lastSignalAt: Date.now(),
          status: typeof message.status === "string" ? message.status : "Running",
          statusCode: code,
          progress:
            typeof message.progress === "string" || typeof message.progress === "number"
              ? String(message.progress)
              : undefined,
          description: typeof message.description === "string" ? message.description : undefined,
          operationId: typeof message.operation_id === "string"
            ? message.operation_id
            : entry.operationId,
        });
      }
    };

    const handleLifecycle = (host: string, message: Record<string, unknown>) => {
      const name = typeof message.instance === "string" ? message.instance : undefined;
      const project = typeof message.project === "string" ? message.project : undefined;
      if (!name) return;
      if (message.action === "instance-deleted") {
        if (!project) {
          readFence.tombstoneName(scopeKey, host, name);
          scheduleReconcile(true);
          return;
        }
        const identity = { actorScope: scopeKey, host, project, name };
        readFence.tombstone(identity);
        removeExactInstance(queryClient, identity);
        dropPending(host, name, project);
        return;
      }
      if (message.action === "instance-renamed" || !project) {
        if (project) {
          readFence.tombstone({ actorScope: scopeKey, host, project, name });
        } else {
          readFence.tombstoneName(scopeKey, host, name);
        }
        const entry = findPending(host, name, project);
        if (entry) updatePendingCreate(scopeKey, entry.id, { lastSignalAt: Date.now() });
        scheduleReconcile(true);
        return;
      }
      const entry = findPending(host, name, project);
      if (entry) updatePendingCreate(scopeKey, entry.id, { lastSignalAt: Date.now() });
      void fetchAndUpsert(host, name, project).then((ok) => settle(host, name, project, ok));
    };

    const handleMessage = (host: string, raw: string) => {
      if (!active) return;
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        return;
      }
      if (message.type === "operation") handleOperation(host, message);
      else if (message.type === "lifecycle") handleLifecycle(host, message);
    };

    const connect = (host: string) => {
      const conn = conns.get(host);
      if (!active || !conn || conn.stop) return;
      const ws = new WebSocket(getEventsWsUrl(host));
      conn.ws = ws;
      conn.healthy = false;
      ws.onopen = () => {
        conn.healthy = true;
        // Every initial-open/reconnect burst gets one actor-wide authoritative snapshot.
        freshness.streamOpened();
      };
      ws.onmessage = (event) => handleMessage(host, event.data as string);
      ws.onclose = (event) => {
        conn.ws = null;
        conn.healthy = false;
        if (event.code === 1008) {
          // Policy violation means the server revoked this actor's scope. Re-read the
          // authorized fleet immediately and do not hammer a socket we may no longer open.
          scheduleReconcile(true);
          return;
        }
        if (active && !conn.stop) {
          const timer = setTimeout(() => {
            reconnectTimers.delete(timer);
            if (active) connect(host);
          }, RECONNECT_MS);
          reconnectTimers.add(timer);
        }
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          // The close path is best effort; the safety read still reconciles the fleet.
        }
      };
    };

    const closeHost = (host: string) => {
      const conn = conns.get(host);
      if (!conn) return;
      conn.stop = true;
      if (conn.ws) {
        conn.ws.onopen = null;
        conn.ws.onmessage = null;
        conn.ws.onclose = null;
        conn.ws.onerror = null;
      }
      try {
        conn.ws?.close();
      } catch {
        // The socket may already be gone.
      }
      conns.delete(host);
    };

    freshness.start();
    for (const host of hosts) {
      conns.set(host, { ws: null, stop: false, healthy: false });
      connect(host);
    }

    const handleVisibility = freshness.visibilityChanged;
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      active = false;
      controller.abort();
      readFence.abortAll();
      freshness.stop();
      reconnectTimers.forEach(clearTimeout);
      ipRetryTimers.forEach(clearTimeout);
      document.removeEventListener("visibilitychange", handleVisibility);
      for (const host of Array.from(conns.keys())) closeHost(host);
    };
  }, [hostsKey, queryClient, scopeKey]); // eslint-disable-line react-hooks/exhaustive-deps

  return pending;
}
