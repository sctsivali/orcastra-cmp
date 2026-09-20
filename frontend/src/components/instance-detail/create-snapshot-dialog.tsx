"use client";

import * as React from "react";
import { AlertTriangle, Loader2 } from "lucide-react";

import {
  SnapshotCreateForm,
  type SnapshotCreateTarget,
} from "@/components/snapshots/snapshot-create-form";
import type { PendingSnapshotNotice } from "@/components/snapshots/pending-snapshot-alert";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useInstanceSnapshots } from "@/hooks/use-lxd";
import { useToast } from "@/hooks/use-toast";
import type { SnapshotCreateResponse, VMInfo } from "@/types/api";

interface CreateSnapshotDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instance: VMInfo;
  project: string;
  /** The source's snapshots are metered and billed by another product. */
  billingLinked: boolean;
  onPending?: (notice: PendingSnapshotNotice) => void;
}

/** Per-instance entry point backed by the same verified, stateless form as the fleet dialog. */
export function CreateSnapshotDialog({
  open,
  onOpenChange,
  instance,
  project,
  billingLinked,
  onPending,
}: CreateSnapshotDialogProps) {
  const [busy, setBusy] = React.useState(false);
  const handleOpenChange = (next: boolean) => {
    if (!next && busy) return;
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={`max-h-[calc(100dvh-2rem)] max-w-lg overflow-hidden flex flex-col ${busy ? "[&>button:last-child]:hidden" : ""}`}
      >
        <DialogHeader className="shrink-0 pr-8 pb-4">
          <DialogTitle>Take a Snapshot</DialogTitle>
          <DialogDescription className="min-w-0">
            Create a stateless restore point for{" "}
            <OverflowText
              value={instance.name}
              label="snapshot target instance"
              lines="none"
              mono
              copy="none"
            />{" "}
            before a change you might need to undo.
          </DialogDescription>
        </DialogHeader>
        <CreateSnapshotDialogBody
          busy={busy}
          instance={instance}
          project={project}
          billingLinked={billingLinked}
          onBusyChange={setBusy}
          onCancel={() => handleOpenChange(false)}
          onDone={() => onOpenChange(false)}
          onPending={onPending}
        />
      </DialogContent>
    </Dialog>
  );
}

interface CreateSnapshotDialogBodyProps {
  busy: boolean;
  instance: VMInfo;
  project: string;
  billingLinked: boolean;
  onBusyChange: (busy: boolean) => void;
  onCancel: () => void;
  onDone: () => void;
  onPending?: (notice: PendingSnapshotNotice) => void;
}

function CreateSnapshotDialogBody({
  busy,
  instance,
  project,
  billingLinked,
  onBusyChange,
  onCancel,
  onDone,
  onPending,
}: CreateSnapshotDialogBodyProps) {
  const { toast } = useToast();
  const snapshots = useInstanceSnapshots(instance.name, instance.host, project, true, true);

  const verifiedTarget: SnapshotCreateTarget | undefined =
    snapshots.isSuccess && !snapshots.isFetching && snapshots.data
      ? {
          instance,
          project,
          billingLinked: billingLinked || snapshots.data.policy.externally_managed,
          operatorCount: snapshots.data.items.filter((row) => row.operator_managed).length,
        }
      : undefined;
  const reviewedTargetRef = React.useRef<SnapshotCreateTarget | undefined>(undefined);
  if (!busy && verifiedTarget) reviewedTargetRef.current = verifiedTarget;
  // A reconnect or explicit verification can refetch while the POST is unresolved. Preserve the
  // exact reviewed target for that interval so the mounted form retains its single-flight guard.
  const target = busy ? reviewedTargetRef.current : verifiedTarget;
  const checkingPolicy = !busy && (snapshots.isLoading || snapshots.isFetching);
  const policyUnavailable =
    !busy &&
    (snapshots.isError ||
      (!snapshots.isLoading && !snapshots.isFetching && !snapshots.data));

  const finish = (result: SnapshotCreateResponse, target: SnapshotCreateTarget) => {
    onDone();
    if (result.status === "succeeded") {
      toast({ title: "Snapshot taken", description: result.message });
      return;
    }

    const notice: PendingSnapshotNotice = {
      host: target.instance.host,
      project: target.project,
      instanceName: target.instance.name,
      snapshotName: result.snapshot,
      message: result.message,
    };
    toast({ title: "Snapshot creation is in progress", description: result.message });
    onPending?.(notice);
  };

  return (
    <SnapshotCreateForm
      target={target}
      onBusyChange={onBusyChange}
      onCancel={onCancel}
      onResult={(result) => {
        if (target) finish(result, target);
      }}
      onViewSnapshots={async () => {
        await snapshots.refreshNow();
        onDone();
      }}
    >
      {checkingPolicy && (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
          Checking the snapshot policy
        </p>
      )}

      {policyUnavailable && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertTitle>Snapshot policy could not be verified</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>
              Refresh this instance before trying again. Snapshot creation stays unavailable until
              the current policy and capacity can be read.
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void snapshots.refetch()}
            >
              Retry policy check
            </Button>
          </AlertDescription>
        </Alert>
      )}
    </SnapshotCreateForm>
  );
}
