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
  lxd_count: number;
  microcloud_count: number;
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
  node: string;
}

export interface IdentityListResponse {
  total: number;
  identities: IdentityInfo[];
  nodes: string[];
}

// Group Types
export interface GroupIdentities {
  tls: string[];
  oidc: string[];
}

export interface GroupInfo {
  name: string;
  description: string;
  identities: GroupIdentities;
  permissions: Array<Record<string, any>>;
  node: string;
}

export interface GroupListResponse {
  total: number;
  groups: GroupInfo[];
  nodes: string[];
}

export interface GroupCreateRequest {
  name: string;
  description?: string;
  permissions?: Array<{
    entity_type: string;
    url: string;
    entitlement: string;
  }>;
}

export interface GroupUpdateRequest {
  description?: string;
  permissions?: Array<{
    entity_type: string;
    url: string;
    entitlement: string;
  }>;
}

export interface IdentityAddRequest {
  auth_type: "tls" | "oidc";
  identity_id: string;
}

// ========== Cluster Registration Types ==========

export enum ClusterStatus {
  PENDING = "pending",
  NOT_AUTHORIZED = "not_authorized",
  ONLINE = "online",
  OFFLINE = "offline",
  ERROR = "error"
}

export interface ClusterRegisterRequest {
  cluster_id: string;
  display_name: string;
  endpoint: string;
  zone?: string;
  merchant_id?: string;
}

// ========== Cluster Members Types ==========

export interface ClusterMember {
  server_name: string;
  url: string;
  database: boolean;
  status: string;
  message: string;
  roles: string[];
  architecture: string | null;
  failure_domain: string | null;
  description: string | null;
  groups: string[];
}

export interface ClusterMembersResponse {
  node: string;
  is_clustered: boolean;
  cluster_enabled: boolean;
  members: ClusterMember[];
  total: number;
}

export interface MemberResources {
  member_name: string;
  cpu_total: number;
  cpu_used: number;
  cpu_available: number;
  cpu_usage_percent: number;
  memory_total: number;
  memory_used: number;
  memory_available: number;
  memory_usage_percent: number;
  gpu_total: number;
  gpu_used: number;
  gpu_available: number;
  storage_total: number;
  storage_used: number;
  storage_available: number;
  storage_usage_percent: number;
  storage_pools: Array<{
    name: string;
    driver: string;
    space_total: number;
    space_used: number;
    space_available: number;
  }>;
  instances_count: number;
  instances_running: number;
}

export interface ClusterInfo {
  cluster_id: string;
  display_name: string;
  endpoint: string;
  zone?: string;
  cluster_type: string;
  merchant_id?: string;
  status: ClusterStatus;
  has_certificate: boolean;
  certificate_common_name?: string;
  labels: Record<string, string>;
  created_at?: string;
  last_checked?: string;
  error_message?: string;
}

export interface ClusterRegisterResponse {
  success: boolean;
  message: string;
  cluster_id: string;
  status: ClusterStatus;
  certificate_generated: boolean;
  trust_added: boolean;
  certificate?: string;
}

export interface ClusterAuthCheckRequest {
  cluster_id: string;
}

export interface ClusterAuthCheckResponse {
  cluster_id: string;
  connected: boolean;
  authenticated: boolean;
  status: ClusterStatus;
  message: string;
  server_info?: Record<string, any>;
}

export interface ClusterListResponse {
  clusters: ClusterInfo[];
  total: number;
}

export interface ClusterDeleteResponse {
  success: boolean;
  message: string;
  cluster_id: string;
}

// Version Information
export interface VersionInfo {
  app_version: string;
  build_date?: string | null;
  build_commit?: string | null;
  environment: string;
}

export interface SystemSettings {
  read_only_mode: boolean;
  debug: boolean;
}

// My Keys
export interface KeyInfo {
  id: string;
  name: string;
  node: string;
  project: string;
  auth_level: string;
  created_at: string;
  expires_at?: string | null;
  is_expired: boolean;
  identity_id: string;
  group_name: string;
  fingerprint: string;
}

export interface KeyCreateRequest {
  name: string;
  node: string;
  project: string;
  auth_level: string;
  expire_days: number;
}

export interface KeyListResponse {
  total: number;
  active: number;
  expired: number;
  keys: KeyInfo[];
}

// ========== Monitoring Types ==========

export interface CPUMetrics {
  usage_percent: number;
  total_cores: number;
  used_cores: number;
  load_average: number[];
}

export interface MemoryMetrics {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  cached_bytes: number;
  usage_percent: number;
}

export interface DiskMetrics {
  pool_name: string;
  driver: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  usage_percent: number;
}

export interface NetworkMetrics {
  interface: string;
  rx_bytes: number;
  tx_bytes: number;
  rx_packets: number;
  tx_packets: number;
  rx_errors: number;
  tx_errors: number;
  rx_dropped: number;
  tx_dropped: number;
}

export interface InstanceStats {
  total: number;
  running: number;
  stopped: number;
  frozen: number;
  error: number;
  containers: number;
  virtual_machines: number;
}

export interface NodeMetrics {
  node_name: string;
  display_name?: string | null;
  endpoint: string;
  connected: boolean;
  node_type?: string | null;
  cpu: CPUMetrics;
  memory: MemoryMetrics;
  storage: DiskMetrics[];
  network: NetworkMetrics[];
  instances: InstanceStats;
  uptime_seconds: number;
  server_version?: string | null;
  timestamp: string;
}

export interface ClusterMetrics {
  cluster_id: string;
  total_nodes: number;
  online_nodes: number;
  offline_nodes: number;
  cpu: CPUMetrics;
  memory: MemoryMetrics;
  storage: DiskMetrics;
  instances: InstanceStats;
  nodes: NodeMetrics[];
  timestamp: string;
}

export interface SystemOverview {
  total_clusters: number;
  online_clusters: number;
  total_nodes: number;
  online_nodes: number;
  total_instances: number;
  running_instances: number;
  stopped_instances: number;
  total_containers: number;
  total_vms: number;
  cpu_allocation_percent: number;
  memory_allocation_percent: number;
  storage_allocation_percent: number;
  total_cpu_cores: number;
  used_cpu_cores: number;
  total_memory_bytes: number;
  used_memory_bytes: number;
  total_storage_bytes: number;
  used_storage_bytes: number;
  clusters: ClusterMetrics[];
  timestamp: string;
}

export interface InstanceMetricsDetail {
  name: string;
  host: string;
  project: string;
  type: string;
  status: string;
  cpu_usage_percent: number;
  cpu_seconds_total: number;
  memory_usage_bytes: number;
  memory_limit_bytes: number;
  memory_usage_percent: number;
  disk_read_bytes: number;
  disk_write_bytes: number;
  network_rx_bytes: number;
  network_tx_bytes: number;
  processes: number;
  uptime_seconds: number;
  timestamp: string;
}

// ========== Monitoring Dashboard Types ==========

export interface ClusterMonitoringMetrics {
  node_name: string;
  node_type: string;
  is_online: boolean;
  cpu_total: number;
  cpu_used: number;
  cpu_usage_percent: number;
  memory_total_bytes: number;
  memory_used_bytes: number;
  memory_usage_percent: number;
  storage_total_bytes: number;
  storage_used_bytes: number;
  storage_usage_percent: number;
  gpu_total: number;
  gpu_used: number;
  instance_count: number;
  running_instances: number;
}

export interface InstanceMonitoringSummary {
  name: string;
  host: string;
  type: string;
  status: string;
  cpu_usage: number;
  memory_usage_bytes: number;
  memory_limit_bytes: number;
  disk_usage_bytes: number;
  network_rx_bytes: number;
  network_tx_bytes: number;
}

export interface StoragePoolMetrics {
  name: string;
  node: string;
  driver: string;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  usage_percent: number;
}

export interface MonitoringNetworkMetrics {
  total_rx_bytes: number;
  total_tx_bytes: number;
  total_rx_packets: number;
  total_tx_packets: number;
  interfaces: Record<string, { rx_bytes: number; tx_bytes: number; rx_packets: number; tx_packets: number }>;
  timestamp: string;
}

export interface WarningsSummary {
  total: number;
  critical: number;
  warning: number;
  info: number;
}

export interface NodeMetricsPoint {
  cpu_usage_percent: number;
  memory_usage_percent: number;
  storage_usage_percent: number;
  network_rx_bytes: number;
  network_tx_bytes: number;
  running_instances: number;
}

export interface MetricsHistoryPoint {
  timestamp: string;
  cpu_usage_percent: number;
  memory_usage_percent: number;
  storage_usage_percent?: number;
  network_rx_bytes: number;
  network_tx_bytes: number;
  disk_io_bytes: number;
  running_instances: number;
  nodes?: Record<string, NodeMetricsPoint>;
}

export interface MonitoringDashboardResponse {
  timestamp: string;
  
  // Cluster Overview
  total_nodes: number;
  online_nodes: number;
  offline_nodes: number;
  
  // Instance Overview
  total_instances: number;
  running_instances: number;
  stopped_instances: number;
  other_instances: number;
  container_count: number;
  vm_count: number;
  
  // Aggregated Resources (Allocation)
  total_cpu: number;
  used_cpu: number;
  cpu_usage_percent: number;
  
  // Real-time CPU usage from running instances
  realtime_cpu_percent: number;
  
  total_memory_bytes: number;
  used_memory_bytes: number;
  memory_usage_percent: number;
  
  // Real-time memory usage from running instances
  realtime_memory_bytes: number;
  realtime_memory_percent: number;
  
  total_storage_bytes: number;
  used_storage_bytes: number;
  storage_usage_percent: number;
  
  total_gpu: number;
  used_gpu: number;
  
  // Detailed Metrics
  nodes: ClusterMonitoringMetrics[];
  storage_pools: StoragePoolMetrics[];
  top_cpu_consumers: InstanceMonitoringSummary[];
  top_memory_consumers: InstanceMonitoringSummary[];
  
  // Warnings
  warnings: WarningsSummary | null;
  
  // Meta
  collection_time_ms: number;
}

// ========================
// System Resource Usage Types
// ========================

export interface CPUInfo {
  percent: number;
  count_logical: number;
  count_physical: number;
  frequency_current: number | null;
  frequency_max: number | null;
  load_avg_1m: number | null;
  load_avg_5m: number | null;
  load_avg_15m: number | null;
}

export interface MemoryInfo {
  total: number;
  available: number;
  used: number;
  percent: number;
  swap_total: number;
  swap_used: number;
  swap_percent: number;
}

export interface DiskInfo {
  device: string;
  mountpoint: string;
  fstype: string;
  total: number;
  used: number;
  free: number;
  percent: number;
}

export interface NetworkInterfaceInfo {
  name: string;
  bytes_sent: number;
  bytes_recv: number;
  packets_sent: number;
  packets_recv: number;
  is_up: boolean;
  speed: number | null;
  addresses: string[];
}

export interface ProcessInfo {
  pid: number;
  cpu_percent: number;
  memory_percent: number;
  memory_rss: number;
  memory_vms: number;
  threads: number;
  open_files: number;
  connections: number;
  create_time: string;
  uptime_seconds: number;
  io_read_bytes: number | null;
  io_write_bytes: number | null;
}

export interface PythonInfo {
  version: string;
  implementation: string;
  compiler: string;
}

export interface OSInfo {
  system: string;
  release: string;
  version: string;
  machine: string;
  hostname: string;
  boot_time: string;
  uptime_seconds: number;
}

export interface SystemResourceUsage {
  timestamp: string;
  cpu: CPUInfo;
  memory: MemoryInfo;
  disks: DiskInfo[];
  network: NetworkInterfaceInfo[];
  process: ProcessInfo | null;
  python: PythonInfo;
  os: OSInfo;
}
