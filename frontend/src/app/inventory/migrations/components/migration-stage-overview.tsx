"use client";

import { Clock3, Info, Power, Route } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { formatDateTime, formatLabel } from "@/lib/utils";
import type { MigrationPlan } from "@/types/migrations";
import { planHasRunningSource, strategyLabel } from "./migration-format";
import { MigrationFindings } from "./migration-findings";
import { MigrationResourceTable } from "./migration-resource-table";
import { SummaryField } from "./migration-summary-field";

function recoverySummary(plan: MigrationPlan): string {
  if (plan.intent === "copy") return "Source stays as it is";
  if (plan.strategy === "member_relocation") {
    return plan.scope === "project" ? "Same instances, moved back on failure" : "Same instance, relocated";
  }
  const hours = plan.rollback_contract?.retention_hours;
  const automatic = plan.rollback_contract?.automatic_cleanup === true;
  return `Source${plan.scope === "project" ? " project" : ""} kept ${typeof hours === "number" ? hours : plan.retention_hours} hours${
    automatic ? ", then deleted" : ", then Finalize"
  }`;
}

export function RunningSourceNotice({ plan }: { plan: MigrationPlan }) {
  if (!planHasRunningSource(plan.resources ?? [])) return null;
  if (plan.scope === "project") {
    const rolling = plan.strategy === "member_relocation";
    return (
      <Alert>
        <Power className="h-4 w-4" aria-hidden />
        <AlertTitle>Running instances will be stopped</AlertTitle>
        <AlertDescription>
          {rolling
            ? "Each running instance is shut down gracefully, moved, and started again before the next one, so one workload at a time is offline. Live migration is not used."
            : "Every running instance is shut down gracefully, and the new ones start once all of them are transferred and verified, in boot priority order. The project is offline for the whole transfer. Live migration is not used."}
        </AlertDescription>
      </Alert>
    );
  }
  const target = plan.intent === "move" ? "The target is started once it is verified." : "The source is started again when the copy is verified.";
  return (
    <Alert>
      <Power className="h-4 w-4" aria-hidden />
      <AlertTitle>The source is running and will be stopped</AlertTitle>
      <AlertDescription>
        Orcastra shuts it down gracefully, transfers it while stopped, and verifies the result. {target}{" "}
        It stays offline for the transfer time. Live migration is not used.
      </AlertDescription>
    </Alert>
  );
}

export function MigrationStageStrategy({ plan }: { plan: MigrationPlan }) {
  const running = planHasRunningSource(plan.resources ?? []);
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <SummaryField
          label="Strategy"
          value={strategyLabel(plan)}
        />
        <SummaryField label="Downtime" value={running ? "For the transfer time" : "None, the source is stopped"} />
        <SummaryField label={plan.intent === "copy" ? "Source" : "Recovery"} value={recoverySummary(plan)} />
      </div>

      <RunningSourceNotice plan={plan} />

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

export function MigrationStagePreflight({ plan }: { plan: MigrationPlan }) {
  const expired = Date.parse(plan.expires_at) <= Date.now();
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
        <div>
          <p className="text-sm font-medium">Plan revision {plan.revision}</p>
          <p className="text-xs text-muted-foreground">Expires {formatDateTime(plan.expires_at)}</p>
        </div>
        <Badge variant={expired || plan.blockers?.length ? "negative" : "positive"}>
          {expired ? "Expired" : plan.blockers?.length ? "Blocked" : "Ready"}
        </Badge>
      </div>
      {expired && (
        <Alert variant="destructive">
          <Clock3 className="h-4 w-4" aria-hidden />
          <AlertTitle>Preflight expired</AlertTitle>
          <AlertDescription>Use Recheck to run preflight again before execution.</AlertDescription>
        </Alert>
      )}
      <RunningSourceNotice plan={plan} />
      <MigrationFindings blockers={plan.blockers} warnings={plan.warnings} showClear />
      {plan.strategy === "member_relocation" && (
        <Alert>
          <Info className="h-4 w-4" aria-hidden />
          <AlertTitle>{plan.scope === "project" ? "The instances keep their identity" : "The instance keeps its identity"}</AlertTitle>
          <AlertDescription>
            Member relocation moves the same {plan.scope === "project" ? "instances" : "instance"} to
            another server of the cluster. Names, project, tags, and access rules do not change.
          </AlertDescription>
        </Alert>
      )}
      {plan.strategy === "project_relocation" && (
        <Alert>
          <Info className="h-4 w-4" aria-hidden />
          <AlertTitle>The project gets a new identity</AlertTitle>
          <AlertDescription>
            The new project is created with the source project&apos;s settings and every profile.
            Its tags and the access rules naming it, and each instance&apos;s tags, schedules, and
            rules, follow it at cutover.
          </AlertDescription>
        </Alert>
      )}
      <MigrationResourceTable resources={plan.resources ?? []} />
    </div>
  );
}
