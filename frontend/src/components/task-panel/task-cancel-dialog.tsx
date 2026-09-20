"use client";

import * as React from "react";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import type { TaskRecord } from "@/types/tasks";
import {
  useActorMutationGuard,
  useCancelTask,
  type ActorMutationGuard,
} from "./hooks/use-tasks";

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
  const activeActorGuardRef = useActorMutationGuard();
  const cancelInFlightRef = React.useRef<{
    actorGuard: ActorMutationGuard;
    token: symbol;
  } | null>(null);
  const [ownedError, setOwnedError] = React.useState<{
    actorGuard: ActorMutationGuard;
    message: string;
  } | null>(null);
  // Held one render past the real target so the body does not blank during the exit fade.
  // The owner guard keeps that courtesy within one actor. A scope transition removes the old
  // instance-identifying body immediately instead of leaving it mounted for the exit fade.
  const shown = React.useRef<{
    actorGuard: ActorMutationGuard;
    task: TaskRecord;
  } | null>(null);
  if (task) shown.current = { actorGuard: activeActorGuardRef.current, task };
  const target =
    task ??
    (shown.current?.actorGuard === activeActorGuardRef.current
      ? shown.current.task
      : null);
  const error =
    ownedError?.actorGuard === activeActorGuardRef.current
      ? ownedError.message
      : null;

  React.useEffect(() => {
    if (!open) setOwnedError(null);
  }, [open]);

  const confirmCancel = async () => {
    const requestActorGuard = activeActorGuardRef.current;
    if (
      !target ||
      requestActorGuard.scopeKey === null ||
      cancelInFlightRef.current?.actorGuard === requestActorGuard
    ) {
      return;
    }
    const requestToken = Symbol("cancel-task");
    cancelInFlightRef.current = {
      actorGuard: requestActorGuard,
      token: requestToken,
    };
    setOwnedError(null);
    try {
      const resultIsCurrent = await cancel.mutateAsync(target.id);
      if (
        resultIsCurrent &&
        activeActorGuardRef.current === requestActorGuard
      ) {
        onOpenChange(false);
      }
    } catch (err) {
      if (activeActorGuardRef.current === requestActorGuard) {
        setOwnedError({
          actorGuard: requestActorGuard,
          message:
            err instanceof Error
              ? err.message
              : "The task could not be cancelled.",
        });
      }
    } finally {
      if (cancelInFlightRef.current?.token === requestToken) {
        cancelInFlightRef.current = null;
      }
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
