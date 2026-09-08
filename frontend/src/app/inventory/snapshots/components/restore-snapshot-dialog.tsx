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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import type { SnapshotInfo } from "@/types/api";

interface RestoreSnapshotDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  snapshot: SnapshotInfo;
  host: string;
  project: string;
  restore: {
    mutateAsync: (vars: {
      instanceName: string;
      snapshotName: string;
      body: {
        host: string;
        project: string;
        confirm_name: string;
        safety_snapshot: boolean;
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
  host,
  project,
  restore,
}: RestoreSnapshotDialogProps) {
  const { toast } = useToast();
  const [confirmName, setConfirmName] = useState("");
  const [safety, setSafety] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const submitInFlightRef = useRef(false);

  useEffect(() => {
    if (open) {
      setConfirmName("");
      setSafety(true);
      setError(null);
    }
  }, [open]);

  const nameMatches = confirmName.trim() === snapshot.source_name;
  const canSubmit = nameMatches && !restore.isPending;

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
            Roll {snapshot.source_name} back to {snapshot.name}.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div className="flex gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3">
            <AlertTriangle className="h-4 w-4 shrink-0 text-destructive mt-0.5" aria-hidden />
            <p className="text-sm">
              Everything written to{" "}
              <span className="font-medium">{snapshot.source_name}</span> since{" "}
              {snapshot.created_at
                ? new Date(snapshot.created_at).toLocaleString()
                : "this snapshot was taken"}{" "}
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

          {error && <p className="text-sm text-destructive">{error}</p>}
        </DialogBody>

        <DialogFooter>
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
