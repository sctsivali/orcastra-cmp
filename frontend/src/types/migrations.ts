export type MigrationScope = "instance" | "project";
export type MigrationIntent = "copy" | "move";

export type MigrationPlanStatus =
  | "draft"
  | "preflighting"
  | "ready"
  | "blocked"
  | "expired"
  | "executed";

export type MigrationJobStatus =
  | "pending"
  | "running"
  | "waiting"
  | "success"
  | "failure"
  | "cancelled"
  | "unknown"
  | "manual_recovery";

export type MigrationJobPhase =
  | "queued"
  | "preparing_target"
  | "preparing_recovery"
  | "checkpointing"
  | "initial_sync"
  | "cutover_pending"
  | "quiescing"
  | "final_sync"
  | "target_validating"
  | "external_cutover_pending"
  | "cutting_over"
  | "verifying"
  | "source_retained"
  | "cleanup_due"
  | "cleaning_up"
  | "unknown"
  | "reconciling"
  | "rollback_available"
  | "rolling_back"
  | "rolled_back"
  | "cleanup_blocked"
  | "completed"
  | "cancelled"
  | "manual_recovery"
  | "failed";

export type MigrationResourceState =
  | "pending"
  | "blocked"
  | "preparing"
  | "syncing"
  | "ready"
  | "cutting_over"
  | "retained"
  | "cleaning"
  | "completed"
  | "failed"
  | "unknown"
  | "skipped";

export type MigrationAction =
  | "cancel"
  | "cutover"
  | "confirm_external_cutover"
  | "rollback"
  | "restore_checkpoint"
  | "cleanup"
  | "recheck"
  | "release";

export type MigrationDowntimeClass = "none" | "brief" | "cold" | "unknown";
export type MigrationJsonValue = string | number | boolean | null | MigrationJsonValue[] | { [key: string]: MigrationJsonValue };

export interface MigrationEndpointRef {
  cluster_id: string;
  project: string;
  resource_name?: string | null;
  member?: string | null;
  cluster_fingerprint?: string | null;
}

export interface MigrationMappings {
  storage_pools: Record<string, string>;
  networks: Record<string, string>;
  profiles: Record<string, string>;
  volumes: Record<string, string>;
  members: Record<string, string>;
  external_dependencies: Record<string, string>;
}

export type MigrationMappingCategory = keyof MigrationMappings;

export interface MigrationIssue {
  code: string;
  severity: "blocker" | "warning" | "info";
  message: string;
  remediation?: string | null;
  resource_key?: string | null;
}

export interface MigrationCapability {
  code: string;
  supported: boolean;
  reason?: string | null;
}

export interface MigrationPlannedResource {
  resource_key: string;
  resource_type: string;
  /** Live source status when the plan was checked. A running source is stopped for the transfer. */
  source_status?: string | null;
  source: Record<string, MigrationJsonValue>;
  target: Record<string, MigrationJsonValue>;
  dependency_keys: string[];
}

/** How one NIC of the source is placed on the target, as decided by preflight. */
export interface MigrationNicDecision {
  device: string;
  source: string;
  target: string | null;
  via: "mapping" | "target_profile" | "same_name" | "unmapped" | "unattached";
}

/** Value-free summary of the definition the target is created with. */
export interface MigrationContractSummary {
  contract_hash: string;
  profiles: string[];
  root_pool: string | null;
  nic_decisions: MigrationNicDecision[];
  pinned_config: string[];
  pinned_devices: string[];
  profile_added_config: string[];
}

export interface MigrationJobResource extends MigrationPlannedResource {
  resource_id: string;
  ordinal: number;
  state: MigrationResourceState;
  operation_refs: Array<Record<string, MigrationJsonValue>>;
  progress: Record<string, MigrationJsonValue>;
  error_code?: string | null;
  error_message?: string | null;
  updated_at: string;
  finished_at?: string | null;
}

export interface MigrationTimelineEntry {
  sequence: number;
  phase: MigrationJobPhase;
  message: string;
  created_at: string;
}

export interface MigrationPlanCreateRequest {
  scope: MigrationScope;
  intent: MigrationIntent;
  source: MigrationEndpointRef;
  target: MigrationEndpointRef;
  retention_hours: number;
  checkpoint_requested: boolean;
  external_cutover_required: boolean;
  mappings: MigrationMappings;
}

export interface MigrationPlanUpdateRequest {
  target?: MigrationEndpointRef;
  retention_hours?: number;
  checkpoint_requested?: boolean;
  external_cutover_required?: boolean;
  mappings?: MigrationMappings;
}

export interface MigrationPlan {
  plan_id: string;
  revision: number;
  scope: MigrationScope;
  intent: MigrationIntent;
  status: MigrationPlanStatus;
  source: MigrationEndpointRef;
  target: MigrationEndpointRef;
  strategy?: string | null;
  downtime_class: MigrationDowntimeClass;
  retention_hours: number;
  checkpoint_requested: boolean;
  external_cutover_required: boolean;
  capabilities: MigrationCapability[];
  resources: MigrationPlannedResource[];
  mappings: MigrationMappings;
  blockers: MigrationIssue[];
  warnings: MigrationIssue[];
  rollback_contract: Record<string, MigrationJsonValue>;
  expires_at: string;
  created_at: string;
  updated_at: string;
}

export interface MigrationExecuteRequest {
  reason: string;
  acknowledgements: string[];
}

export interface MigrationActionRequest {
  expected_revision: number;
  reason: string;
  confirmation?: string;
}

export interface MigrationJob {
  job_id: string;
  plan_id: string;
  plan_revision: number;
  revision: number;
  scope: MigrationScope;
  intent: MigrationIntent;
  source: MigrationEndpointRef;
  target: MigrationEndpointRef;
  strategy: string;
  downtime_class: MigrationDowntimeClass;
  phase: MigrationJobPhase;
  status: MigrationJobStatus;
  retention_hours: number;
  retention_deadline?: string | null;
  external_cutover_required: boolean;
  external_cutover_confirmed_at?: string | null;
  resources: MigrationJobResource[];
  timeline: MigrationTimelineEntry[];
  available_actions: MigrationAction[];
  safe_result?: Record<string, MigrationJsonValue> | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface MigrationJobListResponse {
  items: MigrationJob[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface MigrationPlanListResponse {
  items: MigrationPlan[];
  total: number;
  limit: number;
  offset: number;
  has_more?: boolean;
}

export function emptyMigrationMappings(): MigrationMappings {
  return {
    storage_pools: {},
    networks: {},
    profiles: {},
    volumes: {},
    members: {},
    external_dependencies: {},
  };
}
