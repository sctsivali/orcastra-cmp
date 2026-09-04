/**
 * TypeScript type definitions for the Orcastra CMP API
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
  /** Cluster member the instance runs on, when the target is clustered. */
  location?: string | null;
  project: string | null;
  architecture: string | null;
  created_at: string | null;
  last_used_at: string | null;
  description: string | null;
  type: string;
  resources: ResourceUsage | null;
  config: Record<string, string> | null;
  // Devices set on the instance itself. A root disk sized at create time lands here.
  // Optional because a client may be talking to a backend older than 1.0.0-RC4, which did not
  // return either field.
  devices?: Record<string, Record<string, string>> | null;
  // Devices after profile expansion: what LXD actually applies. Read the effective root disk
  // from here, since an instance that overrides nothing still has one.
  expanded_devices?: Record<string, Record<string, string>> | null;
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
  incus_count: number;
  // Pagination metadata
  offset: number;
  limit: number;
  has_more: boolean;
}

// VM Create Request
export interface VMCreateRequest {
  name: string;
  description?: string;
  host: string;
  image: string;
  image_server?: string;
  image_protocol?: string;
  project?: string;
  instance_type?: "virtual-machine" | "container";
  profiles?: string[];
  config?: Record<string, string>;
  cloud_init?: string;
  devices?: Record<string, Record<string, string>>;
  start_on_create?: boolean;
}

// Body for the dashboard's POST /instances. Mirrors the backend's
// InstanceCreateRequest: same as VMCreateRequest but with a blank-VM source, so
// `image` is omitted when source_type is "none" (an empty root disk, no OS).
// VMCreateRequest stays as-is for Orcahub's POST /vms, where image is required.
export interface InstanceCreateRequest extends Omit<VMCreateRequest, "image"> {
  image?: string;
  source_type?: "image" | "none";
}

export interface GpuCardListResponse {
  host: string;
  /** Every detected card, display controllers included. */
  gpus: GPUCardInfo[];
  /** Cards detected. */
  total: number;
  /** Cards that count as compute capacity. */
  compute_total?: number;
}

// A browsable image from a remote (simplestreams) catalog. One row per
// distribution/release/variant/architecture/type.
export interface RemoteImageInfo {
  distribution: string;
  release: string;
  variant: string;
  architecture: string;
  type: string; // "container" | "virtual-machine"
  alias: string;
  source: string; // e.g. "Custom", "Ubuntu"
  server: string;
  fingerprint: string;
}

export interface RemoteImageListResponse {
  images: RemoteImageInfo[];
  total: number;
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
  project?: string;
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
  /** True when the image was pulled from a remote rather than uploaded here. */
  cached: boolean;
  /** True when the image refreshes itself from its source. */
  auto_update: boolean;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  profiles: string[];
  /** Present on the flattened per-host rows the images page builds, not on the API model. */
  node?: string;
}

// Image List Response
export interface ImageListResponse {
  host: string;
  images: ImageInfo[];
  total: number;
  /** False when this host could not be reached, so an empty list is not "it has none". */
  reachable?: boolean;
  /** Why the host could not be reached, when it could not. */
  error?: string | null;
}

/** What is standing between an image and its deletion, straight from the cluster. */
export interface ImageUsageResponse {
  host: string;
  fingerprint: string;
  /** Instances on that cluster created from this image. */
  dependents: string[];
}

// GPU DRM (Direct Rendering Manager) Info
export interface GPUDRMInfo {
  id?: number;
  card_name?: string;
  card_device?: string;
  control_name?: string;
  control_device?: string;
  render_name?: string;
  render_device?: string;
}

// NVIDIA-specific GPU Information
export interface GPUNvidiaInfo {
  // note: `uuid` is deliberately absent. It is a per-device serial and the backend excludes it
  // from the response, because NodeResources rides the ungated /nodes body that Technologies
  // reads. It still exists server-side, where the card matcher uses it as a key.
  cuda_version?: string;
  nvrm_version?: string;
  brand?: string;
  model?: string;
  architecture?: string;
  card_name?: string;
  card_device?: string;
}

// SR-IOV GPU Information
export interface GPUSRIOVInfo {
  current_vfs: number;
  maximum_vfs: number;
  vfs_count: number;
}

// Mediated Device (mdev) Profile for vGPU
export interface GPUMdevProfile {
  name: string;
  api?: string;
  available: number;
  description?: string;
  devices: string[];
}

// Detailed GPU Card Information
export interface GPUCardInfo {
  // Core identification
  /** Position in the reported list. NOT a device selector. */
  index: number;
  /** DRM card id: the value LXD accepts as a gpu device `id`. */
  id?: number | null;
  driver?: string;
  driver_version?: string;
  pci_address?: string;
  usb_address?: string;
  
  // Vendor/Product info
  vendor?: string;
  vendor_id?: string;
  product?: string;
  product_id?: string;
  
  // System topology
  numa_node?: number;
  
  // Usage status
  in_use: boolean;
  used_by?: string;
  
  // Classification + enrichment (compute vs display, device model, unified memory) - backend-set.
  gpu_class?: "compute" | "integrated" | "display" | "unknown";
  is_compute?: boolean;
  /** Shares system RAM. Orthogonal to gpu_class: a Jetson iGPU is compute and integrated. */
  is_integrated?: boolean;
  /** An operator may pass this card into an instance. */
  attachable?: boolean;
  model_name?: string;
  memory_total?: number;
  memory_shared?: boolean;

  // Detailed info
  drm?: GPUDRMInfo;
  nvidia?: GPUNvidiaInfo;
  sriov?: GPUSRIOVInfo;
  mdev_profiles: GPUMdevProfile[];
}

// Node Resources
// ==================== Detected Hardware ====================
// Mirrors backend/app/models/schemas.py. Every field here survived an allowlist: asset
// identifiers (system UUID and serial, chassis and motherboard serials, disk serial and WWN,
// NIC MAC) are dropped in the backend and have no field on either side.

export interface FirmwareInfo {
  vendor?: string | null;
  version?: string | null;
  date?: string | null;
}

export interface ChassisInfo {
  vendor?: string | null;
  type?: string | null;
  version?: string | null;
}

export interface MotherboardInfo {
  vendor?: string | null;
  product?: string | null;
  version?: string | null;
}

export interface SystemInfo {
  vendor?: string | null;
  product?: string | null;
  family?: string | null;
  version?: string | null;
  type?: string | null;
  firmware?: FirmwareInfo | null;
  chassis?: ChassisInfo | null;
  motherboard?: MotherboardInfo | null;
}

export interface CPUSocketInfo {
  socket?: number | null;
  name?: string | null;
  vendor?: string | null;
  cores: number;
  threads: number;
  /** MHz. */
  frequency?: number | null;
  frequency_turbo?: number | null;
  member?: string | null;
}

export interface CPUTopology {
  /** Empty when the node reported none. Never defaulted to x86_64. */
  architectures: string[];
  /** Logical CPUs. NOT a core count. */
  total: number;
  /** 0 when the node sent no socket topology, which a Jetson does. */
  cores: number;
  threads: number;
  numa_nodes: number;
  sockets: CPUSocketInfo[];
}

export interface MemoryTopology {
  total: number;
  used: number;
  free: number;
  hugepages_total: number;
  hugepages_used: number;
  hugepages_size: number;
}

export interface NetworkPortInfo {
  id?: string | null;
  port?: number | null;
  protocol?: string | null;
  port_type?: string | null;
  transceiver_type?: string | null;
  auto_negotiation?: boolean | null;
  link_detected?: boolean | null;
  /** Mbit/s. */
  link_speed?: number | null;
  link_duplex?: string | null;
}

export interface NetworkCardInfo {
  vendor?: string | null;
  vendor_id?: string | null;
  product?: string | null;
  product_id?: string | null;
  driver?: string | null;
  driver_version?: string | null;
  firmware_version?: string | null;
  pci_address?: string | null;
  numa_node?: number | null;
  ports: NetworkPortInfo[];
  member?: string | null;
}

export interface StorageDeviceInfo {
  id?: string | null;
  device?: string | null;
  model?: string | null;
  type?: string | null;
  size: number;
  numa_node?: number | null;
  removable?: boolean | null;
  read_only?: boolean | null;
  firmware_version?: string | null;
  block_size?: number | null;
  /** 0 means solid state. The node reports rpm, not a boolean. */
  rpm?: number | null;
  partitions: number;
  member?: string | null;
}

export interface HardwareProfile {
  /** "cluster" when folded over MicroCloud members, so singular facts read differently. */
  scope: string;
  member_count: number;
  /** False when the node returned nothing usable. Show nothing rather than zeros. */
  detected: boolean;
  system?: SystemInfo | null;
  cpu: CPUTopology;
  memory: MemoryTopology;
  networks: NetworkCardInfo[];
  storage_devices: StorageDeviceInfo[];
  storage_types: string[];
}

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
  gpu_cards: GPUCardInfo[];
  gpu_cards_total?: number;
  gpu_integrated_total?: number;
  gpu_display_total?: number;
  gpu_unknown_total?: number;
  gpu_vendors?: string[];
  gpu_models?: string[];
  /** LXD reports allocation only. There is no GPU telemetry. */
  gpu_utilization_supported?: boolean;
  /** Usable capacity. On a clustered host a shared backing store is counted once, not once
   * per member, so this can be well below the sum of the pool readings. */
  storage_total: number;
  storage_used: number;
  storage_available: number;
  /** Every member's every pool added up. Equal to storage_total on a standalone host, higher
   * on a cluster with shared storage. Kept so no view loses the number it used to show. */
  storage_raw_total?: number;
  storage_raw_used?: number;
  storage_pools: Array<{
    name: string;
    driver: string;
    /** 'shared' means one backing store behind several members, counted once. 'local' means
     * per-member pools summed. Absent on a standalone host, where the question does not arise. */
    scope?: "shared" | "local";
    /** Which members this row was read from. Absent on a standalone host. */
    members?: string[];
    space_total: number;
    space_used: number;
    space_available: number;
  }>;
  /** At least one part of the read did not come back, so the numbers are floors. A clustered
   * host reporting zero storage used to be indistinguishable from one with no storage. */
  degraded?: boolean;
  /** Which parts were lost: 'storage', 'storage:<pool>'. */
  degraded_parts?: string[];
  /** Detected hardware. Absent when the node did not answer, which is not the same as empty. */
  hardware?: HardwareProfile | null;
  /** When this sample was read off the cluster. Absent means unknown, which must never
   * be rendered as "now": a body cached before this field existed carries no stamp. */
  collected_at?: string | null;
}

// LXD Node Environment Information (from LXD API)
export interface NodeEnvironment {
  // Architecture
  architectures?: string[];
  kernel?: string;
  kernel_architecture?: string;
  kernel_version?: string;
  // Operating System
  os_name?: string;
  os_version?: string;
  // Server Info
  server?: string;
  server_version?: string;
  server_name?: string;
  server_pid?: number;
  server_clustered?: boolean;
  // Driver Info
  driver?: string;
  driver_version?: string;
  // Storage
  storage?: string;
  storage_version?: string;
  // Other
  firewall?: string;
  project?: string;
  // note: these are all served and none was typed, so every read site cast. server_pid,
  // addresses, certificate and certificate_fingerprint are deliberately absent: the backend
  // allowlist drops them before the response leaves.
  instance_types?: string[];
  storage_supported_drivers?: Array<{ Name?: string; Version?: string; Remote?: boolean }>;
  /** Values are the STRINGS "true" and "false". Boolean("false") is true, so compare. */
  kernel_features?: Record<string, string>;
  lxc_features?: Record<string, string>;
  server_lts?: boolean;
  server_event_mode?: string;
  backup_metadata_version_range?: number[];
  [key: string]: unknown;
}

// Node Status
export interface NodeStatus {
  name: string;
  endpoint: string;
  connected: boolean;
  node_type: string | null;
  /** Whether the host reports itself as part of a cluster. null means not determined yet. */
  clustered?: boolean | null;
  server_version: string | null;
  api_version: string | null;
  auth: string | null;
  environment: NodeEnvironment | null;
  error: string | null;
  resources: NodeResources | null;
  tags: string[];
  display_name?: string | null;
  /** Whether the host answered our probe. connected=false with reachable=true means it
   *  answered and refused our certificate; reachable=false means it never answered. */
  reachable?: boolean | null;
  /** Server-derived status, so no client re-derives it from booleans. */
  status?: ClusterStatus;
}

// Nodes Status Response
export interface NodesStatusResponse {
  nodes: NodeStatus[];
  all_connected: boolean;
  /** When this fan-out was built. Covers nodes that never answered and so carry no
   * resources of their own. */
  collected_at?: string | null;
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
  /** False when this host could not be reached, so an empty list is not "it has none". */
  reachable?: boolean;
  /** Why the host could not be reached, when it could not. */
  error?: string | null;
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
  /** Set per row by the fan-out. The backend has always sent it; this type had not. */
  node?: string;
}

export interface NetworkListResponse {
  total: number;
  networks: NetworkInfo[];
  nodes: string[];
  /** Nodes that could not be reached; their networks are missing from `networks`. */
  unreachable_nodes?: string[];
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
  node?: string;
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
  node?: string;
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
  node?: string;
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
  /** All three are set by lxd_operations.py and were missing here, which is why the page
   *  had to fall back to `any` and lost checking on exactly the fields it filters by. */
  node_name?: string;
  project?: string;
  instance_name?: string;
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
  /** The registered node that reported it, distinct from `location`, the cluster member. */
  source_node: string;
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
  node_type: string;  // LXD, Incus, or MicroCloud
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
  auth_type: string;
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
  location?: {
    region_id: string;
    country_code: string;
    country_name: string;
    region_name: string;
    city: string;
    latitude: number;
    longitude: number;
    timezone: string;
    continent: string;
  };
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
  gpu_cards: GPUCardInfo[];
  gpu_cards_total?: number;
  gpu_integrated_total?: number;
  gpu_display_total?: number;
  gpu_unknown_total?: number;
  gpu_vendors?: string[];
  gpu_models?: string[];
  gpu_utilization_supported?: boolean;
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
  /** When this sample was read off the cluster. Absent means unknown, never "now". */
  collected_at?: string | null;
  /** At least one part of the read did not come back, so the numbers above are floors. */
  degraded?: boolean;
  /** Which parts were lost: "storage", "storage:<pool>", "instances". */
  degraded_parts?: string[];
}

export interface ClusterInfo {
  cluster_id: string;
  display_name: string;
  endpoint: string;
  zone?: string;
  /** Hypervisor family. null until the certificate is trusted and a real read is possible. */
  cluster_type: string | null;
  /** Whether the host reports itself as part of a cluster. null means not determined yet. */
  clustered?: boolean | null;
  merchant_id?: string;
  status: ClusterStatus;
  has_certificate: boolean;
  certificate_common_name?: string;
  labels: Record<string, string>;
  created_at?: string;
  last_checked?: string;
  error_message?: string;
  location?: GeoLocation;
}

export interface ClusterRegisterResponse {
  success: boolean;
  message: string;
  cluster_id: string;
  status: ClusterStatus;
  certificate_generated: boolean;
  trust_added: boolean;
  certificate?: string;
  cluster_type?: string;  // Auto-detected: "LXD", "MicroCloud", or "Incus"
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
  cluster_type?: string;  // Auto-detected: "LXD", "MicroCloud", or "Incus" - updated post-auth
  clustered?: boolean | null;  // Read from the authenticated payload; null when not determined
}

export interface ClusterListResponse {
  clusters: ClusterInfo[];
  total: number;
}

/** Machine-readable reason behind a status. Copy keys off this, never off `reason`. */
export enum AvailabilityReason {
  OK = "ok",
  UNTRUSTED_CERTIFICATE = "untrusted_certificate",
  UNREACHABLE = "unreachable",
  NO_CERTIFICATE = "no_certificate",
  UNKNOWN = "unknown",
}

/** One row for a cluster picker. Carries no hardware detail by design. */
export interface ClusterAvailability {
  /** Cluster id. The exact value a create request sends as its node. */
  name: string;
  display_name: string;
  endpoint: string;
  status: ClusterStatus;
  reason_code: AvailabilityReason;
  reason?: string | null;
  node_type?: string | null;
  checked_at: string;
  available: boolean;
}

export interface ClusterAvailabilityResponse {
  clusters: ClusterAvailability[];
  online_count: number;
  total_count: number;
  source: "nodes_cache" | "availability_cache" | "probe";
  generated_at: string;
}

export interface ClusterDeleteResponse {
  success: boolean;
  message: string;
  cluster_id: string;
}

// Version Information
export interface VersionInfo {
  app_version: string;
  /** The API contract version, not the build. Always sent; the backend types it as a required str. */
  api_version: string;
  build_date?: string | null;
  build_commit?: string | null;
  environment: string;
}

export interface SystemSettings {
  read_only_mode: boolean;
  api_version: string;
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
  inactive_nodes: number;
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
  cluster_status?: string; // online, pending_trust, offline, error
  status_message?: string; // Status message for troubleshooting
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
  gpu_available?: number;
  gpu_cards_total?: number;
  gpu_integrated_total?: number;
  gpu_models?: string[];
  gpu_utilization_supported?: boolean;
  instance_count: number;
  running_instances: number;
}

export interface InstanceMonitoringSummary {
  name: string;
  host: string;
  project?: string | null;
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
  /** The registered cluster this pool was read through, not the machine holding the disk. */
  node: string;
  /** Cluster members this row covers. Empty on a standalone host. */
  members?: string[];
  /** 'shared': one backing store counted once. 'local': per-member pools summed. */
  scope?: "shared" | "local" | null;
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
  inactive_nodes: number;
  
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
  total_gpu_cards?: number;
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

export interface ContainerInfo {
  is_containerized: boolean;
  runtime: string;
  cgroup_version: string | null;
  memory_limit: number | null;
  cpu_limit: number | null;
}

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
  container: ContainerInfo | null;
  cpu: CPUInfo;
  memory: MemoryInfo;
  disks: DiskInfo[];
  network: NetworkInterfaceInfo[];
  process: ProcessInfo | null;
  python: PythonInfo;
  os: OSInfo;
}

// ========================
// Certificate Management Types
// ========================

export enum CertificateStatus {
  VALID = "valid",
  EXPIRING = "expiring",
  EXPIRED = "expired",
  UNKNOWN = "unknown",
  PENDING_RENEWAL = "pending_renewal"
}

export enum CertificateTTLOption {
  ONE_MONTH = "720h",
  TWO_MONTHS = "1440h",
  THREE_MONTHS = "2160h",
  FOUR_MONTHS = "2880h",
  FIVE_MONTHS = "3600h",
  SIX_MONTHS = "4320h",
  ONE_YEAR = "8760h"
}

export const TTL_LABELS: Record<CertificateTTLOption, string> = {
  [CertificateTTLOption.ONE_MONTH]: "1 Month",
  [CertificateTTLOption.TWO_MONTHS]: "2 Months",
  [CertificateTTLOption.THREE_MONTHS]: "3 Months",
  [CertificateTTLOption.FOUR_MONTHS]: "4 Months",
  [CertificateTTLOption.FIVE_MONTHS]: "5 Months",
  [CertificateTTLOption.SIX_MONTHS]: "6 Months",
  [CertificateTTLOption.ONE_YEAR]: "1 Year"
};

export interface CertificateInfo {
  cluster_id: string;
  common_name?: string | null;
  endpoint?: string | null;
  issuer?: string | null;
  issued_at?: string | null;
  expires_at?: string | null;
  days_until_expiry?: number | null;
  status: CertificateStatus;
  fingerprint?: string | null;
  has_pending_renewal: boolean;
  serial_number?: string | null;
}

export interface CertificateListResponse {
  certificates: CertificateInfo[];
  total: number;
  expiring_count: number;
  expired_count: number;
}

export interface CertificateRenewalRequest {
  cluster_id: string;
  ttl: CertificateTTLOption;
  trust_password?: string;
}

export interface CertificateRenewalResponse {
  success: boolean;
  message: string;
  cluster_id: string;
  auto_renewed: boolean;
  requires_manual_trust: boolean;
  certificate?: string;
  cli_command?: string;
  pending_cert_id?: string;
}

export interface CertificateVerifyRequest {
  cluster_id: string;
  pending_cert_id?: string;
}

export interface CertificateVerifyResponse {
  success: boolean;
  message: string;
  cluster_id: string;
  verified: boolean;
  activated: boolean;
}

export interface CertificateWarning {
  cluster_id: string;
  display_name: string;
  warning_type: string;
  severity: string;
  message: string;
  days_until_expiry?: number | null;
  expires_at?: string | null;
  endpoint?: string | null;
}

export interface SystemWarningsResponse {
  total: number;
  critical: number;
  warning: number;
  info: number;
  lxd_warnings: WarningInfo[];
  certificate_warnings: CertificateWarning[];
  node_warnings: CertificateWarning[];
  nodes: string[];
}

// ============================================
// External Integrations (Orcahub)
// ============================================

export interface ClusterPermission {
  cluster_id: string;
  // Present on the /validate response, which resolves the display name from Vault so an
  // integrator sees a cluster they recognise rather than an id. Absent on a stored grant.
  cluster_name?: string | null;
  permissions: string[];  // read, write, admin
  projects: string[];
}

export interface ExternalUserMapping {
  external_user_id: string;
  external_email: string;
  external_name?: string | null;
  orcastra_identity_id?: string | null;
  permissions: ClusterPermission[];
  created_at: string;
  updated_at: string;
}

export interface APIKeyInfo {
  id: string;
  name: string;
  app_name: string;
  app_type: string;
  // null marks a key issued before capabilities existed. It keeps unrestricted access to the
  // external router, which is why the UI flags it rather than rendering it as "no access".
  capabilities?: string[] | null;
  created_at: string;
  expires_at?: string | null;
  // note: both derived server-side from expires_at on every read, never stored. `is_expired` is
  // what a client branches on; `expiry_state` is what the card renders, and it carries the
  // "expiring" case the UI had no way to compute.
  is_expired?: boolean;
  expiry_state?: "never" | "active" | "expiring" | "expired";
  is_active: boolean;
  is_revoked: boolean;
  last_used_at?: string | null;
  cluster_permissions: ClusterPermission[];
  user_mappings_count: number;
}

export interface APIKeyCreateRequest {
  name: string;
  app_name: string;
  app_type?: string;
  // Omit to take the application type's defaults. An empty array is rejected by the API.
  capabilities?: string[];
  cluster_permissions: ClusterPermission[];
  expire_days?: number | null;
}

export interface APIKeyValidateRequest {
  api_key_id: string;
  api_key_secret: string;
}

export interface APIKeyValidateResponse {
  valid: boolean;
  app_name?: string | null;
  app_type?: string | null;
  // null means a legacy key issued before capabilities existed, which is unrestricted.
  capabilities?: string[] | null;
  cluster_permissions: ClusterPermission[];
  error?: string | null;
}

export interface CapabilityOption {
  name: string;
  description: string;
  // Needs a deliberate decision rather than an application-type default. The picker marks these
  // instead of listing all capabilities as equivalent ticks.
  elevated: boolean;
  // note: the reason comes from the server because there is more than one. Some elevated
  // capabilities write access control; others reach inside a guest. A single hardcoded label
  // was wrong for half of them.
  elevated_reason?: string | null;
  // Never seeded by an application type and never inherited by a key issued before capabilities
  // existed, so the form says so rather than letting a tick look routine.
  never_inherited?: boolean;
}

export interface ApplicationTypeOption {
  name: string;
  default_capabilities: string[];
}

export interface CapabilityVocabularyResponse {
  capabilities: CapabilityOption[];
  application_types: ApplicationTypeOption[];
}

export interface APIKeyCreateResponse {
  api_key: APIKeyInfo;
  api_key_id: string;
  api_key_secret: string;
}

export interface APIKeyListResponse {
  total: number;
  active: number;
  revoked: number;
  // note: expired keys used to be counted in `active`. Without this the three tiles stopped
  // adding up: on a fleet with six lapsed keys the Active figure simply dropped by six with
  // nowhere for them to go, which reads as keys having been deleted.
  expired?: number;
  keys: APIKeyInfo[];
}

export interface UserMappingRequest {
  external_user_id: string;
  external_email: string;
  external_name?: string;
  cluster_permissions: ClusterPermission[];
}

export interface AvailableCluster {
  /** Cluster id. The exact value a grant stores as cluster_id. */
  id: string;
  /** Display name, falling back to the id. */
  name: string;
  endpoint: string | null;
  server_type: string | null;
  /**
   * Connection status, or absent when reachability could not be established.
   *
   * important: absent is not offline. A picker renders it as no marking at all rather than
   * painting the fleet dead because one probe was slow.
   */
  status?: ClusterStatus | null;
  reason_code?: AvailabilityReason | null;
  reason?: string | null;
  checked_at?: string | null;
  /** Tri-state, mirroring `status`: true, false, or null when there is nothing to judge. */
  available?: boolean | null;
}

export interface AvailableClustersResponse {
  clusters: AvailableCluster[];
}

// ============================================
// Tag System Types (Enterprise-Grade)
// ============================================

export interface TagCategory {
  id: number;
  prefix: string;
  name: string;
  description: string | null;
  color: string;
  icon: string;
  sort_order: number;
  is_active: boolean;
  is_system: boolean;
  created_at: string;
  updated_at: string;
  tag_count: number;
}

export interface TagCategoryCreate {
  prefix: string;
  name: string;
  description?: string | null;
  color?: string;
  icon?: string;
  sort_order?: number;
  is_active?: boolean;
}

export interface TagCategoryUpdate {
  name?: string;
  description?: string | null;
  color?: string;
  icon?: string;
  sort_order?: number;
  is_active?: boolean;
}

export interface Tag {
  id: number;
  name: string;
  display_name: string | null;
  description: string | null;
  color: string;
  category_id: number | null;
  category: TagCategory | null;
  is_system: boolean;
  usage_count: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface TagCreate {
  name: string;
  display_name?: string | null;
  description?: string | null;
  color?: string | null;
  category_id?: number | null;
}

export interface TagUpdate {
  display_name?: string | null;
  description?: string | null;
  color?: string | null;
  category_id?: number | null;
}

export interface TagGrouped {
  location: string[];
  region: string[];
  vendor: string[];
  ownership: string[];
  environment: string[];
  application: string[];
  sla: string[];
  cost_center: string[];
  other: string[];
}

export interface ResourceTagsResponse {
  resource_type: string;
  resource_id: string;
  resource_host: string | null;
  resource_project: string | null;
  tags: string[];
  grouped: TagGrouped;
}

export interface BulkTagAssignRequest {
  tags: string[];
  resource_type?: string;
  resource_id?: string;
  resource_host?: string | null;
  resource_project?: string | null;
}

export interface BulkTagAssignResponse {
  success: boolean;
  message: string;
  assigned_tags: string[];
  failed_tags: string[];
  synced_to_lxd: boolean;
}

export interface TagListResponse {
  tags: Tag[];
  total: number;
  categories: TagCategory[];
}

export interface TagStatistics {
  tag_name: string;
  display_name: string | null;
  category: string | null;
  color: string;
  node_count: number;
  project_count: number;
  instance_count: number;
  total_usage: number;
}

export interface TagStatsResponse {
  total_tags: number;
  total_assignments: number;
  categories: TagCategory[];
  top_tags: TagStatistics[];
  tags_by_category: Record<string, number>;
}

export interface UserTagVisibilitySettings {
  visibility_mode: "all" | "whitelist" | "blacklist";
  visible_categories: string[];
  visible_tags: string[];
  hidden_categories: string[];
  hidden_tags: string[];
  default_filter_tags: string[];
}

export interface UserTagVisibilityResponse extends UserTagVisibilitySettings {
  user_id: string;
  updated_at: string;
}

// Tag category prefix to human-readable name mapping
export const TAG_CATEGORY_PREFIXES: Record<string, { name: string; color: string; icon: string }> = {
  "loc_": { name: "Location", color: "#3b82f6", icon: "map-pin" },
  "reg_": { name: "Region", color: "#06b6d4", icon: "globe" },
  "vend_": { name: "Vendor", color: "#8b5cf6", icon: "building" },
  "own_": { name: "Ownership", color: "#f59e0b", icon: "user-check" },
  "env_": { name: "Environment", color: "#10b981", icon: "layers" },
  "app_": { name: "Application", color: "#ec4899", icon: "package" },
  "sla_": { name: "SLA Level", color: "#ef4444", icon: "shield-check" },
  "cost_": { name: "Cost Center", color: "#84cc16", icon: "wallet" },
};

// Helper function to classify a tag
export function classifyTag(tag: string): { prefix: string; value: string; category: string } {
  const knownPrefixes = ["loc_", "reg_", "vend_", "own_", "env_", "app_", "sla_", "cost_"];
  
  for (const prefix of knownPrefixes) {
    if (tag.startsWith(prefix)) {
      const categoryInfo = TAG_CATEGORY_PREFIXES[prefix];
      return {
        prefix,
        value: tag.slice(prefix.length),
        category: categoryInfo?.name || "Unknown",
      };
    }
  }
  
  return { prefix: "", value: tag, category: "Other" };
}

// Helper function to get tag display name
export function getTagDisplayName(tag: string): string {
  const { value } = classifyTag(tag);
  return value.replace(/-/g, " ").replace(/_/g, " ").split(" ")
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

// Helper function to get tag color
export function getTagColor(tag: string): string {
  const { prefix } = classifyTag(tag);
  if (prefix && TAG_CATEGORY_PREFIXES[prefix]) {
    return TAG_CATEGORY_PREFIXES[prefix].color;
  }
  return "#6b7280"; // Gray for uncategorized
}

// Helper function to group tags
export function groupTags(tags: string[]): TagGrouped {
  const grouped: TagGrouped = {
    location: [],
    region: [],
    vendor: [],
    ownership: [],
    environment: [],
    application: [],
    sla: [],
    cost_center: [],
    other: [],
  };

  const prefixToCategory: Record<string, keyof TagGrouped> = {
    "loc_": "location",
    "reg_": "region",
    "vend_": "vendor",
    "own_": "ownership",
    "env_": "environment",
    "app_": "application",
    "sla_": "sla",
    "cost_": "cost_center",
  };

  for (const tag of tags) {
    const { prefix, value } = classifyTag(tag);
    const category = prefixToCategory[prefix];
    
    if (category) {
      grouped[category].push(value);
    } else {
      grouped.other.push(tag);
    }
  }

  return grouped;
}

// ==================== Regions / Geo Types ====================

export interface GeoLocation {
  region_id: string;
  region_name: string;
  country_code: string;
  country_name: string;
  city: string;
  latitude: number;
  longitude: number;
  timezone: string;
  continent: string;
  facility_name?: string;
  facility_provider?: string;
}

export interface ClusterGeoInfo extends ClusterInfo {
  location?: GeoLocation;
}

// Extended hardware info for regions page
export interface ExtendedHardwareInfo {
  // CPU details
  cpu_total: number;
  cpu_sockets: number;
  cpu_architecture: string;
  cpu_vendor?: string;  // Intel, AMD, Apple, ARM
  cpu_model?: string;   // e.g., "Intel Xeon Gold 6248"
  
  // Memory
  memory_total: number;
  memory_used: number;
  
  // GPU
  /** Compute GPUs: sellable capacity, display controllers excluded. */
  gpu_total: number;
  /** Every DRM card detected, display controllers included. */
  gpu_cards_total?: number;
  gpu_integrated_total?: number;
  gpu_vendors: string[];
  gpu_models: string[];
  
  // Storage
  storage_total: number;
  storage_used: number;
  storage_types: string[];  // e.g., ["NVMe", "SATA", "SSD"]
  storage_drivers: string[];  // e.g., ["zfs", "btrfs", "ceph"]
  
  // Network
  network_interfaces?: string[];
}

export interface RegionSummary {
  region_id: string;
  region_name: string;
  country_code: string;
  country_name: string;
  city: string;
  latitude: number;
  longitude: number;
  continent: string;
  facility_name?: string;
  facility_provider?: string;
  
  // Aggregated stats
  node_count: number;
  instance_count: number;
  instance_running: number;
  
  // Hardware summary (aggregated from all nodes)
  hardware: ExtendedHardwareInfo;
  
  // Node details
  nodes: Array<{
    cluster_id: string;
    display_name: string;
    status: ClusterStatus;
    node_type?: string;
    hardware: ExtendedHardwareInfo;
  }>;
}

export interface RegionsOverviewResponse {
  regions: RegionSummary[];
  total_nodes: number;
  total_instances: number;
  total_cpu: number;
  total_memory: number;
  total_storage: number;
  total_gpu: number;
  total_gpu_cards?: number;
  total_gpu_integrated?: number;
}

// Cluster location update
export interface ClusterLocationUpdateRequest {
  location: GeoLocation;
}

export interface ClusterLocationUpdateResponse {
  success: boolean;
  cluster_id: string;
  message: string;
  location: GeoLocation;
}

// ── Organization types ──

export interface Organization {
  // important: a number on the wire. OrgResponse.id is an int, and this said `string` for as
  // long as the type has existed, which is not a cosmetic lie: it let
  // components/org-personalization-settings.tsx hold the raw value in a `string` state and hand
  // it to a Radix Select, where it never matched the `String(id)` on any SelectItem, so the
  // picker rendered its placeholder while an organization was in fact selected. Coerce with
  // String() at any UI boundary that compares by value.
  id: number;
  name: string;
  slug: string;
  description: string | null;
  created_by: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  member_count: number;
  cluster_count: number;
  // Whether the CALLER belongs to this organization. An Admin's list is every active
  // organization, so this is what separates "yours" from "a customer's".
  is_member: boolean;
}

/** Organization synced from external app (OrcaHub) */
export interface IntegrationOrganization {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  source: string;
  external_id: string | null;
  api_key_id: string | null;
  app_name: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface OrganizationCreateRequest {
  name: string;
  description?: string;
}

export interface OrganizationUpdateRequest {
  name?: string;
  description?: string;
  is_active?: boolean;
}

export interface OrganizationMember {
  id: string;
  organization_id: string;
  user_email: string;
  role: string;
  invited_by: string;
  joined_at: string;
}

export interface OrganizationCluster {
  id: string;
  organization_id: string;
  cluster_id: string;
  registered_by: string;
  registered_at: string;
}

// ── User Groups types ──

export interface UserGroupEntry {
  user_email: string;
  role: string;
  organizations: { id: string; name: string; slug: string }[];
}

export interface UserGroupAssignRequest {
  user_email: string;
  role: string;
  organization_id?: string;
}

export interface UserGroupAssignResponse {
  message: string;
  user_email: string;
  role: string;
  authentik_synced: boolean;
}

export interface AuthentikSyncStatus {
  authentik_configured: boolean;
  authentik_reachable: boolean;
  groups_exist: boolean;
  details: string;
}

// ========== Tenant Access Policy Types (ACL) ==========

/** Valid permission types for tenant policies */
export type TenantPermission = "view" | "operate" | "console" | "snapshot" | "file_transfer";

export const TENANT_PERMISSION_LABELS: Record<TenantPermission, string> = {
  view: "View",
  operate: "Operate (Start/Stop/Restart)",
  console: "Console & Terminal",
  snapshot: "Snapshots",
  file_transfer: "File Transfer",
};

export const TENANT_PERMISSION_DESCRIPTIONS: Record<TenantPermission, string> = {
  view: "See resources in dashboards and monitoring",
  operate: "Start, stop, restart, freeze/unfreeze VMs",
  console: "Open graphical console or terminal to VM",
  snapshot: "Create, restore, and delete snapshots",
  file_transfer: "Upload and download files to/from VM",
};

export const ALL_TENANT_PERMISSIONS: TenantPermission[] = [
  "view", "operate", "console", "snapshot", "file_transfer",
];

export interface PolicyRule {
  id?: number;
  cluster_id: string;
  project_name: string;
  instance_name: string;
  permissions: TenantPermission[];
}

export interface AccessPolicy {
  id: number;
  organization_id: number;
  name: string;
  description?: string | null;
  is_default: boolean;
  source: string;
  external_id?: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  rules: PolicyRule[];
  assignments_count: number;
}

export interface PolicyListResponse {
  policies: AccessPolicy[];
  total: number;
}

export interface PolicyCreateRequest {
  name: string;
  description?: string;
  organization_id: number;
  is_default?: boolean;
  rules: Omit<PolicyRule, "id">[];
}

export interface PolicyUpdateRequest {
  name?: string;
  description?: string;
  is_default?: boolean;
  rules?: Omit<PolicyRule, "id">[];
}

export interface PolicyAssignment {
  id: number;
  policy_id: number;
  policy_name: string;
  org_member_id: number;
  user_email: string;
  organization_id: number;
  organization_name: string;
  assigned_by: string;
  assigned_at: string;
  expires_at?: string | null;
  source: string;
}

export interface AssignmentListResponse {
  assignments: PolicyAssignment[];
  total: number;
}

export interface AssignmentCreateRequest {
  policy_id: number;
  user_email: string;
  organization_id: number;
  expires_at?: string | null;
}

export interface ResolvedAccess {
  cluster_id: string;
  project_name: string;
  instance_name: string;
  permissions: TenantPermission[];
  policy_name: string;
}

export interface MyAccessResponse {
  access: ResolvedAccess[];
  total_clusters: number;
  total_rules: number;
}
