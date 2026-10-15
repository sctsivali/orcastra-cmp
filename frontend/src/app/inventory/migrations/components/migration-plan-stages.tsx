"use client";

import type { NetworkInfo } from "@/types/api";
import type { MigrationPlan } from "@/types/migrations";
import { planContracts } from "./migration-format";
import { MigrationMappingsEditor } from "./migration-mappings-editor";
import { MigrationStagePreflight, MigrationStageStrategy } from "./migration-stage-overview";
import { MigrationStageRecovery } from "./migration-stage-recovery";
import { MigrationStageReview } from "./migration-stage-review";
import type { MigrationDraftState } from "./migration-wizard-model";

export type MigrationPlanStage = "strategy" | "mappings" | "preflight" | "recovery" | "review";

export function MigrationPlanStages({
  stage,
  plan,
  draft,
  onChange,
  networks,
  networksLoading,
}: {
  stage: MigrationPlanStage;
  plan: MigrationPlan;
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
  networks: NetworkInfo[];
  networksLoading: boolean;
}) {
  if (stage === "strategy") return <MigrationStageStrategy plan={plan} />;
  if (stage === "mappings") {
    return (
      <MigrationMappingsEditor
        mappings={draft.mappings}
        capabilities={plan.capabilities ?? []}
        contracts={planContracts(plan.resources ?? [])}
        networks={networks}
        networksLoading={networksLoading}
        onChange={(mappings) => onChange({ mappings })}
      />
    );
  }
  if (stage === "preflight") return <MigrationStagePreflight plan={plan} />;
  if (stage === "recovery") return <MigrationStageRecovery plan={plan} draft={draft} onChange={onChange} />;
  return <MigrationStageReview plan={plan} draft={draft} onChange={onChange} />;
}
