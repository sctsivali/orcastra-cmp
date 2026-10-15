"use client";

import { Clock3, Info, Power, Route } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { formatDateTime, formatLabel } from "@/lib/utils";
import type { MigrationPlan } from "@/types/migrations";
import { planHasRunningSource } from "./migration-format";
import { MigrationFindings } from "./migration-findings";
import { MigrationResourceTable } from "./migration-resource-table";
import { SummaryField } from "./migration-summary-field";

const STRATEGY_LABEL: Record<string, string> = {
  same_cluster_copy: "Copy inside the cluster",
  cross_cluster_pull: "Transfer between clusters",
  member_relocation: "Relocate to another member",
};

function recoverySummary(plan: MigrationPlan): string {
  if (plan.intent === "copy") return "Source stays as it is";
  if (plan.strategy === "member_relocation") return "Same instance, relocated";
  const hours = plan.rollback_contract?.retention_hours;
  const automatic = plan.rollback_contract?.automatic_cleanup === true;
  return `Source kept ${typeof hours === "number" ? hours : plan.retention_hours} hours${
    automatic ? ", then deleted" : ", then Finalize"
  }`;
}

export function RunningSourceNotice({ plan }: { plan: MigrationPlan }) {
  if (!planHasRunningSource(plan.resources ?? [])) return null;
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
          value={plan.strategy ? STRATEGY_LABEL[plan.strategy] ?? formatLabel(plan.strategy) : "Decided by preflight"}
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
          <AlertTitle>The instance keeps its identity</AlertTitle>
          <AlertDescription>
            Member relocation moves the same instance to another server of the cluster. Its
            name, project, tags, and access rules do not change.
          </AlertDescription>
        </Alert>
      )}
      <MigrationResourceTable resources={plan.resources ?? []} />
    </div>
  );
}
