"use client";

import * as React from "react";
import { AlertTriangle, Info } from "lucide-react";

import { DetailField, DetailGrid } from "@/components/detail/detail-field";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DialogBody, DialogFooter } from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import { Textarea } from "@/components/ui/textarea";
import { useCreateSnapshot } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { instanceStatusDisplay } from "@/lib/instance-status";
import { formatInstanceType } from "@/lib/utils";
import { APIError } from "@/types/api";
import type { SnapshotCreateResponse, VMInfo } from "@/types/api";

export const MAX_OPERATOR_SNAPSHOTS = 5;
const MAX_EXPIRY_DAYS = 7;
const DEFAULT_EXPIRY_DAYS = 7;

export interface SnapshotCreateTarget {
  instance: VMInfo;
  project: string;
  billingLinked: boolean;
  operatorCount: number;
}

interface SnapshotCreateFormProps {
  target?: SnapshotCreateTarget;
  /** Target selectors and their loading/error states for the fleet entry point. */
  children?: React.ReactNode;
  onCancel: () => void;
  onResult: (result: SnapshotCreateResponse) => void;
  onBusyChange: (busy: boolean) => void;
  onViewSnapshots: () => Promise<void>;
}

function hasOutcomeUnknownCode(detail: unknown): boolean {
  if (detail === "snapshot_outcome_unknown") return true;
  return (
    typeof detail === "object" &&
    detail !== null &&
    "error" in detail &&
    (detail as { error?: unknown }).error === "snapshot_outcome_unknown"
  );
}

function hasTerminalFailureCode(detail: unknown): boolean {
  return (
    typeof detail === "object" &&
    detail !== null &&
    "error" in detail &&
    (detail as { error?: unknown }).error === "snapshot_terminal_failure"
  );
}

function submitError(error: unknown): { message: string; outcomeUnknown: boolean } {
  if (error instanceof APIError) {
    if (error.status === 0 || error.status === 504 || hasOutcomeUnknownCode(error.detail)) {
      return {
        message:
          "The request ended before Orcastra could confirm the result. Check this instance's snapshots before trying again.",
        outcomeUnknown: true,
      };
    }
    if (error.status === 403) {
      return {
        message: "You no longer have permission to take a snapshot of this instance.",
        outcomeUnknown: false,
      };
    }
    if (error.status === 409) {
      return { message: error.message, outcomeUnknown: false };
    }
    if (error.status === 422) {
      return {
        message: "The target or snapshot details are no longer valid. Review them and try again.",
        outcomeUnknown: false,
      };
    }
    if (error.status === 502 && hasTerminalFailureCode(error.detail)) {
      return {
        message:
          "The cluster could not complete the snapshot. Refresh the target before trying again.",
        outcomeUnknown: false,
      };
    }
    if (error.status >= 500) {
      return {
        message:
          "The request ended before Orcastra could confirm the result. Check this instance's snapshots before trying again.",
        outcomeUnknown: true,
      };
    }
    return { message: error.message, outcomeUnknown: false };
  }
  return {
    message:
      "The request ended before Orcastra could confirm the result. Check this instance's snapshots before trying again.",
    outcomeUnknown: true,
  };
}

/** Shared, stateless-only snapshot form used by fleet and per-instance entry points. */
export function SnapshotCreateForm({
  target,
  children,
  onCancel,
  onResult,
  onBusyChange,
  onViewSnapshots,
}: SnapshotCreateFormProps) {
  const createSnapshot = useCreateSnapshot();
  const { isReadOnly } = useReadOnlyMode();
  const [label, setLabel] = React.useState("");
  const [labelTouched, setLabelTouched] = React.useState(false);
  const [expiryDays, setExpiryDays] = React.useState(String(DEFAULT_EXPIRY_DAYS));
  const [reason, setReason] = React.useState("");
  const [reasonTouched, setReasonTouched] = React.useState(false);
  const [acknowledged, setAcknowledged] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [outcomeUnknown, setOutcomeUnknown] = React.useState(false);
  const [checkingSnapshots, setCheckingSnapshots] = React.useState(false);
  const [snapshotCheckError, setSnapshotCheckError] = React.useState<string | null>(null);
  const submitInFlightRef = React.useRef(false);

  const reviewedTargetRef = React.useRef<SnapshotCreateTarget | undefined>(undefined);
  if (target) reviewedTargetRef.current = target;
  // Keep the reviewed summary and draft visible after an ambiguous POST even when its explicit
  // verification read fails. Submission still keys off `target`, so stale display data can never
  // authorize another write. Changing an upstream selector remounts this keyed form and clears it.
  const presentedTarget = target ?? (outcomeUnknown ? reviewedTargetRef.current : undefined);
  const instance = presentedTarget?.instance;
  const project = presentedTarget?.project ?? "default";
  const billingLinked = presentedTarget?.billingLinked ?? false;
  const operatorCount = presentedTarget?.operatorCount ?? 0;
  const days = Number(expiryDays);
  const expiryValid = Number.isInteger(days) && days >= 1 && days <= MAX_EXPIRY_DAYS;
  const trimmedLabel = label.trim();
  const labelValid = trimmedLabel.length >= 1 && trimmedLabel.length <= 48;
  const trimmedReason = reason.trim();
  const reasonValid = trimmedReason.length >= 1 && trimmedReason.length <= 280;
  const billingReady = !billingLinked || (acknowledged && reasonValid);
  const capacityFull = operatorCount >= MAX_OPERATOR_SNAPSHOTS;
  const formBusy = createSnapshot.isPending || checkingSnapshots;
  const canSubmit =
    Boolean(target) &&
    labelValid &&
    expiryValid &&
    billingReady &&
    !capacityFull &&
    !isReadOnly &&
    !outcomeUnknown &&
    !formBusy;
  const running = String(instance?.status ?? "").toLowerCase() === "running";
  const labelError =
    labelTouched && !labelValid
      ? "Enter a label with at least one non-space character."
      : null;
  const reasonError =
    reasonTouched && billingLinked && !reasonValid
      ? "Enter a reason with at least one non-space character."
      : null;

  const handleViewSnapshots = async () => {
    if (checkingSnapshots) return;
    setCheckingSnapshots(true);
    setSnapshotCheckError(null);
    onBusyChange(true);
    try {
      await onViewSnapshots();
    } catch {
      setSnapshotCheckError(
        "The latest snapshot list could not be verified. The result is still unknown. Stay here and try again.",
      );
    } finally {
      setCheckingSnapshots(false);
      onBusyChange(false);
    }
  };

  const handleCreate = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLabelTouched(true);
    if (billingLinked) setReasonTouched(true);
    if (!target || !canSubmit || submitInFlightRef.current) return;

    submitInFlightRef.current = true;
    onBusyChange(true);
    setError(null);
    setSnapshotCheckError(null);

    let result: SnapshotCreateResponse | null = null;
    try {
      result = await createSnapshot.mutateAsync({
        instanceName: target.instance.name,
        body: {
          host: target.instance.host,
          project: target.project,
          label: trimmedLabel,
          expiry_days: days,
          stateful: false,
          reason: billingLinked ? trimmedReason : undefined,
          acknowledge_billing_impact: billingLinked ? acknowledged : undefined,
        },
      });
    } catch (caught) {
      const mapped = submitError(caught);
      setError(mapped.message);
      setOutcomeUnknown(mapped.outcomeUnknown);
    } finally {
      submitInFlightRef.current = false;
      onBusyChange(false);
    }

    if (result) onResult(result);
  };

  return (
    <form className="contents" onSubmit={handleCreate}>
      <DialogBody className="space-y-4 pr-2">
        {children}

        {presentedTarget && instance && (
          <>
        <section className="space-y-3 rounded-md border bg-muted/30 p-3" aria-label="Target">
          <DetailGrid cols={2}>
            <DetailField
              label="Cluster"
              value={
                <OverflowText
                  value={instance.host}
                  label="cluster name"
                  lines="none"
                  mono
                  copy="none"
                />
              }
            />
            <DetailField
              label="Project"
              value={
                <OverflowText
                  value={project}
                  label="project name"
                  lines="none"
                  mono
                  copy="none"
                />
              }
            />
            <DetailField
              label="Instance"
              value={
                <OverflowText
                  value={instance.name}
                  label="instance name"
                  lines="none"
                  mono
                  copy="none"
                />
              }
            />
            <DetailField
              label="Type and Status"
              value={`${formatInstanceType(instance.type)} · ${instanceStatusDisplay(instance.status).label}`}
            />
          </DetailGrid>
          <p className="text-xs text-muted-foreground">
            {operatorCount} of {MAX_OPERATOR_SNAPSHOTS} operator snapshots used for this instance.
          </p>
        </section>

        {capacityFull && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertTitle>Snapshot limit reached</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>
                This instance already has {MAX_OPERATOR_SNAPSHOTS} operator snapshots. Review the
                existing restore points before removing one.
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void handleViewSnapshots()}
                disabled={checkingSnapshots}
                aria-busy={checkingSnapshots}
              >
                {checkingSnapshots ? "Checking snapshots" : "View existing snapshots"}
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {running && (
          <Alert>
            <Info className="h-4 w-4" aria-hidden />
            <AlertTitle>Running workload</AlertTitle>
            <AlertDescription>
              This stateless snapshot does not capture guest memory. Quiesce the workload first
              when you need application-consistent data.
            </AlertDescription>
          </Alert>
        )}

        <div className="space-y-2">
          <Label htmlFor="snapshot-label">What is this for</Label>
          <Input
            id="snapshot-label"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            onBlur={() => setLabelTouched(true)}
            placeholder="before kernel upgrade"
            maxLength={48}
            autoComplete="off"
            required
            disabled={formBusy}
            aria-invalid={Boolean(labelError)}
            aria-describedby={
              labelError ? "snapshot-label-help snapshot-label-error" : "snapshot-label-help"
            }
          />
          <p id="snapshot-label-help" className="text-xs text-muted-foreground">
            The server generates <span className="font-mono">ops-&lt;label&gt;-&lt;timestamp&gt;</span>.
            Use a short reason that will still make sense during recovery.
          </p>
          {labelError && (
            <p id="snapshot-label-error" className="text-xs text-destructive">
              {labelError}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <Label htmlFor="snapshot-expiry">Keep for (days)</Label>
          <Input
            id="snapshot-expiry"
            type="number"
            min={1}
            max={MAX_EXPIRY_DAYS}
            value={expiryDays}
            onChange={(event) => setExpiryDays(event.target.value)}
            required
            disabled={formBusy}
            aria-invalid={!expiryValid}
            aria-describedby={
              expiryValid
                ? "snapshot-expiry-help"
                : "snapshot-expiry-help snapshot-expiry-error"
            }
          />
          <p id="snapshot-expiry-help" className="text-xs text-muted-foreground">
            Choose 1 to {MAX_EXPIRY_DAYS} days. The hypervisor removes the snapshot after its
            expiry.
          </p>
          {!expiryValid && (
            <p id="snapshot-expiry-error" className="text-xs text-destructive">
              Choose between 1 and {MAX_EXPIRY_DAYS} days.
            </p>
          )}
        </div>

        {billingLinked && (
          <div className="space-y-3 rounded-md border border-destructive/40 p-3">
            <div className="flex gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden />
              <p className="text-sm">
                Snapshots on this instance are managed and billed by another product. Taking one
                here can affect the storage measured by that schedule.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="snapshot-reason">Reason</Label>
              <Textarea
                id="snapshot-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                onBlur={() => setReasonTouched(true)}
                placeholder="Why this operator snapshot is needed now"
                maxLength={280}
                rows={3}
                required
                disabled={formBusy}
                aria-invalid={Boolean(reasonError)}
                aria-describedby={reasonError ? "snapshot-reason-error" : undefined}
              />
              {reasonError && (
                <p id="snapshot-reason-error" className="text-xs text-destructive">
                  {reasonError}
                </p>
              )}
            </div>

            <div className="flex items-start gap-3">
              <Checkbox
                id="snapshot-acknowledge"
                checked={acknowledged}
                onCheckedChange={(checked) => setAcknowledged(checked === true)}
                disabled={formBusy}
              />
              <Label htmlFor="snapshot-acknowledge" className="font-normal">
                I accept the effect on the schedule this instance is billed under
              </Label>
            </div>
          </div>
        )}

        <div className="flex gap-3 rounded-md border bg-muted/30 p-3 text-sm">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <p className="text-muted-foreground">
            This restore point stays in the same storage pool. It is not an off-host backup and
            does not include attached custom storage volumes.
          </p>
        </div>

        {isReadOnly && (
          <p
            id="snapshot-create-read-only-reason"
            role="status"
            className="text-sm text-muted-foreground"
          >
            Snapshot creation is disabled while Orcastra is in read-only mode.
          </p>
        )}

          </>
        )}

      </DialogBody>

      {/* Keep an ambiguous outcome and the reviewed draft visible even when its verification
          read invalidates the active target. Submission remains fail-closed because canSubmit
          still requires the live target, while the recovery action is read-only. */}
      <DialogSubmitError
        message={error}
        title={outcomeUnknown ? "Snapshot result unknown" : "Snapshot not taken"}
      >
        {outcomeUnknown && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void handleViewSnapshots()}
            disabled={checkingSnapshots}
            aria-busy={checkingSnapshots}
          >
            {checkingSnapshots ? "Checking snapshots" : "Check snapshots"}
          </Button>
        )}
      </DialogSubmitError>
      <DialogSubmitError message={snapshotCheckError} />

      <DialogFooter className="shrink-0 gap-2 pt-4 sm:space-x-0">
        <Button
          type="button"
          variant="outline"
          onClick={onCancel}
          disabled={createSnapshot.isPending || checkingSnapshots}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          disabled={!canSubmit}
          aria-busy={createSnapshot.isPending}
          aria-describedby={isReadOnly ? "snapshot-create-read-only-reason" : undefined}
        >
          {createSnapshot.isPending
            ? "Taking snapshot"
            : billingLinked
              ? "Snapshot anyway"
              : "Take Snapshot"}
        </Button>
      </DialogFooter>
    </form>
  );
}
