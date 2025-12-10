/**
 * TypeScript type definitions for Orcastra Dashboard API
 */

// Enums
export type VMStatus = "Running" | "Stopped" | "Frozen" | "Error" | "Unknown";

// Resource Usage
export interface ResourceUsage {
  cpu_usage: number | null;
  memory_usage: number | null;
  memory_limit: number | null;
  disk_usage: Record<string, number> | null;
  network_counters: Record<
    string,
    {
      bytes_received: number;
      bytes_sent: number;
      packets_received: number;
      packets_sent: number;
    }
  > | null;
}

// Instance Metrics
export interface InstanceMetrics {
  instance_name: string;
  host: string;
  status: VMStatus;
  cpu_usage_percent: number;
  memory_used_bytes: number;
  memory_total_bytes: number;
  memory_usage_percent: number;
  disk_usage_bytes: Record<string, number>;
  network_rx_bytes: Record<string, number>;
  network_tx_bytes: Record<string, number>;
  processes: number;
  uptime_seconds: number;
  base_image: string | null;
  ipv4_addresses: string[];
  ipv6_addresses: string[];
  pid: number | null;
  timestamp: string;
}

// VM Information
export interface VMInfo {
  name: string;
  status: VMStatus;
  status_code: number;
  ip_addresses: string[];
  host: string;
  host_endpoint: string;
  project: string | null;
  architecture: string | null;
  created_at: string | null;
  last_used_at: string | null;
  description: string | null;
  type: string;
  resources: ResourceUsage | null;
  config: Record<string, string> | null;
  profiles: string[];
  tags: string[];
}

// VM List Response
export interface VMListResponse {
  total: number;
  vms: VMInfo[];
  nodes: string[];
}

// VM Create Request
export interface VMCreateRequest {
  name: string;
  host: string;
  image: string;
  instance_type?: "virtual-machine" | "container";
  profiles?: string[];
  config?: Record<string, string>;
  cloud_init?: string;
  devices?: Record<string, Record<string, string>>;
  start_on_create?: boolean;
}

// VM Create Response
export interface VMCreateResponse {
  success: boolean;
  message: string;
  vm: VMInfo | null;
  operation_id: string | null;
}

// VM Action Request
export interface VMActionRequest {
  host: string;
  force?: boolean;
  timeout?: number;
}

// VM Action Response
export interface VMActionResponse {
  success: boolean;
  message: string;
  vm_name: string;
  action: string;
  new_status: VMStatus | null;
}

// Image Information
export interface ImageInfo {
  fingerprint: string;
  aliases: string[];
  description: string;
  architecture: string;
  size: number;
  upload_date: string | null;
  public: boolean;
  type: string;
}

// Image List Response
export interface ImageListResponse {
  host: string;
  images: ImageInfo[];
  total: number;
}

// Node Resources
export interface NodeResources {
  cpu_total: number;
  cpu_used: number;
  cpu_available: number;
  cpu_sockets: number;
  memory_total: number;
  memory_used: number;
  memory_available: number;
  gpu_total: number;
  gpu_used: number;
  gpu_available: number;
  storage_total: number;
  storage_available: number;
  storage_pools: Array<{
    name: string;
    driver: string;
    space_total: number;
    space_used: number;
    space_available: number;
  }>;
}

// Node Status
export interface NodeStatus {
  name: string;
  endpoint: string;
  connected: boolean;
  node_type: string | null;
  server_version: string | null;
  api_version: string | null;
  auth: string | null;
  environment: Record<string, unknown> | null;
  error: string | null;
  resources: NodeResources | null;
  tags: string[];
  display_name?: string | null;
}

// Nodes Status Response
export interface NodesStatusResponse {
  nodes: NodeStatus[];
  all_connected: boolean;
}

// Project Information
export interface ProjectInfo {
  name: string;
  description: string | null;
  config: Record<string, string> | null;
  used_by: string[];
  host: string;
  tags: string[];
}

// Project List Response
export interface ProjectListResponse {
  host: string;
  projects: ProjectInfo[];
  total: number;
}

// Project Create Request
export interface ProjectCreateRequest {
  name: string;
  host: string;
  description?: string;
  config?: Record<string, string>;
  max_cpu?: number;
  max_ram?: string;
  max_storage?: string;
}

// Project Create Response
export interface ProjectCreateResponse {
  success: boolean;
  message: string;
  project: ProjectInfo | null;
}

// Error Response
export interface ErrorResponse {
  error: string;
  detail: string | null;
  code: string | null;
}

// API Error
export class APIError extends Error {
  constructor(
    message: string,
    public status: number,
    public detail?: string | null
  ) {
    super(message);
    this.name = "APIError";
  }
}

// ========== LXD Features Types ==========

// Profile Types
export interface ProfileInfo {
  name: string;
  description: string;
  config: Record<string, string>;
  devices: Record<string, Record<string, string>>;
  used_by: string[];
  node: string;
}

export interface ProfileListResponse {
  total: number;
  profiles: ProfileInfo[];
  nodes: string[];
}

export interface ProfileCreateRequest {
  name: string;
  description?: string;
  config?: Record<string, string>;
  devices?: Record<string, Record<string, string>>;
}

// Network Types
export interface NetworkInfo {
  name: string;
  description: string;
  type: string;
  managed: boolean;
  config: Record<string, string>;
  used_by: string[];
  status: string;
}

export interface NetworkListResponse {
  total: number;
  networks: NetworkInfo[];
  nodes: string[];
}

export interface NetworkCreateRequest {
  name: string;
  description?: string;
  type: string;
  config?: Record<string, string>;
}

// Network ACL Types
export interface NetworkACLInfo {
  name: string;
  description: string;
  ingress: Array<Record<string, any>>;
  egress: Array<Record<string, any>>;
  config: Record<string, string>;
  used_by: string[];
}

export interface NetworkACLListResponse {
  total: number;
  acls: NetworkACLInfo[];
  nodes: string[];
}

export interface NetworkACLCreateRequest {
  name: string;
  description?: string;
  ingress?: Array<Record<string, any>>;
  egress?: Array<Record<string, any>>;
  config?: Record<string, string>;
}

// Storage Pool Types
export interface StoragePoolInfo {
  name: string;
  description: string;
  driver: string;
  config: Record<string, string>;
  used_by: string[];
  status: string;
}

export interface StoragePoolListResponse {
  total: number;
  pools: StoragePoolInfo[];
  nodes: string[];
}

export interface StoragePoolCreateRequest {
  name: string;
  description?: string;
  driver: string;
  config?: Record<string, string>;
}

// Storage Volume Types
export interface StorageVolumeInfo {
  name: string;
  type: string;
  pool: string;
  description: string;
  config: Record<string, string>;
  used_by: string[];
  content_type: string;
}

export interface StorageVolumeListResponse {
  total: number;
  volumes: StorageVolumeInfo[];
  nodes: string[];
}

export interface StorageVolumeCreateRequest {
  name: string;
  pool: string;
  type?: string;
  description?: string;
  config?: Record<string, string>;
  content_type?: string;
}

// Operation Types
export interface OperationInfo {
  id: string;
  class_name: string;
  created_at: string;
  updated_at: string;
  status: string;
  status_code: number;
  description: string;
  err: string;
  may_cancel: boolean;
  metadata: Record<string, any>;
}

export interface OperationListResponse {
  total: number;
  operations: OperationInfo[];
  nodes: string[];
}

// Warning Types
export interface WarningInfo {
  uuid: string;
  location: string;
  project: string;
  type: string;
  count: number;
  first_seen_at: string;
  last_seen_at: string;
  last_message: string;
  severity: string;
  status: string;
}

export interface WarningListResponse {
  total: number;
  warnings: WarningInfo[];
  nodes: string[];
}

// Server Types
export interface ServerInfo {
  node: string;
  api_version: string;
  auth: string;
  config: Record<string, any>;
  environment: Record<string, string>;
  public: boolean;
}

export interface ServerListResponse {
  servers: ServerInfo[];
  total: number;
}

// Identity Types
export interface IdentityInfo {
  id: string;
  name: string;
  type: string;
  groups: string[];
}

export interface IdentityListResponse {
  total: number;
  identities: IdentityInfo[];
  nodes: string[];
}

// Group Types
export interface GroupInfo {
  name: string;
  description: string;
  identities: string[];
  permissions: Array<Record<string, any>>;
}

export interface GroupListResponse {
  total: number;
  groups: GroupInfo[];
  nodes: string[];
}
