/**
 * WebSocket-backed monitoring hook.
 * Receives real-time dashboard pushes from the backend metrics collector.
 * Falls back to REST polling when WS is unavailable.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { API_URL } from "@/lib/api";
import type { MonitoringDashboardResponse } from "@/types";

const WS_RECONNECT_BASE_MS = 2000;
const WS_RECONNECT_MAX_MS = 30000;

/**
 * Hook that subscribes to the monitoring WebSocket.
 * When data arrives it injects it into the React Query cache under
 * ["monitoring", "dashboard"], keeping the existing query consumers in sync.
 *
 * Returns { connected } so callers know when to enable polling fallback.
 */
export function useMonitoringWebSocket(enabled: boolean = true) {
  const queryClient = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const attemptRef = useRef(0);
  const [connected, setConnected] = useState(false);

  const connect = useCallback(() => {
    if (!enabled) return;

    // Build WS URL from the API base (http→ws, https→wss)
    const base = API_URL.replace(/\/api\/v1$/, "");
    const wsBase = base.replace(/^http/, "ws");
    const wsUrl = `${wsBase}/api/v1/monitoring/ws`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const data: MonitoringDashboardResponse = JSON.parse(event.data);
          // Inject into React Query cache - all useMonitoringDashboard consumers update instantly
          queryClient.setQueryData(["monitoring", "dashboard"], data);
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        setConnected(false);
        scheduleReconnect();
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch {
      scheduleReconnect();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, queryClient]);

  const scheduleReconnect = useCallback(() => {
    if (!enabled) return;
    const delay = Math.min(
      WS_RECONNECT_BASE_MS * 2 ** attemptRef.current,
      WS_RECONNECT_MAX_MS,
    );
    attemptRef.current += 1;
    reconnectTimer.current = setTimeout(connect, delay);
  }, [enabled, connect]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
      setConnected(false);
    };
  }, [connect]);

  return { connected };
}
