import type { BadgeTone } from "@/components/ui/badge";
import { inventoryLinks } from "@/lib/inventory-links";
import { formatLabel, toMillis } from "@/lib/utils";
import type { ClusterLogEntry, LogSeverity, TaskRecord, TaskStatus } from "@/types/tasks";

// Display rules for a task row, shared by the dock, the detail dialog and the Operations page,
// so the three surfaces cannot disagree on a tone, a duration or an actor's name.

export const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set([
  "success",
  "failure",
  "cancelled",
  "lost",
]);

/** The facet order for the Status column: what needs attention first. */
export const STATUS_ORDER: readonly TaskStatus[] = [
  "running",
  "pending",
  "failure",
  "cancelled",
  "lost",
  "success",
];

export const SEVERITY_ORDER: readonly LogSeverity[] = ["error", "warning", "notice", "info"];

export interface TaskStatusDisplay {
  label: string;
  tone: BadgeTone;
}

const TASK_STATUS_DISPLAY: Record<TaskStatus, TaskStatusDisplay> = {
  pending: { label: "Pending", tone: "neutral" },
  running: { label: "Running", tone: "caution" },
  success: { label: "Succeeded", tone: "positive" },
  failure: { label: "Failed", tone: "negative" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  lost: { label: "Lost", tone: "neutral" },
};

/** Operator-facing metadata. The normalized wire value remains unchanged everywhere else. */
export function taskStatusDisplay(status: string): TaskStatusDisplay {
  const normalized = status.trim().toLowerCase() as TaskStatus;
  return TASK_STATUS_DISPLAY[normalized] ?? { label: formatLabel(status), tone: "neutral" };
}

export function taskStatusLabel(status: string): string {
  return taskStatusDisplay(status).label;
}

export function taskStatusTone(status: TaskStatus): BadgeTone {
  return taskStatusDisplay(status).tone;
}

export interface LogSeverityDisplay {
  label: string;
  tone: BadgeTone | null;
}

const LOG_SEVERITY_DISPLAY: Record<LogSeverity, LogSeverityDisplay> = {
  error: { label: "Error", tone: "negative" },
  warning: { label: "Warning", tone: "caution" },
  notice: { label: "Notice", tone: null },
  info: { label: "Info", tone: null },
};

export function logSeverityDisplay(severity: string): LogSeverityDisplay {
  const normalized = severity.trim().toLowerCase() as LogSeverity;
  return LOG_SEVERITY_DISPLAY[normalized] ?? { label: formatLabel(severity), tone: null };
}

export function logSeverityLabel(severity: string): string {
  return logSeverityDisplay(severity).label;
}

/** A chip only for the two severities that need attention. Everything else is plain text. */
export function severityTone(severity: LogSeverity): BadgeTone | null {
  return logSeverityDisplay(severity).tone;
}

export function isTerminal(status: TaskStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

export function isWebsocketClass(task: Pick<TaskRecord, "operation_class">): boolean {
  return task.operation_class === "websocket";
}

/**
 * LXD's own background work ("Updating images", "Cleaning up expired backups"): listed by the
 * tabs, never counted on the dock. Mirrors `_not_housekeeping` in
 * backend/app/services/task_journal/reads.py, so a live frame moves the counts the way the next
 * summary read will.
 */
export function isLxdHousekeeping(
  task: Pick<TaskRecord, "kind" | "operation_class" | "actor_kind">,
): boolean {
  return task.kind === "lxd_operation" && task.operation_class === "task" && task.actor_kind === "system";
}

/** When the task started, falling back to when the journal first saw it. */
export function startedMs(task: Pick<TaskRecord, "started_at" | "created_at">): number | null {
  return toMillis(task.started_at) ?? toMillis(task.created_at);
}

/**
 * Milliseconds the task has run. Live against `serverNow` while running, fixed once finished,
 * null while pending because nothing has started.
 */
export function durationMs(
  task: Pick<TaskRecord, "status" | "started_at" | "created_at" | "finished_at" | "duration_ms">,
  serverNow?: number,
): number | null {
  if (task.status === "pending") return null;
  if (task.duration_ms !== null && task.duration_ms !== undefined && isTerminal(task.status)) {
    return Number.isFinite(task.duration_ms) && task.duration_ms >= 0 ? task.duration_ms : null;
  }
  const started = startedMs(task);
  if (started === null) return null;
  const finished = toMillis(task.finished_at);
  if (finished !== null) return Math.max(0, finished - started);
  if (task.status !== "running") return null;
  const now = serverNow ?? Date.now();
  return Number.isFinite(now) ? Math.max(0, now - started) : null;
}

// The internal journal kind is shared by compatible hypervisors.
const KIND_LABELS: Record<TaskRecord["kind"], string> = {
  lxd_operation: "Hypervisor Operation",
  dashboard_action: "Dashboard Action",
  upload: "Upload",
  rebuild: "Rebuild",
  schedule: "Schedule",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind as TaskRecord["kind"]] ?? formatLabel(kind);
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

export function actorLabel(
  task: Pick<TaskRecord, "actor_kind" | "actor_email" | "api_key_id" | "requestor">,
): string {
  switch (task.actor_kind) {
    case "user":
      return task.actor_email ?? "Unknown user";
    case "api_key":
      return task.api_key_id ? `API key ${task.api_key_id}` : "API key";
    case "system":
      return "System";
    default:
      return task.requestor?.username ?? "Not attributed";
  }
}

/** The log row's actor. A log row carries no requestor, so an external actor is just that. */
export function logActorLabel(
  entry: Pick<ClusterLogEntry, "actor_kind" | "actor_email" | "api_key_id">,
): string {
  switch (entry.actor_kind) {
    case "user":
      return entry.actor_email ?? "Unknown user";
    case "api_key":
      return entry.api_key_id ? `API key ${entry.api_key_id}` : "API key";
    case "system":
      return "System";
    default:
      return "External";
  }
}

export function actorSubText(
  task: Pick<TaskRecord, "actor_kind" | "actor_role" | "source" | "requestor">,
): string {
  switch (task.actor_kind) {
    case "user":
      return task.actor_role ? `${formatLabel(task.actor_role)} · ${task.source}` : task.source;
    case "external":
      return task.requestor?.protocol ?? "cli";
    default:
      return task.source;
  }
}

/** The filtered list the task's resource lives in, or null when there is no such list. */
export function resourceHref(
  task: Pick<TaskRecord, "resource_type" | "resource_name" | "cluster_id" | "project">,
): string | null {
  const name = task.resource_name;
  if (!name) return null;
  const node = task.cluster_id;
  const project = task.project;
  switch (task.resource_type) {
    case "instance":
      return inventoryLinks.instances({ q: name, host: node, project });
    case "image":
      // An image lives in a store, which is the default project's for every project without
      // features.images; the task's project would filter the fingerprint out of sight.
      return inventoryLinks.images({ q: name, node });
    case "profile":
      return inventoryLinks.profiles({ q: name, node, project });
    case "storage_pool":
      return inventoryLinks.storagePools({ q: name, node });
    case "storage_volume":
      return inventoryLinks.storageVolumes({ q: name, node });
    case "network":
      return inventoryLinks.networkDetail(name, node, project);
    case "network_acl":
      return inventoryLinks.networkAcls({ q: name, node });
    case "migration":
      return inventoryLinks.migrations({ job: name });
    default:
      return null;
  }
}

/** The first `NN%` in a progress string, clamped to 0..100. Mirrors vocabulary.progress_percent. */
export function progressPercent(progress: string | null | undefined): number | null {
  if (!progress) return null;
  const match = /(\d{1,3})%/.exec(progress);
  if (!match) return null;
  return Math.max(0, Math.min(100, Number(match[1])));
}

/** "Instance web-01 on cluster-a", or "On cluster-a" when the task names no resource. */
export function taskSubtitle(
  task: Pick<TaskRecord, "resource_type" | "resource_name" | "cluster_id">,
): string {
  const type = task.resource_type ? formatLabel(task.resource_type) : null;
  if (type && task.resource_name) return `${type} ${task.resource_name} on ${task.cluster_id}`;
  return `On ${task.cluster_id}`;
}

/** Why the cancel action is unavailable, or null when it can run. */
export function cancelUnavailableReason(
  task: Pick<TaskRecord, "status" | "may_cancel" | "operation_class">,
  readOnly: boolean,
): string | null {
  if (readOnly) return "Read-only mode is on";
  if (isTerminal(task.status)) return "Already finished";
  if (!task.may_cancel || isWebsocketClass(task)) return "Cannot be cancelled";
  return null;
}
