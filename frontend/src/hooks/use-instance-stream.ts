"use client";

/** Keeps inventory current from daemon events, durable jobs, and a bounded safety read. */
import { useCallback, useEffect, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useActorQueryScope } from "@/hooks/use-query-scope";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import {
  createInstanceFreshnessCoordinator,
  getInstanceStreamHosts,
} from "@/lib/instance-freshness";
import {
  createExactInstanceReadFence,
  isHiddenByPendingDelete,
  noteInstanceCreatedEvent,
  noteInstanceDeletedEvent,
  readClock,
  reconcileInstanceFleet,
  removeExactInstance,
  retainInstanceReconcileRateLimit,
  upsertExactInstance,
} from "@/lib/instance-reconciliation";
import { WS_CLOSE, ticketedSocketUrl } from "@/lib/ws-url";
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
const RECONNECT_MAX_MS = 30_000;

interface Conn {
  ws: WebSocket | null;
  stop: boolean;
  healthy: boolean;
  /** Connects in a row that never opened or mint attempts that failed; reset on open. */
  failures: number;
}

const reconnectDelay = (failures: number): number =>
  Math.min(RECONNECT_MS * 2 ** Math.max(0, failures - 1), RECONNECT_MAX_MS);

export function useInstanceStream(authorizedHosts: string[]): PendingCreate[] {
  const { scopeKey } = useActorQueryScope();
  const subscribe = useCallback(
    (listener: () => void) => subscribePending(scopeKey, listener),
    [scopeKey],
  );
  const getSnapshot = useCallback(() => getPendingSnapshot(scopeKey), [scopeKey]);
  const pending = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const queryClient = useQueryClient();
  // The durable create jobs are followed app-wide (components/pending-create-follower).

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
      const readStartedAt = readClock();
      try {
        const vm = await api.instances.get(name, host, project, {
          signal: lease.signal,
        });
        if (!active || !readFence.isCurrent(lease)) return false;
        // A delete owns this row: nothing to patch and no address to wait for.
        if (isHiddenByPendingDelete(queryClient, identity, readStartedAt)) return true;
        if (!upsertExactInstance(queryClient, identity, vm, readStartedAt)) {
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
      // A create still in flight keeps its placeholder until the job decides. Once the job has
      // a verdict, success or not, a deleted instance leaves nothing to show; a placeholder kept
      // here would survive reloads in session storage.
      if (entry.jobId && (!entry.jobStatus || ["pending", "running"].includes(entry.jobStatus))) {
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
        // A list read made in the next moments can still come from a cache the writer has not
        // cleared yet (another tab, another operator, OrcaHub): it must not bring the row back.
        noteInstanceDeletedEvent(queryClient, identity);
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
      if (message.action === "instance-created") {
        noteInstanceCreatedEvent(queryClient, { actorScope: scopeKey, host, project, name });
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

    const reconnectLater = (host: string, conn: Conn): void => {
      if (!active || conn.stop) return;
      const timer = setTimeout(() => {
        reconnectTimers.delete(timer);
        if (active) void connect(host);
      }, reconnectDelay(conn.failures));
      reconnectTimers.add(timer);
    };

    // Every connect mints its own single-use ticket. A refused mint (403) is the same
    // signal as a 1008; any other failed mint backs off like a failed handshake.
    const connect = async (host: string): Promise<void> => {
      const conn = conns.get(host);
      if (!active || !conn || conn.stop) return;
      let url: string;
      try {
        url = await ticketedSocketUrl({ kind: "events", host });
      } catch (error) {
        if (error instanceof APIError && error.status === 403) {
          scheduleReconcile(true);
          return;
        }
        conn.failures += 1;
        reconnectLater(host, conn);
        return;
      }
      // The host may have been closed, or the effect torn down, while the ticket was minted.
      if (!active || conn.stop || conns.get(host) !== conn) return;
      const ws = new WebSocket(url);
      conn.ws = ws;
      conn.healthy = false;
      ws.onopen = () => {
        conn.healthy = true;
        conn.failures = 0;
        // Every initial-open/reconnect burst gets one actor-wide authoritative snapshot.
        freshness.streamOpened();
      };
      ws.onmessage = (event) => handleMessage(host, event.data as string);
      ws.onclose = (event) => {
        const opened = conn.healthy;
        conn.ws = null;
        conn.healthy = false;
        if (event.code === 1008) {
          // Policy violation: the server revoked this actor's scope. Re-read the authorized
          // fleet now and do not reconnect to a socket this actor may not be allowed to open.
          scheduleReconcile(true);
          return;
        }
        if (event.code === WS_CLOSE.SESSION_ENDED) {
          // The session the ticket was minted under ended. Catch up from the cached read and
          // reopen under the current session; the open itself asks for an authoritative one.
          scheduleReconcile(false);
          conn.failures = 0;
        } else if (!opened) {
          conn.failures += 1;
        }
        reconnectLater(host, conn);
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
      conns.set(host, { ws: null, stop: false, healthy: false, failures: 0 });
      void connect(host);
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
