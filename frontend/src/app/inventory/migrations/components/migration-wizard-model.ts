import type {
  MigrationIntent,
  MigrationPlanCreateRequest,
  MigrationScope,
} from "@/types/migrations";
import { emptyMigrationMappings, type MigrationMappings } from "@/types/migrations";

export interface MigrationWizardSeed {
  scope?: MigrationScope;
  intent?: MigrationIntent;
  sourceCluster?: string;
  sourceProject?: string;
  resourceName?: string;
  sourceMember?: string;
  mode?: "member" | "cluster";
}

export interface MigrationDraftState {
  scope: MigrationScope;
  intent: MigrationIntent;
  sourceCluster: string;
  sourceProject: string;
  resourceName: string;
  sourceMember: string;
  targetCluster: string;
  targetProject: string;
  targetResourceName: string;
  targetMember: string;
  targetStoragePool: string;
  mappings: MigrationMappings;
  retentionHours: number;
  /** What the operator typed. Only a valid value updates retentionHours. */
  retentionInput: string;
  checkpointRequested: boolean;
  externalCutoverRequired: boolean;
  reason: string;
  acknowledgements: string[];
}

export const MIGRATION_STEPS = [
  "Source",
  "Destination",
  "Strategy",
  "Mappings",
  "Preflight",
  "Recovery",
  "Review",
  "Execution",
  "Cutover",
  "Rollback and Cleanup",
] as const;

/** The steps the wizard itself walks. The rest describe what happens after Start. */
export const PLANNING_STEP_COUNT = 7;
export const MIN_RETENTION_HOURS = 1;
export const MAX_RETENTION_HOURS = 168;

export const LXD_NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$/;

/** Suggested name for a copy. A name that would exceed the LXD limit is left for the operator. */
export function defaultCopyName(name: string): string {
  const candidate = `${name}-copy`;
  return LXD_NAME_PATTERN.test(candidate) ? candidate : name;
}

export function parseRetention(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const hours = Number(value.trim());
  return hours >= MIN_RETENTION_HOURS && hours <= MAX_RETENTION_HOURS ? hours : null;
}

/** Whether the draft points at the instance or project it starts from. */
export function isSameLocation(draft: MigrationDraftState): boolean {
  const targetMember = draft.targetMember.trim();
  return (
    draft.targetCluster === draft.sourceCluster &&
    draft.targetProject.trim() === draft.sourceProject.trim() &&
    (draft.scope === "project" || draft.targetResourceName.trim() === draft.resourceName.trim()) &&
    (!targetMember || targetMember === draft.sourceMember.trim())
  );
}

export function initialMigrationDraft(
  seed: MigrationWizardSeed = {},
  defaultRetentionHours = 24,
): MigrationDraftState {
  return {
    scope: seed.scope ?? "instance",
    intent: seed.intent ?? "move",
    sourceCluster: seed.sourceCluster ?? "",
    sourceProject: seed.sourceProject ?? "default",
    resourceName: seed.resourceName ?? "",
    sourceMember: seed.sourceMember ?? "",
    targetCluster: seed.mode === "member" ? seed.sourceCluster ?? "" : "",
    targetProject: seed.scope === "project" ? seed.sourceProject ?? "" : "",
    targetResourceName: seed.resourceName
      ? seed.intent === "copy"
        ? defaultCopyName(seed.resourceName)
        : seed.resourceName
      : "",
    targetMember: "",
    targetStoragePool: "",
    mappings: emptyMigrationMappings(),
    retentionHours: defaultRetentionHours,
    retentionInput: String(defaultRetentionHours),
    checkpointRequested: false,
    externalCutoverRequired: false,
    reason: "",
    acknowledgements: [],
  };
}

export function draftFromPlan(plan: {
  scope: MigrationScope;
  intent: MigrationIntent;
  source: MigrationPlanCreateRequest["source"];
  target: MigrationPlanCreateRequest["target"];
  mappings?: MigrationMappings;
  retention_hours: number;
  checkpoint_requested: boolean;
  external_cutover_required: boolean;
}): MigrationDraftState {
  return {
    scope: plan.scope,
    intent: plan.intent,
    sourceCluster: plan.source.cluster_id,
    sourceProject: plan.source.project,
    resourceName: plan.source.resource_name ?? "",
    sourceMember: plan.source.member ?? "",
    targetCluster: plan.target.cluster_id,
    targetProject: plan.target.project,
    targetResourceName: plan.target.resource_name ?? plan.source.resource_name ?? "",
    targetMember: plan.target.member ?? "",
    targetStoragePool: plan.mappings?.storage_pools?.root ?? plan.mappings?.storage_pools?.["*"] ?? "",
    mappings: plan.mappings ?? emptyMigrationMappings(),
    retentionHours: plan.retention_hours,
    retentionInput: String(plan.retention_hours),
    checkpointRequested: plan.checkpoint_requested,
    externalCutoverRequired: plan.external_cutover_required,
    reason: "",
    acknowledgements: [],
  };
}

export function createPlanBody(draft: MigrationDraftState): MigrationPlanCreateRequest {
  const resourceName = draft.scope === "instance" ? draft.resourceName.trim() : undefined;
  return {
    scope: draft.scope,
    intent: draft.intent,
    source: {
      cluster_id: draft.sourceCluster,
      project: draft.sourceProject,
      resource_name: resourceName,
      member: draft.scope === "instance" ? draft.sourceMember.trim() || undefined : undefined,
    },
    target: {
      cluster_id: draft.targetCluster,
      project: draft.targetProject.trim(),
      resource_name:
        draft.scope === "instance"
          ? draft.targetResourceName.trim() || resourceName
          : undefined,
      member: draft.targetMember.trim() || undefined,
    },
    mappings: {
      ...draft.mappings,
      storage_pools: draft.targetStoragePool.trim()
        ? { ...draft.mappings.storage_pools, root: draft.targetStoragePool.trim() }
        : draft.mappings.storage_pools,
    },
    retention_hours: draft.retentionHours,
    checkpoint_requested: draft.checkpointRequested,
    external_cutover_required: draft.externalCutoverRequired,
  };
}
