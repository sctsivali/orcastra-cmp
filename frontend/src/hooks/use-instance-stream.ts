"use client";

/**
 * useInstanceStream
 *
 * Keeps the instances list current from the LXD events stream instead of
 * polling. The page fetches the initial snapshot once (useInstances with
 * `realtime: true`), then this hook opens one WebSocket per relevant host
 * (`/api/v1/events/ws?host=…`; the backend shares a single upstream LXD events
 * connection per node) and applies changes directly to the React Query cache:
 *
 *   - lifecycle `instance-deleted`            → remove the row from the cache
 *   - lifecycle `instance-renamed`            → refetch (rare; name changed)
 *   - lifecycle `instance-*` (created/started/stopped/restarted/updated/…)
 *                                             → fetch that one instance + upsert
 *   - operation events                        → drive the "Creating…" placeholder
 *                                               (progress / error); on success the
 *                                               instance-created lifecycle event
 *                                               upserts the real row, so the swap
 *                                               is atomic (no disappear/reappear).
 *
 * Watches the union of the hosts that currently have instances plus the hosts of
 * any pending creates. Returns the live pending-create list for rendering.
 */
import { useEffect, useRef, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { getEventsWsUrl } from "@/lib/ws-url";
import {
  PendingCreate,
  getPendingSnapshot,
  removePendingCreate,
  subscribePending,
  updatePendingCreate,
} from "@/lib/pending-creates";

// LXD operation status codes: 200 = Success, 400 = Failure, 401 = Cancelled.
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
  const pending = useSyncExternalStore(
    subscribePending,
    getPendingSnapshot,
    getPendingSnapshot,
  );
  const queryClient = useQueryClient();
  const connsRef = useRef<Map<string, Conn>>(new Map());

  const hosts = Array.from(
    new Set([...instanceHosts, ...pending.map((p) => p.host)]),
  ).filter(Boolean);
  const hostsKey = hosts.slice().sort().join(",");
  // Live view of wanted hosts for the reconnect closure (avoids stale capture).
  const hostsRef = useRef<string[]>(hosts);
  hostsRef.current = hosts;

  useEffect(() => {
    const conns = connsRef.current;

    const upsertInstance = (vm: any) => {
      queryClient.setQueriesData({ queryKey: ["instances"] }, (old: any) => {
        if (!old || !Array.isArray(old.vms)) return old;
        const idx = old.vms.findIndex((i: any) =>
          sameInstance(i, vm.host, vm.name, vm.project),
        );
        const vms =
          idx >= 0
            ? old.vms.map((i: any, k: number) => (k === idx ? vm : i))
            : [vm, ...old.vms];
        return { ...old, vms, total: vms.length };
      });
    };

    const removeInstance = (host: string, name: string, project?: string) => {
      queryClient.setQueriesData({ queryKey: ["instances"] }, (old: any) => {
        if (!old || !Array.isArray(old.vms)) return old;
        const vms = old.vms.filter(
          (i: any) => !sameInstance(i, host, name, project),
        );
        if (vms.length === old.vms.length) return old;
        return { ...old, vms, total: vms.length };
      });
    };

    // Fetch one instance and upsert it. A just-started instance is "Running"
    // before its DHCP lease lands, so its IPs are briefly empty; in that case we
    // re-fetch a few times (bounded, event-triggered — not a return to polling)
    // until the address appears or we give up.
    // note: the event names its project, and passing it on reads that instance rather than
    // the first same-named one the caller may see
    const fetchAndUpsert = async (
      host: string,
      name: string,
      project?: string,
      ipRetries = 6,
    ) => {
      try {
        const vm: any = await api.instances.get(name, host, project);
        upsertInstance(vm);
        const needsIp =
          vm.status === "Running" &&
          (!Array.isArray(vm.ip_addresses) || vm.ip_addresses.length === 0);
        if (needsIp && ipRetries > 0) {
          setTimeout(() => {
            // Only keep retrying while the row is still present.
            void fetchAndUpsert(host, name, project, ipRetries - 1);
          }, 4000);
        }
      } catch {
        // Instance gone or a transient race — ignore.
      }
    };

    const dropPending = (host: string, name: string) => {
      const p = getPendingSnapshot().find(
        (x) => x.host === host && x.name === name,
      );
      if (p) removePendingCreate(p.id);
    };

    const handleOperation = (host: string, msg: any) => {
      const names: string[] = Array.isArray(msg.instances)
        ? msg.instances
        : msg.instance
          ? [msg.instance]
          : [];
      if (names.length === 0) return;
      for (const p of getPendingSnapshot()) {
        if (p.host !== host || !names.includes(p.name)) continue;
        const code: number | undefined =
          typeof msg.status_code === "number" ? msg.status_code : undefined;
        if (code !== undefined && code >= STATUS_SUCCESS) {
          if (code === STATUS_SUCCESS) {
            // Created: pull the real row in, then drop the placeholder.
            void fetchAndUpsert(host, p.name, msg.project || p.project).finally(() =>
              dropPending(host, p.name),
            );
          } else {
            updatePendingCreate(p.id, {
              status: "Error",
              statusCode: code,
              error: msg.err || "Creation failed",
            });
            setTimeout(() => removePendingCreate(p.id), 8000);
          }
        } else {
          updatePendingCreate(p.id, {
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
      const name: string | undefined = msg.instance;
      if (!name) return;
      const action: string = msg.action || "";
      if (action === "instance-deleted") {
        removeInstance(host, name, msg.project);
        dropPending(host, name);
        return;
      }
      if (action === "instance-renamed") {
        // Name changed — cheapest correct move is a single refetch.
        queryClient.invalidateQueries({ queryKey: ["instances"] });
        return;
      }
      // created / started / stopped / restarted / updated / paused / resumed …
      void fetchAndUpsert(host, name, msg.project).finally(() => dropPending(host, name));
    };

    const handleMessage = (host: string, raw: string) => {
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
      if (!conn || conn.stop) return;
      const ws = new WebSocket(getEventsWsUrl(host));
      conn.ws = ws;
      ws.onmessage = (ev) => handleMessage(host, ev.data as string);
      ws.onclose = () => {
        conn.ws = null;
        if (!conn.stop && hostsRef.current.includes(host)) {
          setTimeout(() => connect(host), 2000);
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
  }, [hostsKey, queryClient]); // eslint-disable-line react-hooks/exhaustive-deps

  // Close everything on unmount.
  useEffect(() => {
    const conns = connsRef.current;
    return () => {
      for (const conn of conns.values()) {
        conn.stop = true;
        try {
          conn.ws?.close();
        } catch {
          /* ignore */
        }
      }
      conns.clear();
    };
  }, []);

  return pending;
}
