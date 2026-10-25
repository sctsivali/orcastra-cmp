"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { formatDateTime } from "@/lib/utils";
import type { SnapshotInfo } from "@/types/api";

import { snapshotScope } from "./snapshot-format";
import { acknowledgementLabel, snapshotGates } from "./snapshot-origin";

interface RestoreSnapshotDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  snapshot: SnapshotInfo;
  /** The source's snapshots are metered and billed by another product. */
  billingLinked?: boolean;
  restore: {
    mutateAsync: (vars: {
      instanceName: string;
      snapshotName: string;
      body: {
        host: string;
        project: string;
        confirm_name: string;
        safety_snapshot: boolean;
        reason?: string;
        acknowledge_billing_impact?: boolean;
        acknowledge_upstream_snapshot?: boolean;
      };
    }) => Promise<unknown>;
    isPending: boolean;
  };
}

/**
 * Restoring discards every write since the snapshot was taken, so this asks for the instance
 * name rather than a checkbox. Typing belongs to actions that destroy data the operator cannot
 * get back, which is exactly this one.
 *
 * The safety snapshot defaults on and is taken before the restore runs. Taken afterwards it
 * would capture the already-rolled-back state and protect nothing.
 */
export function RestoreSnapshotDialog({
  open,
  onOpenChange,
  snapshot,
  billingLinked = false,
  restore,
}: RestoreSnapshotDialogProps) {
  // The row's own cluster and project. Under a fleet-wide listing the page has no single scope
  // to hand down, and a restore discards every write since the snapshot: aiming it at the wrong
  // cluster is not recoverable.
  const { host, project } = snapshotScope(snapshot);
  const { toast } = useToast();
  const [confirmName, setConfirmName] = useState("");
  const [safety, setSafety] = useState(true);
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitInFlightRef = useRef(false);

  useEffect(() => {
    if (open) {
      setConfirmName("");
      setSafety(true);
      setReason("");
      setAcknowledged(false);
      setError(null);
    }
  }, [open]);

  const nameMatches = confirmName.trim() === snapshot.source_name;
  // Both halves required together, same rule as the create dialog.
  // The same union the delete path uses. A restore can be gated because the SOURCE is billed
  // elsewhere, because the SNAPSHOT belongs to the upstream product, or both, and the two are
  // independent facts. The hand-taken class needs no extra field here: this dialog already makes
  // the operator type the instance name, and a second typed string in one dialog is what
  // produces copy-paste without reading.
  const { origin, needsAcknowledgement } = snapshotGates(snapshot, billingLinked);
  const overrideReady = !needsAcknowledgement || (acknowledged && reason.trim().length > 0);
  const canSubmit = nameMatches && overrideReady && !restore.isPending;

  const handleSubmit = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      await restore.mutateAsync({
        instanceName: snapshot.source_name,
        snapshotName: snapshot.name,
        body: {
          host,
          project,
          confirm_name: snapshot.source_name,
          safety_snapshot: safety,
          reason: reason.trim() || undefined,
          acknowledge_billing_impact: billingLinked ? acknowledged : undefined,
          acknowledge_upstream_snapshot: origin === "upstream" ? acknowledged : undefined,
        },
      });
      toast({
        title: "Restore started",
        description: safety
          ? `${snapshot.source_name} is rolling back. A safety snapshot was taken first.`
          : `${snapshot.source_name} is rolling back to ${snapshot.name}.`,
      });
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not restore the snapshot.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] flex flex-col overflow-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Restore This Instance</DialogTitle>
          <DialogDescription>
            Roll {snapshot.source_name} back to {snapshot.name} on {host} / {project}.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="flex gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3">
            <AlertTriangle className="h-4 w-4 shrink-0 text-destructive mt-0.5" aria-hidden />
            <p className="text-sm">
              Everything written to{" "}
              <span className="font-medium">{snapshot.source_name}</span> since{" "}
              {snapshot.created_at ? formatDateTime(snapshot.created_at) : "this snapshot was taken"}{" "}
              will be discarded. The instance stops and starts as part of the restore.
            </p>
          </div>

          <div className="flex items-start gap-3">
            <Checkbox
              id="safety-snapshot"
              checked={safety}
              onCheckedChange={(checked) => setSafety(checked === true)}
            />
            <div className="space-y-1">
              <Label htmlFor="safety-snapshot" className="font-normal">
                Take a safety snapshot first
              </Label>
              <p className="text-xs text-muted-foreground">
                Captures the current state before rolling back, so this restore can itself be
                undone. It expires on its own like any operator snapshot.
              </p>
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="confirm-instance-name">
              Type <span className="font-mono">{snapshot.source_name}</span> to confirm
            </Label>
            <Input
              id="confirm-instance-name"
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              placeholder={snapshot.source_name}
              autoComplete="off"
              aria-invalid={confirmName.length > 0 && !nameMatches}
            />
            {confirmName.length > 0 && !nameMatches && (
              <p className="text-xs text-destructive">
                That does not match the instance name.
              </p>
            )}
          </div>

          {needsAcknowledgement && (
            <div className="space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <p className="text-sm">
                {origin === "upstream"
                  ? "This restore point belongs to the upstream snapshot product. Rolling onto it acts on something that product owns and a customer may be paying to keep."
                  : "Snapshots on this instance are managed and billed by another product. A restore changes the storage that schedule is measured on, and stops and starts the instance a customer is paying for."}
              </p>
              <div className="space-y-2">
                <Label htmlFor="restore-reason">Reason</Label>
                <Textarea
                  id="restore-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Why this instance must be rolled back now"
                  maxLength={280}
                  rows={3}
                />
              </div>
              <div className="flex items-start gap-3">
                <Checkbox
                  id="restore-acknowledge"
                  checked={acknowledged}
                  onCheckedChange={(checked) => setAcknowledged(checked === true)}
                />
                <Label htmlFor="restore-acknowledge" className="font-normal">
                  {acknowledgementLabel(origin, billingLinked)}
                </Label>
              </div>
            </div>
          )}

        </DialogBody>

        {/* Announced by its role while focus stays on the submit button, which does not move;
            without the live region a screen-reader user is told nothing at all. */}
        <DialogSubmitError message={error} />

        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={restore.isPending}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={handleSubmit} disabled={!canSubmit}>
            {restore.isPending ? "Restoring" : "Restore instance"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
