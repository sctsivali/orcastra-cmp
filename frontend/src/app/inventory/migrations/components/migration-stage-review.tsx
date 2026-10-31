"use client";

import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { formatLabel, plural } from "@/lib/utils";
import type { MigrationPlan } from "@/types/migrations";
import { endpointLabel, planHasRunningSource } from "./migration-format";
import { MigrationFindings } from "./migration-findings";
import { SummaryField } from "./migration-summary-field";
import type { MigrationDraftState } from "./migration-wizard-model";

export const MIGRATION_REASON_MAX = 1000;

/** Mirrors the server's required acknowledgements (services/migrations/billing.py). */
export function requiredAcknowledgements(
  draft: MigrationDraftState,
  plan?: Pick<MigrationPlan, "strategy" | "resources"> & Partial<Pick<MigrationPlan, "warnings">> | null,
): string[] {
  const values = [draft.intent === "move" ? "downtime-reviewed" : "copy-impact-reviewed", "billing-impact-reviewed"];
  if (draft.intent === "copy" && plan && planHasRunningSource(plan.resources ?? [])) {
    values.push("downtime-reviewed");
  }
  if (draft.intent === "move" && plan?.strategy !== "member_relocation") {
    values.push("source-retention-understood");
  }
  if (plan?.strategy === "project_relocation") {
    values.push("source-project-deletion-understood");
  }
  if (plan?.warnings?.some((warning) => warning.code === "source_size_unmeasured")) {
    values.push("unmeasured-size-accepted");
  }
  return values;
}

function toggleValue(values: string[], value: string, checked: boolean): string[] {
  if (checked) return values.includes(value) ? values : [...values, value];
  return values.filter((item) => item !== value);
}

const ACK_LABEL: Record<string, string> = {
  "downtime-reviewed": "I reviewed the downtime. A running source is stopped for the transfer.",
  "copy-impact-reviewed": "I reviewed the transfer impact. The copy is a new, independent instance.",
  "source-retention-understood":
    "I understand the source is kept stopped for the retention window and then deleted.",
  "source-project-deletion-understood":
    "I understand that Finalize, or the end of retention, deletes the whole source project: its instances, profiles, and cached images.",
  "billing-impact-reviewed": "I reviewed snapshot schedule and billing impact for every source workload.",
  "unmeasured-size-accepted":
    "I accept that this transfer's size cannot be measured on a dir pool and may fill the target pool.",
};

export function MigrationStageReview({
  plan,
  draft,
  onChange,
}: {
  plan: MigrationPlan;
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
}) {
  const required = requiredAcknowledgements(draft, plan);
  const memberRelocation = plan.strategy === "member_relocation";
  const reasonLength = draft.reason.trim().length;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <SummaryField label="Source" value={endpointLabel(plan.source)} mono />
        <SummaryField label="Destination" value={endpointLabel(plan.target)} mono />
        <SummaryField label="Operation" value={`${formatLabel(plan.intent)} ${formatLabel(plan.scope)}`} />
        <SummaryField
          label={draft.intent === "copy" ? "Source after copy" : memberRelocation ? "Recovery" : "Retention"}
          value={
            draft.intent === "copy"
              ? "Unchanged and independent"
              : memberRelocation
                ? plan.scope === "project"
                  ? "Same instances, moved back automatically on failure"
                  : "Same instance, no retained copy"
                : plural(draft.retentionHours, "hour")
          }
        />
      </div>

      <MigrationFindings blockers={plan.blockers} warnings={plan.warnings} />

      <div className="space-y-2">
        <Label htmlFor="migration-reason">Reason for This Migration</Label>
        <Textarea
          id="migration-reason"
          value={draft.reason}
          onChange={(event) => onChange({ reason: event.target.value })}
          maxLength={MIGRATION_REASON_MAX}
          rows={3}
          placeholder="Describe the operational reason and change reference"
          aria-invalid={reasonLength > 0 && reasonLength < 8}
        />
        {reasonLength > 0 && reasonLength < 8 && (
          <p className="text-xs text-destructive">Enter at least 8 characters.</p>
        )}
        <p className="text-xs text-muted-foreground">
          Recorded in the audit trail with this exact plan revision.
        </p>
      </div>

      <div className="space-y-3 rounded-md border p-3">
        {required.map((id) => (
          <div key={id} className="flex items-start gap-3">
            <Checkbox
              id={`ack-${id}`}
              checked={draft.acknowledgements.includes(id)}
              onCheckedChange={(value) =>
                onChange({ acknowledgements: toggleValue(draft.acknowledgements, id, value === true) })
              }
            />
            <Label htmlFor={`ack-${id}`} className="font-normal leading-5">
              {ACK_LABEL[id] ?? formatLabel(id)}
            </Label>
          </div>
        ))}
      </div>

      {required.some((value) => !draft.acknowledgements.includes(value)) && (
        <Alert>
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertTitle>Review acknowledgements</AlertTitle>
          <AlertDescription>Every acknowledgement above is required before execution.</AlertDescription>
        </Alert>
      )}
    </div>
  );
}
