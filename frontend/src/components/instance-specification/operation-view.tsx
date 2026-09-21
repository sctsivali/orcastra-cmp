import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DialogBody, DialogFooter } from "@/components/ui/dialog";
import type { InstanceOperationJob, InstanceSpecificationPreflightResponse } from "@/types/api";
import { APIError } from "@/types/api";

import { ChangeSummary, OperationReadback } from "./operation-summary";
import type { SpecificationOperationAttempt } from "./operation-recovery";

export type DialogPhase = "form" | "confirm" | "running" | "result" | "retry";

function phaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    pending: "Queued",
    preflighting: "Checking current state",
    submitting: "Submitting to the cluster",
    stopping: "Gracefully stopping the instance",
    applying: "Applying specifications",
    starting: "Starting the instance",
    verifying: "Verifying the applied state",
  };
  return labels[phase] || "Working";
}

interface OperationViewProps {
  phase: DialogPhase;
  attempt: SpecificationOperationAttempt | null;
  error: string | null;
  updating: boolean;
  writeDisabledReason?: string;
  pollingHalted: boolean;
  identityConflict: string | null;
  statusError: unknown;
  job: InstanceOperationJob | null | undefined;
  plan: InstanceSpecificationPreflightResponse | null;
  onClose: () => void;
  onRetryAttempt: (attempt: SpecificationOperationAttempt) => void;
  onRetryStatus: () => void;
  onReload: () => void;
  onDone: () => void;
}

export function SpecificationOperationView({
  phase,
  attempt,
  error,
  updating,
  writeDisabledReason,
  pollingHalted,
  identityConflict,
  statusError,
  job,
  plan,
  onClose,
  onRetryAttempt,
  onRetryStatus,
  onReload,
  onDone,
}: OperationViewProps) {
  if (phase === "retry" && attempt) {
    return (
      <>
        <DialogBody className="space-y-4">
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <AlertTitle>Submission result was not received</AlertTitle>
            <AlertDescription>
              {error || "The server may already be processing this update. Do not submit a new change."}
            </AlertDescription>
          </Alert>
          <p className="text-sm text-muted-foreground">
            Retry replays the exact typed request, ETag, and idempotency key held in this open
            console. They are not stored in sessionStorage. After a page reload, inspect actual
            state before submitting any new update.
          </p>
          {writeDisabledReason && (
            <p className="text-sm text-muted-foreground">{writeDisabledReason}</p>
          )}
        </DialogBody>
        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={onClose} disabled={updating}>Close</Button>
          <Button variant="outline" onClick={onReload} disabled={updating}>
            Reload actual state
          </Button>
          <Button
            onClick={() => onRetryAttempt(attempt)}
            disabled={updating || !!writeDisabledReason}
            title={writeDisabledReason}
          >
            {updating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Retry same request
          </Button>
        </DialogFooter>
      </>
    );
  }

  if (phase === "result" && identityConflict) {
    return (
      <>
        <DialogBody>
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <AlertTitle>Operation identity conflict</AlertTitle>
            <AlertDescription>
              {identityConflict} Reload actual state before attempting another update.
            </AlertDescription>
          </Alert>
        </DialogBody>
        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={onClose}>Close</Button>
          <Button onClick={onReload}>Reload actual state</Button>
        </DialogFooter>
      </>
    );
  }

  if (phase === "running") {
    return (
      <>
        <DialogBody className="space-y-4">
          <div className="flex items-start gap-3 rounded-md border p-4" role="status" aria-live="polite">
            <Loader2 className="mt-0.5 h-5 w-5 shrink-0 animate-spin" aria-hidden="true" />
            <div>
              <p className="font-medium">{phaseLabel(job?.phase || "pending")}</p>
              <p className="text-sm text-muted-foreground">
                You can close this dialog. Status resumes from the same job when you open it again.
              </p>
            </div>
          </div>
          {plan && <ChangeSummary changes={plan.changes} />}
          {!!statusError && (
            <Alert variant="destructive">
              <AlertTitle>Status check failed</AlertTitle>
              <AlertDescription className="space-y-2">
                <p>The operation may still be running. Retrying does not submit it again.</p>
                <Button size="sm" variant="outline" onClick={onRetryStatus}>
                  <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
                  Retry status check
                </Button>
              </AlertDescription>
            </Alert>
          )}
        </DialogBody>
        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={onClose}>Close</Button>
        </DialogFooter>
      </>
    );
  }

  if (phase === "result" && pollingHalted) {
    const missing = statusError instanceof APIError && statusError.status === 404;
    return (
      <>
        <DialogBody>
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            <AlertTitle>Operation status unavailable</AlertTitle>
            <AlertDescription>
              {missing
                ? "This operation is no longer available. Polling stopped to avoid an endless retry loop."
                : "You no longer have permission to read this operation. Polling stopped."}
            </AlertDescription>
          </Alert>
        </DialogBody>
        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={onClose}>Close</Button>
          <Button onClick={onReload}>Reload actual state</Button>
        </DialogFooter>
      </>
    );
  }

  if (phase !== "result" || !job) return null;
  const title = job.status === "success"
    ? "Specifications applied and verified"
    : job.status === "partial"
      ? "Specifications partially applied"
      : job.status === "unknown"
        ? "Outcome needs verification"
        : "Specifications were not changed";
  return (
    <>
      <DialogBody className="space-y-4">
        <Alert variant={job.status === "success" ? "default" : "destructive"}>
          {job.status !== "success" && <AlertTriangle className="h-4 w-4" aria-hidden="true" />}
          <AlertTitle>{title}</AlertTitle>
          <AlertDescription>
            {job.status === "success"
              ? "The daemon operation completed and the cluster readback matches the requested values."
              : job.error || (job.status === "unknown"
                ? "The request timed out before a terminal result was available. Read the actual cluster state before retrying."
                : "The cluster could not complete the requested change.")}
          </AlertDescription>
        </Alert>
        {plan && <ChangeSummary changes={plan.changes} />}
        <OperationReadback job={job} />
      </DialogBody>
      <DialogFooter className="shrink-0 pt-4">
        {job.status === "success" ? (
          <Button onClick={onDone}>Done</Button>
        ) : (
          <>
            <Button variant="outline" onClick={onClose}>Close</Button>
            <Button onClick={onReload}>Reload actual state</Button>
          </>
        )}
      </DialogFooter>
    </>
  );
}
