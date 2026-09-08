"use client";

import * as React from "react";

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
  /** The source carries an external schedule, so the reason becomes mandatory. */
  requiresReason: boolean;
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
  requiresReason,
}: CreateSnapshotDialogProps) {
  const { toast } = useToast();
  const createSnapshot = useCreateSnapshot();
  const [label, setLabel] = React.useState("");
  const [expiryDays, setExpiryDays] = React.useState(String(DEFAULT_EXPIRY_DAYS));
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const submitInFlightRef = React.useRef(false);

  React.useEffect(() => {
    if (open) {
      setLabel("");
      setExpiryDays(String(DEFAULT_EXPIRY_DAYS));
      setReason("");
      setError(null);
    }
  }, [open]);

  const days = Number(expiryDays);
  const expiryValid = Number.isInteger(days) && days >= 1 && days <= MAX_EXPIRY_DAYS;
  const labelValid = label.trim().length > 0 && label.trim().length <= 48;
  const reasonValid = !requiresReason || reason.trim().length > 0;
  const canSubmit = labelValid && expiryValid && reasonValid && !createSnapshot.isPending;

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

          {requiresReason && (
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
              <p className="text-xs text-muted-foreground">
                This instance is snapshotted on a schedule owned elsewhere. Your snapshot will
                appear in that owner&apos;s list and count toward their measured storage, so the
                reason is recorded in the audit trail.
              </p>
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
            {createSnapshot.isPending ? "Taking snapshot" : "Take snapshot"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
