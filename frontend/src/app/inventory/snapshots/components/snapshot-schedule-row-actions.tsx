"use client";

import { useRef, useState } from "react";
import { CalendarClock, Pause, Play, Trash2 } from "lucide-react";

import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { useToast } from "@/hooks/use-toast";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import {
  useDeleteSnapshotSchedule,
  usePauseSnapshotSchedule,
  useResumeSnapshotSchedule,
  useRunSnapshotScheduleNow,
} from "@/hooks/use-lxd";
import type { SnapshotSchedule } from "@/types/api";

import { scheduleState } from "./snapshot-schedule-vocabulary";

/**
 * Pause, resume, run now and delete.
 *
 * Only the delete carries a dialog, and a plain one. `DestructiveConfirmDialog` documents that its
 * three gates are not interchangeable: type the name when the mistake would be WHICH row, tick a
 * box to accept a consequence, ask for a reason only where the act is recorded as an override.
 * Deleting a schedule is none of those. It destroys a PLAN, not a restore point, and the snapshots
 * it already took stay where they are and expire on their own. Adding friction here would be the
 * generic kind that component warns against, and it would teach an operator to click through the
 * gates that do matter.
 *
 * note: `check-submit-guards.mjs` matches only five handler names and none of these is one of
 * them, so CI stays green without the in-flight ref. It is here anyway: the double-fire it guards
 * against is real regardless of whether a script can see it.
 */
export function ScheduleRowActions({ schedule }: { schedule: SnapshotSchedule }) {
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  const pause = usePauseSnapshotSchedule();
  const resume = useResumeSnapshotSchedule();
  const runNow = useRunSnapshotScheduleNow();
  const remove = useDeleteSnapshotSchedule();

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  const state = scheduleState(schedule);
  const paused = state === "paused";
  const unavailableReason = isReadOnly ? "The dashboard is in read-only mode" : undefined;

  async function fire(
    run: () => Promise<{ message: string }>,
    title: string,
    failureTitle: string,
  ): Promise<void> {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const result = await run();
      toast({ title, description: result.message });
    } catch (error) {
      toast({
        title: failureTitle,
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      inFlightRef.current = false;
    }
  }

  const actions: RowAction[] = [
    {
      id: "run-now",
      label: "Run Now",
      icon: CalendarClock,
      unavailableReason:
        unavailableReason ??
        (paused ? "Resume this schedule before running it" : undefined),
      onSelect: () =>
        void fire(() => runNow.mutateAsync(schedule.id), "Snapshot taken", "Could not take snapshot"),
    },
    paused
      ? {
          id: "resume",
          label: "Resume",
          icon: Play,
          unavailableReason,
          onSelect: () =>
            void fire(() => resume.mutateAsync(schedule.id), "Schedule resumed", "Could not resume schedule"),
        }
      : {
          id: "pause",
          label: "Pause",
          icon: Pause,
          unavailableReason,
          onSelect: () => void fire(() => pause.mutateAsync(schedule.id), "Schedule paused", "Could not pause schedule"),
        },
    {
      id: "delete",
      label: "Delete Schedule",
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      unavailableReason,
      onSelect: () => {
        setDeleteError(null);
        setConfirmDelete(true);
      },
    },
  ];

  async function handleDelete(): Promise<void> {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setDeleteError(null);
    try {
      const result = await remove.mutateAsync(schedule.id);
      toast({ title: "Schedule removed", description: result.message });
      setConfirmDelete(false);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
    } finally {
      inFlightRef.current = false;
    }
  }

  return (
    <>
      <RowActions actions={actions} triggerLabel={`Actions for schedule ${schedule.label}`} />
      <DestructiveConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete This Schedule"
        description={
          <>
            The <span className="font-mono">{schedule.label}</span> schedule on{" "}
            <span className="font-mono">{schedule.instance_name}</span> stops and is removed.
            {" "}
            {/* The question an operator will actually have, answered here rather than in support. */}
            The snapshots it already took stay where they are and expire on their own.
          </>
        }
        confirmLabel="Delete schedule"
        pendingLabel="Deleting"
        pending={remove.isPending}
        error={deleteError}
        onConfirm={() => void handleDelete()}
      />
    </>
  );
}
