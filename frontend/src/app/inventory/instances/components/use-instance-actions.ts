"use client";

import { useRef, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import {
  useStartInstance,
  useStopInstance,
  useRestartInstance,
  useDeleteInstance,
} from "@/hooks/use-lxd";
import type { VMInfo } from "@/types/api";
import type { ConfirmActionState } from "./instance-action-dialogs";

export type InstanceAction = "start" | "stop" | "restart";

/**
 * The instance list's row actions: the mutations, their toasts, and the targets the shared
 * dialogs are controlled by. The page keeps the list, the filters and the layout; a row only
 * needs what this returns.
 */
export function useInstanceActions({ refetch }: { refetch: () => unknown }) {
  const startInstance = useStartInstance();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();

  const [actionLoading, setActionLoading] = useState<string | null>(null);
  // important: actionLoading and isPending are React state and only reach the DOM on the next
  // render, so two activations inside one tick both pass. The ref is what actually blocks the
  // second request. Same recipe as snapshots/components/snapshot-row-actions.tsx, keyed by
  // instance and action rather than one boolean: this hook serves every row of the table, and
  // a single flag would drop a Stop on one row while another row's Start was still awaiting
  // its response. Only the same action on the same instance is a repeat.
  const actionsInFlightRef = useRef<Set<string>>(new Set());
  // Instance pending delete confirmation (controls the shared AlertDialog).
  const [deleteTarget, setDeleteTarget] = useState<VMInfo | null>(null);
  // Instance pending rebuild (controls the shared RebuildDialog).
  const [rebuildTarget, setRebuildTarget] = useState<VMInfo | null>(null);
  // Instance pending a copy to another project, member or cluster.
  const [copyTarget, setCopyTarget] = useState<VMInfo | null>(null);
  // Radix keeps AlertDialogContent mounted through its ~200ms exit fade, so
  // reading a just-nulled deleteTarget flashes blank name/host. Keep the last
  // target around and render the dialog body from it so it stays populated
  // during that fade (on both the Delete and Cancel paths).
  const deleteDisplayRef = useRef<VMInfo | null>(null);
  if (deleteTarget) deleteDisplayRef.current = deleteTarget;
  const deleteDisplay = deleteTarget ?? deleteDisplayRef.current;
  // Confirmation for stop/restart, with a Force option (power-cycle) for guests
  // that can't answer a graceful ACPI shutdown, e.g. a VM with no OS installed.
  const [confirmAction, setConfirmAction] = useState<ConfirmActionState>({
    open: false,
    action: null,
    name: "",
    host: "",
    project: "",
    force: false,
  });

  // Entry point for the row action buttons. Start fires immediately; stop and
  // restart open a confirm dialog so the user can choose a graceful or forced
  // (power-cycle) operation.
  const handleAction = (
    instanceName: string,
    host: string,
    project: string,
    action: "start" | "stop" | "restart"
  ) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    if (action === "start") {
      runAction(instanceName, host, project, action);
      return;
    }
    setConfirmAction({ open: true, action, name: instanceName, host, project, force: false });
  };

  const runAction = async (
    instanceName: string,
    host: string,
    project: string,
    action: "start" | "stop" | "restart",
    force = false
  ) => {
    const key = `${host}/${project}/${instanceName}:${action}`;
    if (actionsInFlightRef.current.has(key)) return;
    actionsInFlightRef.current.add(key);
    try {
      setActionLoading(`${instanceName}-${action}`);

      if (action === "start") {
        await startInstance.mutateAsync({ name: instanceName, host, project });
      } else if (action === "stop") {
        await stopInstance.mutateAsync({ name: instanceName, host, project, force });
      } else {
        await restartInstance.mutateAsync({ name: instanceName, host, project, force });
      }

      toast({
        title: "Success",
        description: `Instance ${instanceName} ${action} initiated`,
      });

      // Refresh instances after action
      setTimeout(() => refetch(), 2000);
    } catch (error) {
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : `Failed to ${action} instance`,
        variant: "destructive",
      });
    } finally {
      setActionLoading(null);
      actionsInFlightRef.current.delete(key);
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    const key = `${target.host}/${target.project || "default"}/${target.name}:delete`;
    if (actionsInFlightRef.current.has(key)) return;
    actionsInFlightRef.current.add(key);
    try {
      await deleteInstance.mutateAsync({
        name: target.name,
        host: target.host,
        force: true, // stops a running instance before deleting
        project: target.project || "default",
      });
      toast({
        title: "Instance deleted",
        description: `"${target.name}" has been deleted.`,
      });
    } catch (err) {
      toast({
        title: "Failed to delete instance",
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
    } finally {
      setDeleteTarget(null);
      actionsInFlightRef.current.delete(key);
    }
  };

  const confirmActionRun = () => {
    if (confirmAction.action) {
      runAction(
        confirmAction.name,
        confirmAction.host,
        confirmAction.project,
        confirmAction.action,
        confirmAction.force,
      );
    }
    setConfirmAction((prev) => ({ ...prev, open: false }));
  };

  /** Whether the shared delete is running for this row, so its own control can show it. */
  const deletePending = (instance: Pick<VMInfo, "name" | "host">) =>
    deleteInstance.isPending &&
    deleteTarget?.name === instance.name &&
    deleteTarget?.host === instance.host;

  return {
    isReadOnly,
    actionLoading,
    deleteTarget,
    setDeleteTarget,
    deleteDisplay,
    deleteInFlight: deleteInstance.isPending,
    deletePending,
    rebuildTarget,
    setRebuildTarget,
    copyTarget,
    setCopyTarget,
    confirmAction,
    setConfirmAction,
    handleAction,
    handleDeleteConfirm,
    confirmActionRun,
  };
}
