/**
 * WebSocket-backed monitoring hook.
 * Receives real-time dashboard pushes from the backend metrics collector.
 * Falls back to REST polling when WS is unavailable.
 *
 * note: every connect mints its own single-use ticket. A failed mint backs off like a failed
 * handshake, and a 4401 (the session ended) reconnects at the base delay under the new session.
 */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { getActorQueryScope } from "@/hooks/use-query-scope";
import { WS_CLOSE, ticketedSocketUrl } from "@/lib/ws-url";
import { Role, resolveRoles } from "@/types/rbac";
import type { MonitoringDashboardResponse } from "@/types";

const WS_RECONNECT_BASE_MS = 2000;
const WS_RECONNECT_MAX_MS = 30000;

/**
 * Hook that subscribes to the monitoring WebSocket.
 * When data arrives it injects it into the React Query cache under
 * the current actor's monitoring dashboard key, keeping the existing query consumers in sync.
 *
 * Returns { connected } so callers know when to enable polling fallback.
 */
export function useMonitoringWebSocket(enabled: boolean = true) {
  const queryClient = useQueryClient();
  const { data: session } = useSession();
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);

  const user = session?.user as any;
  const groups = Array.isArray(user?.groups) ? user.groups : [];
  const roles = resolveRoles(groups);
  const isAdmin = roles.includes(Role.ADMIN);
  const scopeKey = getActorQueryScope(session);

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;

    setConnected(false);
    if (!enabled || !isAdmin) {
      return () => {
        active = false;
      };
    }

    const clearReconnect = () => {
      if (reconnectTimer !== undefined) {
        clearTimeout(reconnectTimer);
        reconnectTimer = undefined;
      }
    };

    const scheduleReconnect = () => {
      if (!active) return;
      clearReconnect();
      const delay = Math.min(
        WS_RECONNECT_BASE_MS * 2 ** attempt,
        WS_RECONNECT_MAX_MS,
      );
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = undefined;
        if (active) connect();
      }, delay);
    };

    const connect = async (): Promise<void> => {
      if (!active) return;
      let wsUrl: string;
      try {
        wsUrl = await ticketedSocketUrl({ kind: "monitoring" });
      } catch {
        scheduleReconnect();
        return;
      }
      // The effect may have been torn down while the ticket was minted.
      if (!active) return;

      try {
        const ws = new WebSocket(wsUrl);
        socket = ws;
        wsRef.current = ws;

        ws.onopen = () => {
          if (!active || socket !== ws) return;
          attempt = 0;
          setConnected(true);
        };

        ws.onmessage = (event) => {
          if (!active || socket !== ws) return;
          try {
            const data: MonitoringDashboardResponse = JSON.parse(event.data);
            // Inject into React Query cache - all useMonitoringDashboard consumers update instantly
            queryClient.setQueryData(["monitoring", "dashboard", scopeKey], data);
          } catch {
            // ignore malformed messages
          }
        };

        ws.onclose = (event) => {
          if (!active || socket !== ws) return;
          socket = null;
          if (wsRef.current === ws) wsRef.current = null;
          setConnected(false);
          if (event.code === WS_CLOSE.SESSION_ENDED) attempt = 0;
          scheduleReconnect();
        };

        ws.onerror = () => {
          if (active && socket === ws) ws.close();
        };
      } catch {
        scheduleReconnect();
      }
    };

    void connect();

    return () => {
      active = false;
      clearReconnect();
      const ws = socket;
      socket = null;
      if (wsRef.current === ws) wsRef.current = null;
      if (ws) {
        // Closing is intentional during Pause, role/session changes, and unmount. Remove the
        // old callbacks first so a late close event cannot arm another reconnect timer.
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        try {
          ws.close();
        } catch {
          // The socket may already have completed its close handshake.
        }
      }
    };
  }, [enabled, isAdmin, queryClient, scopeKey]);

  return { connected };
}
