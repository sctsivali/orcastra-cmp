"use client";

import * as React from "react";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import type { TaskRecord } from "@/types/tasks";
import { useCancelTask } from "./hooks/use-tasks";

export interface TaskCancelDialogProps {
  task: TaskRecord | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Confirm-and-cancel for one task. The mutation toasts its own failure, so this only closes
 * on success and stays open, with the error line, on a refusal.
 *
 * important: the in-flight ref is claimed synchronously before the request starts. React
 * state lands a render later, and a double click inside that window used to send two DELETEs.
 */
export function TaskCancelDialog({ task, open, onOpenChange }: TaskCancelDialogProps) {
  const cancel = useCancelTask();
  const cancelInFlightRef = React.useRef(false);
  const [error, setError] = React.useState<string | null>(null);
  // Held one render past the real target so the body does not blank during the exit fade.
  const shown = React.useRef<TaskRecord | null>(null);
  if (task) shown.current = task;
  const target = task ?? shown.current;

  React.useEffect(() => {
    if (!open) setError(null);
  }, [open]);

  const confirmCancel = async () => {
    if (!target || cancelInFlightRef.current) return;
    cancelInFlightRef.current = true;
    setError(null);
    try {
      await cancel.mutateAsync(target.id);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The task could not be cancelled.");
    } finally {
      cancelInFlightRef.current = false;
    }
  };

  if (!target) return null;

  return (
    <DestructiveConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Cancel Task"
      confirmLabel="Cancel Task"
      pendingLabel="Cancelling"
      pending={cancel.isPending}
      error={error}
      onConfirm={() => void confirmCancel()}
      description={
        <>
          This stops &quot;{target.description}&quot; on {target.cluster_id}.
          {target.resource_name
            ? ` LXD may leave ${target.resource_name} in a partial state.`
            : " LXD may leave the resource in a partial state."}
        </>
      }
    />
  );
}
