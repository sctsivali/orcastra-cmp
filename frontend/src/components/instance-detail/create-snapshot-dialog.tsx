"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { useCreateSnapshot } from "@/hooks/use-lxd";
import { useToast } from "@/hooks/use-toast";
import type { VMInfo } from "@/types/api";

const MAX_EXPIRY_DAYS = 7;
const DEFAULT_EXPIRY_DAYS = 7;

interface CreateSnapshotDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instance: VMInfo;
  project: string;
  /** The source's snapshots are metered and billed by another product. */
  billingLinked: boolean;
}

/**
 * Take an operator snapshot.
 *
 * The name is generated rather than typed. Operator snapshots have to stay inside their own
 * prefix so nothing upstream mistakes one for a scheduled snapshot it owns, and asking an
 * operator to remember that under pressure is how the rule gets broken.
 */
export function CreateSnapshotDialog({
  open,
  onOpenChange,
  instance,
  project,
  billingLinked,
}: CreateSnapshotDialogProps) {
  const { toast } = useToast();
  const createSnapshot = useCreateSnapshot();
  const [label, setLabel] = React.useState("");
  const [expiryDays, setExpiryDays] = React.useState(String(DEFAULT_EXPIRY_DAYS));
  const [reason, setReason] = React.useState("");
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const submitInFlightRef = React.useRef(false);

  React.useEffect(() => {
    if (open) {
      setLabel("");
      setExpiryDays(String(DEFAULT_EXPIRY_DAYS));
      setReason("");
      setAcknowledged(false);
      setError(null);
    }
  }, [open]);

  const days = Number(expiryDays);
  const expiryValid = Number.isInteger(days) && days >= 1 && days <= MAX_EXPIRY_DAYS;
  const labelValid = label.trim().length > 0 && label.trim().length <= 48;
  // Both halves are required together. A tick box with no words is not attribution, and words
  // without the tick are not a decision.
  const overrideReady = !billingLinked || (acknowledged && reason.trim().length > 0);
  const canSubmit = labelValid && expiryValid && overrideReady && !createSnapshot.isPending;

  const handleCreate = async () => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      const result = await createSnapshot.mutateAsync({
        instanceName: instance.name,
        body: {
          host: instance.host,
          project,
          label: label.trim(),
          expiry_days: days,
          reason: reason.trim() || undefined,
          acknowledge_billing_impact: billingLinked ? acknowledged : undefined,
        },
      });
      toast({ title: "Snapshot taken", description: result.message });
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not take the snapshot.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] flex flex-col overflow-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Take a Snapshot</DialogTitle>
          <DialogDescription>
            A restore point for {instance.name}, before a change you might need to undo.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="snapshot-label">What is this for</Label>
            <Input
              id="snapshot-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="before kernel upgrade"
              maxLength={48}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              Becomes part of the snapshot name, which starts with{" "}
              <span className="font-mono">ops-</span> and ends with a timestamp.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="snapshot-expiry">Keep for (days)</Label>
            <Input
              id="snapshot-expiry"
              type="number"
              min={1}
              max={MAX_EXPIRY_DAYS}
              value={expiryDays}
              onChange={(e) => setExpiryDays(e.target.value)}
              aria-invalid={!expiryValid}
            />
            <p className="text-xs text-muted-foreground">
              The hypervisor removes it on its own when the time is up, so a forgotten snapshot
              cannot sit on the pool. Maximum {MAX_EXPIRY_DAYS} days.
            </p>
            {!expiryValid && (
              <p className="text-xs text-destructive">
                Choose between 1 and {MAX_EXPIRY_DAYS} days.
              </p>
            )}
          </div>

          {billingLinked && (
            <div className="space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <div className="flex gap-3">
                <AlertTriangle className="h-4 w-4 shrink-0 text-destructive mt-0.5" aria-hidden />
                <p className="text-sm">
                  Snapshots on this instance are managed and billed by another product. Taking one
                  here adds to the storage that schedule is measured on, which can pause it for the
                  customer paying for it.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="snapshot-reason">Reason</Label>
                <Textarea
                  id="snapshot-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Why this instance needs an operator snapshot right now"
                  maxLength={280}
                  rows={3}
                />
              </div>

              <div className="flex items-start gap-3">
                <Checkbox
                  id="snapshot-acknowledge"
                  checked={acknowledged}
                  onCheckedChange={(checked) => setAcknowledged(checked === true)}
                />
                <Label htmlFor="snapshot-acknowledge" className="font-normal">
                  I accept the effect on the schedule this instance is billed under
                </Label>
              </div>
            </div>
          )}

          {error && <p className="text-sm text-destructive">{error}</p>}
        </DialogBody>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={createSnapshot.isPending}
          >
            Cancel
          </Button>
          <Button onClick={handleCreate} disabled={!canSubmit}>
            {createSnapshot.isPending
              ? "Taking snapshot"
              : billingLinked
                ? "Snapshot anyway"
                : "Take snapshot"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
