"use client";

import { useEffect, useRef, useState } from "react";
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
import { useActorQueryScope } from "@/hooks/use-query-scope";

export type InstanceAction = "start" | "stop" | "restart";
const NO_PENDING_ACTIONS: ReadonlySet<string> = new Set();

/**
 * The instance list's row actions: the mutations, their toasts, and the targets the shared
 * dialogs are controlled by. The page keeps the list, the filters and the layout; a row only
 * needs what this returns.
 */

/** The key one lifecycle action holds while in flight: an instance is unique per host and project. */
export function actionKey(host: string, project: string, name: string, action: InstanceAction | "delete"): string {
  return `${host}/${project}/${name}:${action}`;
}

export function useInstanceActions({ refetch }: { refetch: () => unknown }) {
  const { scopeKey } = useActorQueryScope();
  const startInstance = useStartInstance();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  const activeActorScopeRef = useRef(scopeKey);
  activeActorScopeRef.current = scopeKey;
  const previousActorScopeRef = useRef(scopeKey);
  const [actionUiActorScope, setActionUiActorScope] = useState(scopeKey);
  const actionUiIsCurrent = actionUiActorScope === scopeKey;
  const actionRefetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deleteRequestActorScopeRef = useRef<string | null>(null);

  // The lifecycle actions in flight, keyed like the ref below (host/project/name:action), so a
  // row shows its own pending state and never a same-named instance's on another cluster.
  const [pendingActions, setPendingActions] = useState<ReadonlySet<string>>(() => new Set());
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
  if (actionUiIsCurrent && deleteTarget) deleteDisplayRef.current = deleteTarget;
  const deleteDisplay = actionUiIsCurrent
    ? deleteTarget ?? deleteDisplayRef.current
    : null;
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

  useEffect(() => {
    if (previousActorScopeRef.current === scopeKey) return;
    previousActorScopeRef.current = scopeKey;
    setActionUiActorScope(scopeKey);
    setPendingActions(new Set());
    actionsInFlightRef.current.clear();
    setDeleteTarget(null);
    setRebuildTarget(null);
    setCopyTarget(null);
    deleteDisplayRef.current = null;
    deleteRequestActorScopeRef.current = null;
    setConfirmAction({
      open: false,
      action: null,
      name: "",
      host: "",
      project: "",
      force: false,
    });
    if (actionRefetchTimerRef.current !== null) {
      clearTimeout(actionRefetchTimerRef.current);
      actionRefetchTimerRef.current = null;
    }
  }, [scopeKey]);

  useEffect(
    () => () => {
      if (actionRefetchTimerRef.current !== null) {
        clearTimeout(actionRefetchTimerRef.current);
      }
    },
    [],
  );

  // Entry point for the row action buttons. Start fires immediately; stop and
  // restart open a confirm dialog so the user can choose a graceful or forced
  // (power-cycle) operation.
  const handleAction = (
    instanceName: string,
    host: string,
    project: string,
    action: "start" | "stop" | "restart"
  ) => {
    if (activeActorScopeRef.current !== scopeKey) return;
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
    setActionUiActorScope(scopeKey);
    setConfirmAction({ open: true, action, name: instanceName, host, project, force: false });
  };

  const runAction = async (
    instanceName: string,
    host: string,
    project: string,
    action: "start" | "stop" | "restart",
    force = false
  ) => {
    const requestActorScope = scopeKey;
    if (activeActorScopeRef.current !== requestActorScope) return;
    const key = actionKey(host, project, instanceName, action);
    const requestKey = `${requestActorScope}:${key}`;
    if (actionsInFlightRef.current.has(requestKey)) return;
    actionsInFlightRef.current.add(requestKey);
    try {
      setPendingActions((prev) => new Set(prev).add(key));

      if (action === "start") {
        await startInstance.mutateAsync({ name: instanceName, host, project });
      } else if (action === "stop") {
        await stopInstance.mutateAsync({ name: instanceName, host, project, force });
      } else {
        await restartInstance.mutateAsync({ name: instanceName, host, project, force });
      }

      if (activeActorScopeRef.current === requestActorScope) {
        toast({
          title: "Success",
          description: `Instance ${instanceName} ${action} initiated`,
        });

        // The callback belongs to this actor's list query. Never invoke it after a session
        // transition, even if the old request finishes successfully.
        if (actionRefetchTimerRef.current !== null) {
          clearTimeout(actionRefetchTimerRef.current);
        }
        actionRefetchTimerRef.current = setTimeout(() => {
          actionRefetchTimerRef.current = null;
          if (activeActorScopeRef.current === requestActorScope) void refetch();
        }, 2000);
      }
    } catch (error) {
      if (activeActorScopeRef.current === requestActorScope) {
        toast({
          title: "Error",
          description: error instanceof Error ? error.message : `Failed to ${action} instance`,
          variant: "destructive",
        });
      }
    } finally {
      actionsInFlightRef.current.delete(requestKey);
      if (activeActorScopeRef.current === requestActorScope) {
        setPendingActions((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
      }
    }
  };

  const handleDeleteConfirm = async () => {
    if (!actionUiIsCurrent || !deleteTarget || activeActorScopeRef.current !== scopeKey) return;
    const requestActorScope = scopeKey;
    const target = deleteTarget;
    const key = actionKey(target.host, target.project || "default", target.name, "delete");
    const requestKey = `${requestActorScope}:${key}`;
    if (actionsInFlightRef.current.has(requestKey)) return;
    actionsInFlightRef.current.add(requestKey);
    deleteRequestActorScopeRef.current = requestActorScope;
    try {
      await deleteInstance.mutateAsync({
        name: target.name,
        host: target.host,
        force: true, // stops a running instance before deleting
        project: target.project || "default",
      });
      if (activeActorScopeRef.current === requestActorScope) {
        toast({
          title: "Instance deleted",
          description: `"${target.name}" has been deleted.`,
        });
      }
    } catch (err) {
      if (activeActorScopeRef.current === requestActorScope) {
        toast({
          title: "Failed to delete instance",
          description: err instanceof Error ? err.message : String(err),
          variant: "destructive",
        });
      }
    } finally {
      actionsInFlightRef.current.delete(requestKey);
      if (activeActorScopeRef.current === requestActorScope) {
        setDeleteTarget(null);
      }
      if (deleteRequestActorScopeRef.current === requestActorScope) {
        deleteRequestActorScopeRef.current = null;
      }
    }
  };

  const confirmActionRun = () => {
    if (!actionUiIsCurrent || activeActorScopeRef.current !== scopeKey) return;
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
  const deletePending = (instance: Pick<VMInfo, "name" | "host" | "project">) =>
    actionUiIsCurrent &&
    deleteInstance.isPending &&
    deleteRequestActorScopeRef.current === scopeKey &&
    deleteTarget?.name === instance.name &&
    deleteTarget?.host === instance.host &&
    (deleteTarget?.project || "default") === (instance.project || "default");

  return {
    isReadOnly,
    pendingActions: actionUiIsCurrent ? pendingActions : NO_PENDING_ACTIONS,
    deleteTarget: actionUiIsCurrent ? deleteTarget : null,
    setDeleteTarget: (target: VMInfo | null) => {
      if (activeActorScopeRef.current !== scopeKey) return;
      setActionUiActorScope(scopeKey);
      setDeleteTarget(target);
    },
    deleteDisplay,
    deleteInFlight:
      deleteInstance.isPending && deleteRequestActorScopeRef.current === scopeKey,
    deletePending,
    rebuildTarget: actionUiIsCurrent ? rebuildTarget : null,
    setRebuildTarget: (target: VMInfo | null) => {
      if (activeActorScopeRef.current !== scopeKey) return;
      setActionUiActorScope(scopeKey);
      setRebuildTarget(target);
    },
    copyTarget: actionUiIsCurrent ? copyTarget : null,
    setCopyTarget: (target: VMInfo | null) => {
      if (activeActorScopeRef.current !== scopeKey) return;
      setActionUiActorScope(scopeKey);
      setCopyTarget(target);
    },
    confirmAction: actionUiIsCurrent
      ? confirmAction
      : { open: false, action: null, name: "", host: "", project: "", force: false },
    setConfirmAction: (next: ConfirmActionState) => {
      if (activeActorScopeRef.current !== scopeKey) return;
      setActionUiActorScope(scopeKey);
      setConfirmAction(next);
    },
    handleAction,
    handleDeleteConfirm,
    confirmActionRun,
  };
}
