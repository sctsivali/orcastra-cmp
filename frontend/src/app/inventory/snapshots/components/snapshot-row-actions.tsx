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
import { snapshotScope } from "./snapshot-format";
import { acknowledgementLabel, snapshotGates } from "./snapshot-origin";

interface SnapshotRowActionsProps {
  snapshot: SnapshotInfo;
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
  billingLinked = false,
}: SnapshotRowActionsProps) {
  // important: the row's own cluster and project, never the page's. The props that used to carry
  // them are gone rather than corrected: a component that cannot be handed a cluster cannot be
  // handed the wrong one, which is the same reasoning the unreachable banner records for the
  // node names it refuses to accept.
  const { host, project } = snapshotScope(snapshot);
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  const deleteSnapshot = useDeleteSnapshot();
  const restoreSnapshot = useRestoreSnapshot();

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showRestore, setShowRestore] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteAcknowledged, setDeleteAcknowledged] = useState(false);
  const submitInFlightRef = useRef(false);

  // important: origin is no longer a refusal. It used to disable both actions for anything
  // without the `ops-` prefix, which meant an operator was refused their own hand-taken snapshot
  // and told it was "managed outside this console". What origin decides now is which gate the
  // action faces, not whether it is offered.
  const { origin, needsAcknowledgement, needsTypedName } = snapshotGates(snapshot, billingLinked);

  const unavailableReason = !snapshot.host
    ? "This snapshot's cluster is unknown"
    : origin === "unknown"
      ? "This snapshot's origin could not be read"
      : isReadOnly
        ? "The dashboard is in read-only mode"
        : undefined;

  // Said on the item, not only in the dialog, so an operator learns before opening it.
  const billingNote = needsTypedName
    ? " (taken on the cluster, type its name)"
    : origin === "upstream"
      ? " (upstream restore point, needs a reason)"
      : billingLinked
        ? " (billed elsewhere, needs a reason)"
        : "";

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
  const handleDelete = async (reason?: string) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setDeleteError(null);
    try {
      await deleteSnapshot.mutateAsync({
        instanceName: snapshot.source_name,
        snapshotName: snapshot.name,
        host,
        project,
        // important: the reason is typed by the operator, never composed here. Deleting lowers the
        // storage the external schedule is measured on, but the rule an operator holds is
        // "anything on a billing-linked instance is acknowledged", and a rule with an exception is
        // one people have to remember the shape of. This path used to send a canned sentence, so
        // every forced delete wrote the same machine-authored justification into an audit record
        // whose whole purpose is attribution.
        reason: needsAcknowledgement ? reason : undefined,
        acknowledgeBillingImpact: billingLinked || undefined,
        // important: a distinct field. Reusing the billing acknowledgement would let one tick
        // assert two unrelated facts, and the backend refuses each on its own.
        acknowledgeUpstreamSnapshot: origin === "upstream" || undefined,
        confirmSnapshot: needsTypedName ? snapshot.name : undefined,
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
            <span className="font-medium">{snapshot.source_name}</span> on{" "}
            <span className="font-mono">{host}</span> /{" "}
            <span className="font-mono">{project}</span>. The instance itself is untouched, but
            this restore point is gone for good.
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
          needsAcknowledgement ? acknowledgementLabel(origin, billingLinked) : undefined
        }
        reasonLabel={needsAcknowledgement ? "Reason" : undefined}
        reasonPlaceholder="Why this snapshot is being removed right now"
        confirmPhrase={
          needsTypedName
            ? { prompt: "Type the snapshot's name to confirm", value: snapshot.name }
            : undefined
        }
      />

      <RestoreSnapshotDialog
        open={showRestore}
        onOpenChange={setShowRestore}
        snapshot={snapshot}
        billingLinked={billingLinked}
        restore={restoreSnapshot}
      />
    </>
  );
}
