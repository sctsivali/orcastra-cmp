"use client";

import { useRef, useState } from "react";
import { RotateCcw, Trash2 } from "lucide-react";

import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { useDeleteSnapshot, useRestoreSnapshot } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useToast } from "@/hooks/use-toast";
import type { SnapshotInfo } from "@/types/api";

import { RestoreSnapshotDialog } from "./restore-snapshot-dialog";

interface SnapshotRowActionsProps {
  snapshot: SnapshotInfo;
  host: string;
  project: string;
  /** The source's snapshots are metered and billed by another product. */
  billingLinked?: boolean;
}

/**
 * Row actions for one snapshot.
 *
 * A snapshot this console did not create is listed but not actionable, and the menu says why
 * rather than hiding the items. Someone upstream is paying to keep it, and a restore point that
 * silently disappears from a tenant's account is worse than one an operator cannot reach.
 */
export function SnapshotRowActions({
  snapshot,
  host,
  project,
  billingLinked = false,
}: SnapshotRowActionsProps) {
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  const deleteSnapshot = useDeleteSnapshot();
  const restoreSnapshot = useRestoreSnapshot();

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showRestore, setShowRestore] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteAcknowledged, setDeleteAcknowledged] = useState(false);
  const submitInFlightRef = useRef(false);

  const unavailableReason = !snapshot.operator_managed
    ? "Managed outside this console"
    : isReadOnly
      ? "The dashboard is in read-only mode"
      : undefined;

  // Said on the item, not only in the dialog, so an operator learns before opening it.
  const billingNote = billingLinked ? " (billed elsewhere, needs a reason)" : "";

  const actions: RowAction[] = [
    {
      id: "restore",
      label: `Restore Instance to This Snapshot${billingNote}`,
      icon: RotateCcw,
      onSelect: () => setShowRestore(true),
      // Same rule as delete. Rolling an instance onto a snapshot this console did not create
      // discards everything written since a restore point someone upstream is paid to keep, and
      // the backend refuses it, so offering the item here only produced a 403 after the typing.
      unavailableReason,
    },
    {
      id: "delete",
      label: "Delete Snapshot",
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      onSelect: () => {
        setDeleteError(null);
        setDeleteAcknowledged(false);
        setConfirmDelete(true);
      },
      unavailableReason,
    },
  ];

  // disabled={pending} is React state and only reaches the DOM on the next render, so two
  // clicks inside one tick both fire. The ref is what actually blocks the second.
  const handleDelete = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setDeleteError(null);
    try {
      await deleteSnapshot.mutateAsync({
        instanceName: snapshot.source_name,
        snapshotName: snapshot.name,
        host,
        project,
        // Deleting lowers the storage the external schedule is measured on, but the rule an
        // operator holds is "anything on a billing-linked instance is acknowledged". A rule with
        // an exception is one people have to remember the shape of.
        reason: billingLinked ? "Operator removing a snapshot this console created" : undefined,
        acknowledgeBillingImpact: billingLinked || undefined,
      });
      toast({
        title: "Snapshot deleted",
        description: `${snapshot.name} is gone from ${snapshot.source_name}.`,
      });
      setConfirmDelete(false);
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "Could not delete the snapshot.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <>
      <RowActions triggerLabel={`Actions for snapshot ${snapshot.name}`} actions={actions} />

      <DestructiveConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete this snapshot"
        description={
          <>
            <span className="font-mono">{snapshot.name}</span> will be removed from{" "}
            <span className="font-medium">{snapshot.source_name}</span>. The instance itself is
            untouched, but this restore point is gone for good.
          </>
        }
        confirmLabel="Delete snapshot"
        pendingLabel="Deleting"
        pending={deleteSnapshot.isPending}
        onConfirm={handleDelete}
        error={deleteError}
        // The component's own guidance: an acknowledgement belongs where proceeding overrides a
        // real refusal, never as generic friction. This is that case.
        acknowledgeLabel={
          billingLinked
            ? "I accept the effect on the schedule this instance is billed under"
            : undefined
        }
      />

      <RestoreSnapshotDialog
        open={showRestore}
        onOpenChange={setShowRestore}
        snapshot={snapshot}
        host={host}
        project={project}
        billingLinked={billingLinked}
        restore={restoreSnapshot}
      />
    </>
  );
}
