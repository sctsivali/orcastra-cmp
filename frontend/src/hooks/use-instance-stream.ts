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
import { toast } from "@/hooks/use-toast";
import {
  PendingCreate,
  getPendingSnapshot,
  removePendingCreate,
  resolvePendingCreate,
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

    // A list is keyed ["instances", host, project] with "*" for "every" (queryKeys.instances);
    // a row only belongs in the lists whose scope contains it, or a project-scoped list would
    // gain every other project's creates.
    const scopeHolds = (key: readonly unknown[], vm: any) => {
      const [, host, project] = key as [string, string | undefined, string | undefined];
      const hostOk = host === undefined || host === "*" || host === vm.host;
      const projectOk = project === undefined || project === "*" || project === (vm.project || "default");
      return hostOk && projectOk;
    };

    const upsertInstance = (vm: any) => {
      queryClient.setQueriesData({ queryKey: ["instances"], predicate: (q) => scopeHolds(q.queryKey, vm) }, (old: any) => {
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
        return true;
      } catch {
        // Instance gone, or a transient race. The list never polls in realtime mode, so a
        // refetch is the one thing that can still carry the row in.
        void queryClient.invalidateQueries({ queryKey: ["instances"] });
        return false;
      }
    };

    // The placeholder a frame is about. A frame that names its project matches on it too; a
    // frame without one (older daemons) matches on host and name as before.
    const findPending = (host: string, name: string, project?: string | null) =>
      getPendingSnapshot().find(
        (x) =>
          x.host === host &&
          x.name === name &&
          (!project || (x.project || "default") === project),
      );

    const dropPending = (host: string, name: string, project?: string | null) => {
      const p = findPending(host, name, project);
      if (p) removePendingCreate(p.id);
    };

    // A placeholder retires only once the real row is in the cache. It used to be dropped in a
    // `finally`, so a GET that failed removed the row and upserted nothing, on a page that does
    // not poll: the create had succeeded and the list showed neither.
    const settle = (host: string, name: string, project: string | null | undefined, ok: boolean) => {
      if (!ok) return;
      const p = findPending(host, name, project);
      if (p) resolvePendingCreate(p.id);
    };

    const handleOperation = (host: string, msg: any) => {
      const names: string[] = Array.isArray(msg.instances)
        ? msg.instances
        : msg.instance
          ? [msg.instance]
          : [];
      if (names.length === 0) return;
      const project: string | undefined = msg.project || undefined;
      for (const p of getPendingSnapshot()) {
        if (p.host !== host || !names.includes(p.name)) continue;
        if (project && (p.project || "default") !== project) continue;
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
            // LXD can emit more than one frame for the same failed operation, so only the
            // transition into Error raises a toast. Without this the operator gets the same
            // destructive toast several times for one create.
            const alreadyFailed = p.status === "Error";
            updatePendingCreate(p.id, {
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
        dropPending(host, name, msg.project);
        return;
      }
      if (action === "instance-renamed") {
        // Name changed. The cheapest correct move is a single refetch.
        queryClient.invalidateQueries({ queryKey: ["instances"] });
        return;
      }
      // created / started / stopped / restarted / updated / paused / resumed …
      const project: string | undefined = msg.project || undefined;
      void fetchAndUpsert(host, name, project).then((ok) => settle(host, name, project, ok));
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
