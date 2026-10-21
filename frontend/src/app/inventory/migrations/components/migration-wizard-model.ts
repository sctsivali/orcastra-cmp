import type {
  MigrationIntent,
  MigrationPlanCreateRequest,
  MigrationScope,
} from "@/types/migrations";
import { emptyMigrationMappings, type MigrationMappings } from "@/types/migrations";

/** How a whole project moves: its workloads to a member, to another cluster, or renamed. */
export type ProjectRelocationMode = "member" | "cluster" | "rename";

export interface MigrationWizardSeed {
  scope?: MigrationScope;
  intent?: MigrationIntent;
  sourceCluster?: string;
  sourceProject?: string;
  resourceName?: string;
  sourceMember?: string;
  mode?: ProjectRelocationMode;
}

export interface MigrationDraftState {
  scope: MigrationScope;
  intent: MigrationIntent;
  projectMode: ProjectRelocationMode;
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
/** `?step=` values, in step order, so a refreshed or shared plan link reopens where it was. */
export const PLANNING_STEP_KEYS = [
  "source",
  "destination",
  "strategy",
  "mappings",
  "preflight",
  "recovery",
  "review",
] as const;

export function planningStepFromKey(key: string | null | undefined): number | null {
  const index = PLANNING_STEP_KEYS.indexOf(key as (typeof PLANNING_STEP_KEYS)[number]);
  return index >= 0 ? index : null;
}
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

/** The destination a project relocation mode implies before the operator edits it. */
export function projectModeTarget(
  mode: ProjectRelocationMode,
  sourceCluster: string,
  sourceProject: string,
): Pick<MigrationDraftState, "targetCluster" | "targetProject" | "targetMember"> {
  if (mode === "member") return { targetCluster: sourceCluster, targetProject: sourceProject, targetMember: "" };
  if (mode === "rename") return { targetCluster: sourceCluster, targetProject: "", targetMember: "" };
  return { targetCluster: "", targetProject: sourceProject, targetMember: "" };
}

export function projectModeOf(target: { cluster_id: string; project: string }, source: { cluster_id: string; project: string }): ProjectRelocationMode {
  if (target.cluster_id !== source.cluster_id) return "cluster";
  return target.project === source.project ? "member" : "rename";
}

export const PROJECT_MODE_LABEL: Record<ProjectRelocationMode, string> = {
  member: "Move Workloads to Another Member",
  cluster: "Move Project to Another Cluster",
  rename: "Rename Project on This Cluster",
};

export const PROJECT_MODE_HELP: Record<ProjectRelocationMode, string> = {
  member:
    "Every workload keeps its name and identity and moves to the chosen member, one at a time. If one cannot move, the others go back.",
  cluster:
    "The project, its profiles, and every instance are recreated on the other cluster. The stopped source project is kept for rollback until you finalize.",
  rename:
    "The project is recreated under a new name on this cluster with its profiles and instances. The stopped source project is kept for rollback until you finalize.",
};

/** Why a project relocation destination cannot be planned, or undefined when it can. */
export function projectDestinationProblem(
  draft: MigrationDraftState,
  existingTargetProjects: readonly string[],
): string | undefined {
  const name = draft.targetProject.trim();
  if (draft.projectMode === "member") {
    return draft.targetMember.trim() ? undefined : "Choose the member to move the workloads to.";
  }
  if (!draft.targetCluster) return "Choose a destination cluster.";
  if (draft.projectMode === "cluster" && draft.targetCluster === draft.sourceCluster) {
    return "Choose another cluster, or rename the project on this one instead.";
  }
  if (!name) return "Enter a name for the new project.";
  if (!LXD_NAME_PATTERN.test(name)) {
    return "Use letters, digits, and dashes, starting with a letter or digit, up to 63 characters.";
  }
  if (draft.projectMode === "rename" && name === draft.sourceProject.trim()) {
    return "Choose a name other than the current one.";
  }
  if (existingTargetProjects.includes(name)) {
    return `Project '${name}' already exists there. A relocation creates the project and never merges into an existing one.`;
  }
  return undefined;
}

export function initialMigrationDraft(
  seed: MigrationWizardSeed = {},
  defaultRetentionHours = 24,
): MigrationDraftState {
  const scope = seed.scope ?? "instance";
  const projectMode = seed.mode ?? "cluster";
  const projectTarget =
    scope === "project" ? projectModeTarget(projectMode, seed.sourceCluster ?? "", seed.sourceProject ?? "") : null;
  return {
    scope,
    intent: scope === "project" ? "move" : seed.intent ?? "move",
    projectMode,
    sourceCluster: seed.sourceCluster ?? "",
    sourceProject: seed.sourceProject ?? "default",
    resourceName: seed.resourceName ?? "",
    sourceMember: seed.sourceMember ?? "",
    targetCluster: projectTarget?.targetCluster ?? "",
    targetProject: projectTarget?.targetProject ?? "",
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
    projectMode: projectModeOf(plan.target, plan.source),
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
