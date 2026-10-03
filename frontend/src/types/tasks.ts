/**
 * Wire shapes of the task journal. Mirrors backend/app/models/task_schemas.py one to one;
 * change both or neither.
 */

export type TaskStatus = "pending" | "running" | "success" | "failure" | "cancelled" | "lost";
export type TaskKind = "lxd_operation" | "dashboard_action" | "upload" | "rebuild" | "schedule";
export type ActorKind = "user" | "api_key" | "system" | "external";
export type TaskSource = "dashboard" | "events" | "cli" | "mcp" | "integration" | "schedule";
export type LogKind = "audit" | "lifecycle" | "warning" | "task";
export type LogSeverity = "info" | "notice" | "warning" | "error";
export type FollowerState = "connected" | "disconnected" | "unregistered";

export interface Requestor {
  username: string | null;
  protocol: string | null;
  /** Admin only on the wire. */
  address: string | null;
}

export interface TaskRecord {
  id: string;
  seq: number;
  kind: TaskKind;
  cluster_id: string;
  project: string;
  resource_type: string | null;
  resource_name: string | null;
  operation_id: string | null;
  operation_class: string | null;
  action: string | null;
  description: string;
  status: TaskStatus;
  status_code: number | null;
  progress: string | null;
  error: string | null;
  may_cancel: boolean;
  actor_email: string | null;
  actor_role: string | null;
  actor_kind: ActorKind;
  api_key_id: string | null;
  requestor: Requestor | null;
  source: TaskSource;
  request_id: string | null;
  target_organization_id: number | null;
  target_organization_slug: string | null;
  location: string | null;
  metadata: Record<string, unknown>;
  lxd_created_at: string | null;
  lxd_updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  duration_ms: number | null;
  /** Pending rank within the caller's visible cluster queue; null outside the queue. */
  queue_position: number | null;
}

export interface TaskListResponse {
  tasks: TaskRecord[];
  total: number;
  running: number;
  failed_recent: number;
  limit: number;
  offset: number;
  has_more: boolean;
  disconnected_clusters: string[];
  follower_active: boolean;
}

export interface ClusterLogEntry {
  id: number;
  ts: string;
  kind: LogKind;
  severity: LogSeverity;
  cluster_id: string | null;
  project: string | null;
  resource_type: string | null;
  resource_name: string | null;
  action: string;
  message: string;
  actor_email: string | null;
  actor_role: string | null;
  actor_kind: ActorKind;
  api_key_id: string | null;
  result: string | null;
  request_id: string | null;
  event_id: string | null;
  task_id: string | null;
  target_organization_id: number | null;
  details: Record<string, unknown> | null;
}

export interface ClusterLogResponse {
  entries: ClusterLogEntry[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface TaskDetailResponse {
  task: TaskRecord;
  log: ClusterLogEntry[];
}

export interface DueSchedule {
  schedule_id: number;
  cluster_id: string;
  project: string;
  instance_name: string;
  label: string;
  next_run_at: string;
  run_state: string;
}

export interface QueueCluster {
  cluster_id: string;
  state: FollowerState;
  running: TaskRecord[];
  pending: TaskRecord[];
  due_schedules: DueSchedule[];
}

export interface TaskQueueResponse {
  clusters: QueueCluster[];
  generated_at: string;
  disconnected_clusters: string[];
  follower_active: boolean;
  total_open: number;
  limit: number;
  offset: number;
  has_more: boolean;
  due_schedules_total: number;
  due_schedules_truncated: boolean;
}

export interface CancelResponse {
  success: boolean;
  task_id: string;
  operation_id: string;
  message: string;
}

/** A 409 refusal from DELETE /tasks/{id}. */
export type CancelRefusalCode = "NO_OPERATION" | "WEBSOCKET_CLASS" | "ALREADY_FINISHED" | "NOT_CANCELLABLE";

export type TaskFeedFrame =
  | { type: "hello"; server_time: string; follower_active: boolean }
  | { type: "task"; task: TaskRecord }
  | { type: "log"; entry: ClusterLogEntry }
  | { type: "follower"; active: boolean }
  | { type: "resync"; reason: string };
