/**
 * useInstancePower - start/stop/restart lifecycle for a single instance from the
 * console pages. Owns the transient instance status, the confirm dialog for
 * destructive actions, and the initial status fetch. Keeps the console pages
 * thin and avoids duplicating the operation flow.
 */

"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "@/hooks/use-toast";
import { API_URL, getAccessToken } from "@/lib/api";

export type InstanceStatus =
  | "Running" | "Stopped" | "Starting" | "Stopping" | "Restarting" | "Unknown" | "Error";
export type ConsoleOperation = "start" | "stop" | "restart";

interface ConfirmDialog {
  open: boolean;
  operation: ConsoleOperation | null;
  title: string;
  description: string;
}

interface Options {
  instanceName: string;
  host: string;
  project: string;
  onAfterRestart?: () => void;
}

const TRANSITION: Record<ConsoleOperation, InstanceStatus> = {
  start: "Starting",
  stop: "Stopping",
  restart: "Restarting",
};

const RESULT_STATUS: Record<string, InstanceStatus> = {
  running: "Running", stopped: "Stopped", Running: "Running", Stopped: "Stopped",
};

export function useInstancePower({ instanceName, host, project, onAfterRestart }: Options) {
  const [instanceStatus, setInstanceStatus] = useState<InstanceStatus>("Running");
  const [operationLoading, setOperationLoading] = useState<ConsoleOperation | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialog>({
    open: false,
    operation: null,
    title: "",
    description: "",
  });

  // Initial status so Start/Stop buttons start correct.
  useEffect(() => {
    if (!instanceName || !host) return;
    const fetchStatus = async () => {
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/capabilities` +
            `?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
          { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } },
        );
        if (response.ok) {
          const data = await response.json();
          const state = data.state ?? "Unknown";
          if (state && state !== "Unknown") setInstanceStatus(state as InstanceStatus);
        }
      } catch (err) {
        console.warn("[Console] Failed to fetch instance status:", err);
      }
    };
    fetchStatus();
  }, [instanceName, host, project]);

  const executeOperation = useCallback(
    async (operation: ConsoleOperation) => {
      setOperationLoading(operation);
      setInstanceStatus(TRANSITION[operation]);
      try {
        const response = await fetch(`${API_URL}/instances/${encodeURIComponent(instanceName)}/${operation}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
          body: JSON.stringify({ host, force: false, timeout: 30 }),
        });
        if (!response.ok) {
          const err = await response.json().catch(() => ({ detail: "Unknown error" }));
          throw new Error(err.detail || `Failed to ${operation} instance`);
        }
        const result = await response.json();
        const newStatus = RESULT_STATUS[result.new_status] || "Unknown";
        setInstanceStatus(newStatus);
        toast({ title: `Instance ${operation}ed successfully`, description: `${instanceName} is now ${newStatus.toLowerCase()}` });
        if (operation === "restart") setTimeout(() => onAfterRestart?.(), 2000);
      } catch (error) {
        setInstanceStatus("Error");
        toast({
          title: `Failed to ${operation} instance`,
          description: error instanceof Error ? error.message : "Unknown error",
          variant: "destructive",
        });
      } finally {
        setOperationLoading(null);
      }
    },
    [instanceName, host, onAfterRestart],
  );

  const requestOperation = useCallback(
    (operation: ConsoleOperation) => {
      if (operation === "start") {
        executeOperation(operation);
        return;
      }
      const cfg = {
        stop: { title: `Stop ${instanceName}?`, description: "This will gracefully stop the instance. The console connection will be lost." },
        restart: { title: `Restart ${instanceName}?`, description: "This will restart the instance. The console will attempt to reconnect after restart." },
      } as const;
      setConfirmDialog({ open: true, operation, ...cfg[operation as "stop" | "restart"] });
    },
    [instanceName, executeOperation],
  );

  const confirm = useCallback(() => {
    if (confirmDialog.operation) executeOperation(confirmDialog.operation);
    setConfirmDialog((prev) => ({ ...prev, open: false }));
  }, [confirmDialog.operation, executeOperation]);

  const setConfirmOpen = useCallback((open: boolean) => {
    setConfirmDialog((prev) => ({ ...prev, open }));
  }, []);

  const isTransitioning = ["Starting", "Stopping", "Restarting"].includes(instanceStatus);
  const showStart = instanceStatus === "Stopped" || instanceStatus === "Unknown" || instanceStatus === "Error";
  const showRestartStop = instanceStatus === "Running" || instanceStatus === "Unknown" || instanceStatus === "Error";

  return {
    instanceStatus,
    operationLoading,
    confirmDialog,
    setConfirmOpen,
    requestOperation,
    confirm,
    showStart,
    showRestartStop,
    isOperationDisabled: isTransitioning || operationLoading !== null,
  };
}
