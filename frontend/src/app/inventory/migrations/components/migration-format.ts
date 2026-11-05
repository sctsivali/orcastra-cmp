import type { BadgeTone } from "@/components/ui/badge";
import { formatLabel } from "@/lib/utils";
import type {
  MigrationEndpointRef,
  MigrationIssue,
  MigrationJob,
  MigrationJobPhase,
  MigrationJobResource,
  MigrationJobStatus,
  MigrationContractSummary,
  MigrationJsonValue,
  MigrationPlannedResource,
  MigrationView,
} from "@/types/migrations";

const PHASE_META: Record<MigrationJobPhase, { label: string; tone: BadgeTone }> = {
  queued: { label: "Queued", tone: "caution" },
  preparing_target: { label: "Preparing Target", tone: "caution" },
  preparing_recovery: { label: "Preparing Recovery", tone: "caution" },
  checkpointing: { label: "Creating Checkpoint", tone: "caution" },
  initial_sync: { label: "Initial Sync", tone: "caution" },
  cutover_pending: { label: "Cutover Pending", tone: "caution" },
  quiescing: { label: "Quiescing", tone: "caution" },
  final_sync: { label: "Final Sync", tone: "caution" },
  target_validating: { label: "Validating Target", tone: "caution" },
  external_cutover_pending: { label: "External Cutover Pending", tone: "caution" },
  cutting_over: { label: "Cutting Over", tone: "caution" },
  verifying: { label: "Verifying", tone: "caution" },
  source_retained: { label: "Source Retained", tone: "positive" },
  cleanup_due: { label: "Cleanup Due", tone: "caution" },
  cleaning_up: { label: "Cleaning Up", tone: "caution" },
  unknown: { label: "Outcome Unknown", tone: "negative" },
  reconciling: { label: "Reconciling", tone: "caution" },
  rollback_available: { label: "Rollback Available", tone: "caution" },
  rolling_back: { label: "Rolling Back", tone: "caution" },
  rolled_back: { label: "Rolled Back", tone: "neutral" },
  cleanup_blocked: { label: "Cleanup Blocked", tone: "negative" },
  completed: { label: "Completed", tone: "positive" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  manual_recovery: { label: "Manual Recovery", tone: "negative" },
  failed: { label: "Failed", tone: "negative" },
};

const STATUS_META: Record<MigrationJobStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: "Pending", tone: "caution" },
  running: { label: "Running", tone: "caution" },
  waiting: { label: "Waiting", tone: "caution" },
  success: { label: "Succeeded", tone: "positive" },
  failure: { label: "Failed", tone: "negative" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  unknown: { label: "Outcome Unknown", tone: "negative" },
  manual_recovery: { label: "Manual Recovery", tone: "negative" },
};

const ATTENTION_PHASES = new Set<MigrationJobPhase>([
  "cutover_pending",
  "external_cutover_pending",
  "unknown",
  "reconciling",
  "rollback_available",
  "cleanup_due",
  "cleanup_blocked",
  "manual_recovery",
  "failed",
]);

const HISTORY_PHASES = new Set<MigrationJobPhase>([
  "completed",
  "rolled_back",
  "cancelled",
  "failed",
]);

export function migrationStatusMeta(job: Pick<MigrationJob, "phase" | "status">) {
  return PHASE_META[job.phase] ?? STATUS_META[job.status] ?? { label: formatLabel(job.phase), tone: "neutral" as const };
}

// important: GET /migrations?view= filters with these same predicates on the server
// (backend/app/services/migrations/repository.py); change both together.
export function migrationInView(job: Pick<MigrationJob, "phase" | "status">, view: MigrationView): boolean {
  if (view === "attention") {
    return ATTENTION_PHASES.has(job.phase) || ["failure", "unknown", "manual_recovery"].includes(job.status);
  }
  if (view === "history") {
    return HISTORY_PHASES.has(job.phase) || ["success", "failure", "cancelled"].includes(job.status);
  }
  return !HISTORY_PHASES.has(job.phase) && !["success", "failure", "cancelled"].includes(job.status);
}

/** The Migration Queue's job tabs, in order, with what each says when it is empty. */
export const JOB_VIEW_TABS: ReadonlyArray<{
  view: MigrationView;
  label: string;
  emptyTitle: string;
  emptyDescription: string;
}> = [
  {
    view: "active",
    label: "Active",
    emptyTitle: "No active migrations",
    emptyDescription: "Queued and in-progress jobs appear here.",
  },
  {
    view: "attention",
    label: "Needs Attention",
    emptyTitle: "No migration needs attention",
    emptyDescription: "Blocked, uncertain, failed, and overdue jobs appear here.",
  },
  {
    view: "history",
    label: "History",
    emptyTitle: "No migration history",
    emptyDescription: "Completed, cancelled, and failed jobs appear here.",
  },
];

export function findingMessage(finding: MigrationIssue): string {
  return finding.message;
}

export function findingTitle(finding: MigrationIssue): string | null {
  return finding.code ? formatLabel(finding.code) : null;
}

export function findingRemediation(finding: MigrationIssue): string | null {
  return finding.remediation ?? null;
}

const STRATEGY_LABEL: Record<string, string> = {
  cross_cluster_pull: "Transfer between clusters",
  member_relocation: "Relocate to another member",
  project_relocation: "Recreate the project and transfer its instances",
  revert_relocation: "Move back to the original member",
};

/**
 * The transfer method in operator words. `same_cluster_copy` is also how a move inside one
 * cluster stages its target, so for a move it must not read "copy": the source is retained,
 * not left running beside a copy.
 */
export function strategyLabel(item: { intent?: string | null; strategy?: string | null }): string {
  if (!item.strategy) return "Decided by preflight";
  if (item.strategy === "same_cluster_copy") {
    return item.intent === "move" ? "Transfer inside the cluster" : "Copy inside the cluster";
  }
  return STRATEGY_LABEL[item.strategy] ?? formatLabel(item.strategy);
}

/** What an operator would call the job: a project relocation is not a "move project". */
export function operationLabel(job: Pick<MigrationJob, "intent" | "scope" | "strategy">): string {
  if (job.scope === "project") {
    return job.strategy === "member_relocation" ? "Relocate Workloads" : "Relocate Project";
  }
  return `${job.intent === "copy" ? "Copy" : "Move"} Instance`;
}

export function endpointLabel(endpoint: MigrationEndpointRef): string {
  const resource = endpoint.resource_name ? `/${endpoint.resource_name}` : "";
  return `${endpoint.cluster_id}/${endpoint.project}${resource}`;
}

export function endpointPrimary(endpoint: MigrationEndpointRef): string {
  return endpoint.resource_name || endpoint.project;
}

export function jsonScalar(value: MigrationJsonValue | undefined): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

export function resourceName(resource: MigrationPlannedResource): string {
  return (
    jsonScalar(resource.source.name) ||
    jsonScalar(resource.target.name) ||
    resource.resource_key
  );
}

export function resourceLocation(value: Record<string, MigrationJsonValue>): string | null {
  const cluster = jsonScalar(value.cluster_id);
  const project = jsonScalar(value.project);
  const name = jsonScalar(value.name);
  const parts = [cluster, project, name].filter((part): part is string => !!part);
  return parts.length ? parts.join("/") : null;
}

export function resourceProgress(resource: MigrationJobResource): {
  percent: number | null;
  transferred: number | null;
  total: number | null;
} {
  const percent = resource.progress.percent;
  const transferred = resource.progress.bytes_transferred;
  const total = resource.progress.bytes_total;
  return {
    percent: typeof percent === "number" ? percent : null,
    transferred: typeof transferred === "number" ? transferred : null,
    total: typeof total === "number" ? total : null,
  };
}

export function jobProgress(job: Pick<MigrationJob, "resources">): number | null {
  const values = job.resources
    .map((resource) => resourceProgress(resource).percent)
    .filter((value): value is number => value !== null);
  if (!values.length) return null;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

/**
 * Value-free contract summaries keyed by display name: one per planned instance, and one per
 * profile a project relocation clones, since a profile NIC is mapped the same way.
 */
export function planContracts(
  resources: MigrationPlannedResource[],
): Array<{ name: string; contract: MigrationContractSummary }> {
  return resources.flatMap((resource) => {
    const contract = resource.target?.contract;
    const kind = resource.resource_type;
    if ((kind !== "instance" && kind !== "profile") || !contract || typeof contract !== "object" || Array.isArray(contract)) {
      return [];
    }
    const name = kind === "profile" ? `profile ${resourceName(resource)}` : resourceName(resource);
    return [{ name, contract: contract as unknown as MigrationContractSummary }];
  });
}

export function planHasRunningSource(resources: MigrationPlannedResource[]): boolean {
  return resources.some((resource) => resource.source_status === "running");
}

/** Phases in which a staged move keeps its stopped source for rollback. */
export const RETAINED_PHASES = new Set<MigrationJobPhase>(["source_retained", "cleanup_due", "cleanup_blocked"]);

/**
 * The retention deadline while the source is still retained. The server never clears the field,
 * so a rolled-back or finalized move would otherwise keep showing a deadline that no longer applies.
 */
export function activeRetentionDeadline(job: Pick<MigrationJob, "phase" | "retention_deadline">): string | null {
  return RETAINED_PHASES.has(job.phase) ? job.retention_deadline ?? null : null;
}
