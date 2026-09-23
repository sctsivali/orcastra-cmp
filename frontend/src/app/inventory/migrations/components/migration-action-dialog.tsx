"use client";

import * as React from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useMigrationAction } from "@/hooks/use-migrations";
import { useToast } from "@/hooks/use-toast";
import type { MigrationAction, MigrationJob } from "@/types/migrations";
import { endpointLabel } from "./migration-format";

const ACTION_COPY: Record<MigrationAction, { title: string; description: string; submit: string }> = {
  cancel: {
    title: "Cancel Migration",
    description: "Cancel a queued migration. No workload changes have started.",
    submit: "Cancel migration",
  },
  cutover: {
    title: "Approve Cutover",
    description: "Quiesce the source, perform the final sync, validate the target, and begin traffic cutover.",
    submit: "Approve cutover",
  },
  confirm_external_cutover: {
    title: "Confirm External Cutover",
    description: "Confirm that external DNS or load balancer traffic now points to the validated target.",
    submit: "Confirm traffic cutover",
  },
  rollback: {
    title: "Start Rollback",
    description: "Start rollback only after the server proves eligibility. Automated rollback is unavailable after target activation or new writes; recovery then requires manual reverse sync.",
    submit: "Start rollback",
  },
  restore_checkpoint: {
    title: "Restore Checkpoint",
    description: "Replace current workload state with the retained checkpoint. This can discard newer writes.",
    submit: "Restore checkpoint",
  },
  cleanup: {
    title: "Delete Retained Source",
    description: "Permanently remove the retained source after fresh drift and target-health checks.",
    submit: "Delete source",
  },
  recheck: {
    title: "Recheck Migration",
    description: "Reconcile authoritative source, target, and daemon operation state without replaying a mutation.",
    submit: "Run recheck",
  },
};

export function migrationActionCopy(
  job: MigrationJob,
  action: MigrationAction,
): { title: string; description: string; submit: string } {
  if (action === "cancel" && job.phase === "cutover_pending") {
    return {
      title: "Abort Before Cutover",
      description:
        "Stop before any daemon mutation. In this release, cutover approval is requested before target staging. The server refuses this action if it finds mutation evidence.",
      submit: "Abort before cutover",
    };
  }
  return ACTION_COPY[action];
}

const CONFIRMATION: Partial<Record<MigrationAction, string>> = {
  rollback: "ROLLBACK",
  restore_checkpoint: "RESTORE",
  cleanup: "CLEANUP",
};

interface MigrationActionDialogProps {
  job: MigrationJob;
  action: MigrationAction | null;
  onOpenChange: (open: boolean) => void;
  onCompleted: (job: MigrationJob) => void;
}

export function MigrationActionDialog({
  job,
  action,
  onOpenChange,
  onCompleted,
}: MigrationActionDialogProps) {
  const [reason, setReason] = React.useState("");
  const [confirmation, setConfirmation] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const mutation = useMigrationAction();
  const { toast } = useToast();
  const submitInFlightRef = React.useRef(false);

  React.useEffect(() => {
    if (action) {
      setReason("");
      setConfirmation("");
      setError(null);
    }
  }, [action, job.job_id, job.revision]);

  if (!action) return null;
  const copy = migrationActionCopy(job, action);
  const phrase = CONFIRMATION[action];
  const destructive = action === "cleanup" || action === "restore_checkpoint" || action === "rollback";
  const valid = reason.trim().length >= 8 && (!phrase || confirmation === phrase);

  const submit = async () => {
    if (!valid || submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      const updated = await mutation.mutateAsync({
        jobId: job.job_id,
        action,
        body: {
          expected_revision: job.revision,
          reason: reason.trim(),
          confirmation: phrase ? confirmation : undefined,
        },
      });
      toast({ title: `${copy.title} accepted`, description: `Migration is now in ${updated.phase.replaceAll("_", " ")}.` });
      onCompleted(updated);
      onOpenChange(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The migration action failed.");
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !mutation.isPending && onOpenChange(open)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {destructive && (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" aria-hidden />
              <AlertTitle>This action can discard data</AlertTitle>
              <AlertDescription>
                <span className="block">
                  The server will fail closed if fingerprints, target health, or source state no
                  longer match the approved migration.
                </span>
                <span className="mt-2 block break-all font-mono text-xs">
                  Source: {endpointLabel(job.source)}
                </span>
                <span className="block break-all font-mono text-xs">
                  Destination: {endpointLabel(job.target)}
                </span>
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="migration-action-reason">Reason</Label>
            <Textarea
              id="migration-action-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              maxLength={500}
              placeholder="Record why this action is required"
            />
            {reason.trim().length > 0 && reason.trim().length < 8 && (
              <p className="text-xs text-destructive">Enter at least 8 characters.</p>
            )}
          </div>

          {phrase && (
            <div className="space-y-2">
              <Label htmlFor="migration-action-confirmation">
                Type <span className="font-mono">{phrase}</span> to confirm
              </Label>
              <Input
                id="migration-action-confirmation"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                autoComplete="off"
              />
            </div>
          )}

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          {mutation.isPending && (
            <p role="status" className="sr-only" aria-live="polite">
              Submitting {copy.submit.toLowerCase()}.
            </p>
          )}
        </div>

        <DialogFooter className="gap-2 sm:space-x-0">
          <Button
            className="min-h-11 w-full sm:min-h-10 sm:w-auto"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            Keep current state
          </Button>
          <Button
            className="min-h-11 w-full sm:min-h-10 sm:w-auto"
            variant={destructive ? "destructive" : "default"}
            onClick={submit}
            disabled={!valid || mutation.isPending}
          >
            {mutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />}
            {mutation.isPending ? "Submitting" : copy.submit}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
