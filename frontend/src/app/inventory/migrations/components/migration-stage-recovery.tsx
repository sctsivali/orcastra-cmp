"use client";

import { ShieldCheck, Undo2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { MigrationPlan } from "@/types/migrations";
import {
  MAX_RETENTION_HOURS,
  MIN_RETENTION_HOURS,
  parseRetention,
  type MigrationDraftState,
} from "./migration-wizard-model";

export function MigrationStageRecovery({
  plan,
  draft,
  onChange,
}: {
  plan: MigrationPlan;
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
}) {
  if (draft.intent === "copy") {
    return (
      <Alert>
        <ShieldCheck className="h-4 w-4" aria-hidden />
        <AlertTitle>The source is not changed</AlertTitle>
        <AlertDescription>
          A copy creates a new, independent instance. A running source is stopped only for the
          transfer and started again afterwards. There is no retention window, rollback, or
          cleanup for it.
        </AlertDescription>
      </Alert>
    );
  }

  if (plan.strategy === "member_relocation") {
    return (
      <Alert>
        <ShieldCheck className="h-4 w-4" aria-hidden />
        <AlertTitle>No retained copy</AlertTitle>
        <AlertDescription>
          Member relocation moves the same instance to another server, so no second copy is kept.
          To undo it, plan another relocation back to the original member.
        </AlertDescription>
      </Alert>
    );
  }

  const parsed = parseRetention(draft.retentionInput);
  const invalid = draft.retentionInput.trim().length > 0 && parsed === null;
  const automaticCleanup = plan.rollback_contract?.automatic_cleanup === true;
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="migration-retention">Keep the stopped source for (hours)</Label>
        <Input
          id="migration-retention"
          inputMode="numeric"
          value={draft.retentionInput}
          onChange={(event) => {
            const retentionInput = event.target.value;
            const hours = parseRetention(retentionInput);
            onChange(hours === null ? { retentionInput } : { retentionInput, retentionHours: hours });
          }}
          aria-invalid={invalid || draft.retentionInput.trim().length === 0}
          aria-describedby="migration-retention-help"
          className="max-w-[12rem]"
        />
        {(invalid || draft.retentionInput.trim().length === 0) && (
          <p className="text-xs text-destructive">
            Enter a whole number from {MIN_RETENTION_HOURS} to {MAX_RETENTION_HOURS}.
          </p>
        )}
        <p id="migration-retention-help" className="text-xs text-muted-foreground">
          {automaticCleanup
            ? "After this window the source is deleted automatically, once Orcastra checks it is unchanged and the target is healthy. Finalize deletes it earlier."
            : "After this window, use Finalize to delete the source. Nothing is deleted automatically on this deployment."}
        </p>
      </div>

      <Alert>
        <Undo2 className="h-4 w-4" aria-hidden />
        <AlertTitle>Rollback stays available during retention</AlertTitle>
        <AlertDescription>
          Rollback deletes the new instance and restores the source, including its running state
          and Orcastra records such as tags, schedules, and access rules. Anything written to the
          new instance after cutover is lost on rollback.
        </AlertDescription>
      </Alert>
    </div>
  );
}
