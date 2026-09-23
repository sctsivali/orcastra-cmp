"use client";

import * as React from "react";
import { AlertTriangle, CheckCircle2, Clock3, Info, Route, ShieldCheck } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { formatDateTime, formatLabel } from "@/lib/utils";
import type { MigrationPlan } from "@/types/migrations";
import { endpointLabel } from "./migration-format";
import { MigrationFindings } from "./migration-findings";
import { MigrationMappingsEditor } from "./migration-mappings-editor";
import { MigrationResourceTable } from "./migration-resource-table";
import type { MigrationDraftState } from "./migration-wizard-model";
import { jsonScalar } from "./migration-format";

interface MigrationPlanStagesProps {
  stage: "strategy" | "mappings" | "preflight" | "recovery" | "review";
  plan: MigrationPlan;
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
}

function toggleValue(values: string[], value: string, checked: boolean): string[] {
  if (checked) return values.includes(value) ? values : [...values, value];
  return values.filter((item) => item !== value);
}

export function requiredAcknowledgements(
  draft: MigrationDraftState,
  strategy?: string | null,
): string[] {
  const values = [
    draft.intent === "move" ? "downtime-reviewed" : "copy-impact-reviewed",
    "billing-impact-reviewed",
  ];
  if (draft.intent === "move" && strategy !== "member_relocation") {
    values.push("source-retention-understood");
  }
  if (draft.externalCutoverRequired) values.push("external-cutover-owned");
  return values;
}

export function MigrationPlanStages({ stage, plan, draft, onChange }: MigrationPlanStagesProps) {
  const memberRelocation = plan.strategy === "member_relocation";
  const externalCutoverCapability = plan.capabilities?.find(
    (capability) => capability.code === "external_cutover_webhook",
  );
  const externalCutoverSupported = externalCutoverCapability?.supported === true;

  React.useEffect(() => {
    const patch: Partial<MigrationDraftState> = {};
    if (draft.intent === "copy" && draft.checkpointRequested) patch.checkpointRequested = false;
    if (
      draft.externalCutoverRequired &&
      (draft.intent === "copy" || memberRelocation || !externalCutoverSupported)
    ) {
      patch.externalCutoverRequired = false;
    }
    if (Object.keys(patch).length) onChange(patch);
  }, [draft.checkpointRequested, draft.externalCutoverRequired, draft.intent, externalCutoverSupported, memberRelocation, onChange]);

  if (stage === "strategy") {
    const rollbackSummary = draft.intent === "copy"
      ? "Source remains active"
      : memberRelocation
        ? "No retained source"
        : jsonScalar(plan.rollback_contract.reason) || "Manual recovery only";
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <SummaryField label="Strategy" value={plan.strategy ? formatLabel(plan.strategy) : "Capability driven"} />
          <SummaryField label="Downtime" value={plan.downtime_class ? formatLabel(plan.downtime_class) : "Calculated at cutover"} />
          <SummaryField label={draft.intent === "copy" ? "Source" : "Recovery"} value={rollbackSummary} />
        </div>

        <Alert>
          <Info className="h-4 w-4" aria-hidden />
          <AlertTitle>{draft.intent === "copy" ? "Independent destination copy" : "No zero-downtime promise"}</AlertTitle>
          <AlertDescription>
            {draft.intent === "copy"
              ? "Copy does not cut over traffic or stop and fence the source. The server still validates transfer compatibility before creating an independent destination."
              : "This release uses staged cold cutover. The server validates daemon capabilities, storage, workload type, and device dependencies again before cutover."}
          </AlertDescription>
        </Alert>

        <div className="space-y-2">
          <h3 className="text-sm font-semibold">Capabilities</h3>
          {plan.capabilities?.length ? (
            <div className="divide-y rounded-md border">
              {plan.capabilities.map((capability) => (
                <div key={capability.code} className="flex items-start justify-between gap-4 p-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium">{formatLabel(capability.code)}</p>
                    {capability.reason && (
                      <p className="mt-0.5 text-xs text-muted-foreground">{capability.reason}</p>
                    )}
                  </div>
                  <Badge variant={capability.supported ? "positive" : "neutral"}>
                    {capability.supported ? "Supported" : "Unavailable"}
                  </Badge>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Route}
              title="No capability detail reported"
              description="The server selected a safe baseline strategy without optional accelerators."
              className="py-8 rounded-md border"
            />
          )}
        </div>
      </div>
    );
  }

  if (stage === "mappings") {
    return (
      <div className="space-y-4">
        <Alert>
          <Route className="h-4 w-4" aria-hidden />
          <AlertTitle>Infrastructure is mapped, never cloned blindly</AlertTitle>
          <AlertDescription>
            Resolve every required pool, network, profile, device, and external dependency. An
            empty required target will block the next preflight.
          </AlertDescription>
        </Alert>

        <MigrationMappingsEditor
          mappings={draft.mappings}
          capabilities={plan.capabilities}
          onChange={(mappings) => onChange({ mappings })}
        />
      </div>
    );
  }

  if (stage === "preflight") {
    const expired = Date.parse(plan.expires_at) <= Date.now();
    return (
      <div className="space-y-4">
        {draft.intent === "move" && plan.strategy !== "member_relocation" && (
          <label className="flex items-start gap-3 rounded-md border p-3">
            <Checkbox
              checked={draft.checkpointRequested}
              onCheckedChange={(checked) => onChange({ checkpointRequested: checked === true })}
            />
            <span className="text-sm">
              <span className="font-medium">Create a pre-cutover checkpoint</span>
              <span className="mt-1 block text-xs text-muted-foreground">
                The checkpoint is infrastructure-level and crash-consistent unless the workload is
                stopped or explicitly quiesced.
              </span>
            </span>
          </label>
        )}

        {plan.strategy === "member_relocation" && (
          <Alert>
            <AlertTitle>Migration checkpoint unavailable</AlertTitle>
            <AlertDescription>
              Member relocation does not create an automatic checkpoint. Use a separately reviewed
              snapshot procedure if your recovery policy requires one.
            </AlertDescription>
          </Alert>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
          <div>
            <p className="text-sm font-medium">Plan revision {plan.revision}</p>
            <p className="text-xs text-muted-foreground">Expires {formatDateTime(plan.expires_at)}</p>
          </div>
          <Badge variant={expired ? "negative" : plan.blockers?.length ? "negative" : "positive"}>
            {expired ? "Expired" : plan.blockers?.length ? "Blocked" : "Ready"}
          </Badge>
        </div>
        {expired && (
          <Alert variant="destructive">
            <Clock3 className="h-4 w-4" aria-hidden />
            <AlertTitle>Preflight expired</AlertTitle>
            <AlertDescription>Go back and recheck mappings before execution.</AlertDescription>
          </Alert>
        )}
        <MigrationFindings blockers={plan.blockers} warnings={plan.warnings} showClear />
        <MigrationResourceTable resources={plan.resources ?? []} />
      </div>
    );
  }

  if (stage === "recovery") {
    if (draft.intent === "copy") {
      return (
        <Alert>
          <ShieldCheck className="h-4 w-4" aria-hidden />
          <AlertTitle>The source remains active</AlertTitle>
          <AlertDescription>
            Copy creates an independent destination. It does not create source fencing, a retention
            countdown, a rollback window, an external cutover workflow, or a source cleanup action.
          </AlertDescription>
        </Alert>
      );
    }

    if (memberRelocation) {
      return (
        <Alert>
          <ShieldCheck className="h-4 w-4" aria-hidden />
          <AlertTitle>No retained source or rollback window</AlertTitle>
          <AlertDescription>
            Member relocation moves the same instance identity to another cluster member. It does
            not create a second source, retention countdown, rollback action, checkpoint, external
            traffic cutover, or cleanup action. A reversal requires a separate relocation plan.
          </AlertDescription>
        </Alert>
      );
    }

    const externalCutoverReason = externalCutoverCapability?.reason
      || "The server did not advertise signed external cutover webhook support.";
    return (
      <div className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="migration-retention">Source retention in hours</Label>
          <Input
            id="migration-retention"
            type="number"
            min={1}
            max={168}
            value={draft.retentionHours}
            onChange={(event) =>
              onChange({ retentionHours: Math.min(168, Math.max(1, Number(event.target.value) || 1)) })
            }
          />
          <p className="text-xs text-muted-foreground">
            Default is 24 hours. The source stays stopped and fenced. Cleanup still requires a
            separate approval after the deadline.
          </p>
        </div>

        <label className="flex items-start gap-3 rounded-md border p-3">
          <Checkbox
            checked={draft.externalCutoverRequired}
            disabled={!externalCutoverSupported}
            onCheckedChange={(checked) => onChange({ externalCutoverRequired: checked === true })}
          />
          <span className="text-sm">
            <span className="font-medium">Require external traffic confirmation</span>
            <span className="mt-1 block text-xs text-muted-foreground">
              {externalCutoverSupported
                ? "Pause after target validation and wait for an authorized operator to confirm the configured DNS or load balancer cutover before source retention."
                : `Unavailable: ${externalCutoverReason}`}
            </span>
          </span>
        </label>

        <Alert>
          <ShieldCheck className="h-4 w-4" aria-hidden />
          <AlertTitle>Automated recovery is not released</AlertTitle>
          <AlertDescription>
            Automated rollback, checkpoint restore, and source cleanup are unavailable in this
            release. Keep the source fenced and use a separately reviewed recovery plan. A target
            that may have accepted writes requires reverse sync.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const required = requiredAcknowledgements(draft, plan.strategy);
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
              ? "Active and independent"
              : memberRelocation
                ? "No retained source"
                : `${draft.retentionHours} hours`
          }
        />
      </div>

      <MigrationFindings blockers={plan.blockers} warnings={plan.warnings} />

      <div className="space-y-2">
        <Label htmlFor="migration-reason">Reason for this migration</Label>
        <Textarea
          id="migration-reason"
          value={draft.reason}
          onChange={(event) => onChange({ reason: event.target.value })}
          maxLength={500}
          rows={3}
          placeholder="Describe the operational reason and change reference"
        />
        {draft.reason.trim().length > 0 && draft.reason.trim().length < 8 && (
          <p className="text-xs text-destructive">Enter at least 8 characters.</p>
        )}
        <p className="text-xs text-muted-foreground">
          The reason and acknowledgements are submitted at execution and are not stored in the draft.
        </p>
      </div>

      <div className="space-y-3 rounded-md border p-3">
        <Acknowledgement
          id={draft.intent === "move" ? "downtime-reviewed" : "copy-impact-reviewed"}
          checked={draft.acknowledgements.includes(
            draft.intent === "move" ? "downtime-reviewed" : "copy-impact-reviewed",
          )}
          onCheckedChange={(checked) =>
            onChange({
              acknowledgements: toggleValue(
                draft.acknowledgements,
                draft.intent === "move" ? "downtime-reviewed" : "copy-impact-reviewed",
                checked,
              ),
            })
          }
          label={
            draft.intent === "move"
              ? "I reviewed the downtime class and cutover sequence."
              : "I reviewed transfer impact and understand the source remains active."
          }
        />
        {draft.intent === "move" && !memberRelocation && (
          <Acknowledgement
            id="source-retention-understood"
            checked={draft.acknowledgements.includes("source-retention-understood")}
            onCheckedChange={(checked) =>
              onChange({
                acknowledgements: toggleValue(
                  draft.acknowledgements,
                  "source-retention-understood",
                  checked,
                ),
              })
            }
            label="I understand the retained source stays stopped and fenced."
          />
        )}
        <Acknowledgement
          id="billing-impact-reviewed"
          checked={draft.acknowledgements.includes("billing-impact-reviewed")}
          onCheckedChange={(checked) =>
            onChange({
              acknowledgements: toggleValue(
                draft.acknowledgements,
                "billing-impact-reviewed",
                checked,
              ),
            })
          }
          label="I reviewed snapshot schedule and billing impact for every source workload."
        />
        {draft.externalCutoverRequired && (
          <Acknowledgement
            id="external-cutover-owned"
            checked={draft.acknowledgements.includes("external-cutover-owned")}
            onCheckedChange={(checked) =>
              onChange({
                acknowledgements: toggleValue(
                  draft.acknowledgements,
                  "external-cutover-owned",
                  checked,
                ),
              })
            }
            label="An authorized operator owns the external DNS or load balancer cutover."
          />
        )}
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

function SummaryField({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-md border bg-muted/30 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-1 break-words text-sm font-medium ${mono ? "font-mono" : ""}`}>{value}</p>
    </div>
  );
}

function Acknowledgement({
  id,
  checked,
  onCheckedChange,
  label,
}: {
  id: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <Checkbox
        id={`ack-${id}`}
        checked={checked}
        onCheckedChange={(value) => onCheckedChange(value === true)}
      />
      <Label htmlFor={`ack-${id}`} className="font-normal leading-5">
        {label}
      </Label>
    </div>
  );
}
