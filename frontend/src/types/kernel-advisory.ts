// Mirrors backend/app/models/kernel_advisory_schemas.py.

export type KernelAdvisoryState = "patched" | "exposed" | "unverified";
export type KernelAdvisorySeverity = "critical" | "warning" | "info";
/** "ubuntu": Ubuntu's record of the CVE decided; "built-in": the data shipped with this version. */
export type KernelAdvisorySource = "ubuntu" | "built-in";

export interface KernelAdvisoryScore {
  source: string;
  score: number | null;
  vector: string | null;
  rating: string | null;
}

export interface KernelAdvisoryInfo {
  id: string;
  name: string | null;
  summary: string;
  scores: KernelAdvisoryScore[];
  reference_url: string;
  data_as_of: string;
  introduced: string;
  fixed_upstream: string[];
  fixed_mainline: string;
  // Optional: absent from responses older than the Ubuntu check.
  source?: KernelAdvisorySource;
  /** The last successful read of Ubuntu's record. */
  checked_at?: string | null;
  /** The last read is older than the verdicts trust (three days). */
  stale?: boolean;
  /** Why the last read failed, while it is still failing. */
  check_error?: string | null;
  ubuntu_priority?: string | null;
  ubuntu_cvss_score?: number | null;
  ubuntu_url?: string | null;
}

export interface KernelAdvisoryFinding {
  advisory_id: string;
  state: KernelAdvisoryState;
  severity: KernelAdvisorySeverity;
  reason: string;
  check_command: string | null;
  fixed_in: string | null;
  reference_url: string;
  source?: KernelAdvisorySource;
  checked_at?: string | null;
}

export interface HostKernelRow {
  cluster_id: string;
  display_name: string;
  member: string;
  clustered: boolean;
  members_listed: boolean;
  kernel_version: string | null;
  os_name: string | null;
  os_version: string | null;
  vm_capable: boolean;
  read_error: string | null;
  collected_at: string | null;
  findings: KernelAdvisoryFinding[];
}

export interface KernelAdvisoryResponse {
  advisories: KernelAdvisoryInfo[];
  hosts: HostKernelRow[];
  counts: { exposed: number; unverified: number; patched: number };
  unreachable_nodes?: string[];
  partial_nodes?: string[];
  node_errors?: Record<string, string>;
  /** False when the deployment turned the reads of Ubuntu's records off (KERNEL_CVE_FEED_ENABLED). */
  ubuntu_check_enabled?: boolean;
}

/** One `/warnings/system/all` row: a member that is exposed or cannot be verified. */
export interface KernelAdvisoryWarning {
  cluster_id: string;
  display_name: string;
  member: string;
  warning_type: "kernel_exposed" | "kernel_unverified";
  advisory_id: string;
  state: KernelAdvisoryState;
  severity: KernelAdvisorySeverity;
  message: string;
  kernel_version: string | null;
  os_name: string | null;
  os_version: string | null;
  check_command: string | null;
  reference_url: string;
  data_as_of: string;
  fixed_in?: string | null;
  source?: KernelAdvisorySource;
  checked_at?: string | null;
  ubuntu_priority?: string | null;
  ubuntu_url?: string | null;
}

/** One tracked CVE's reads of Ubuntu's record (GET .../ubuntu-status, POST .../check). */
export interface KernelCveCheck {
  id: string;
  source: KernelAdvisorySource;
  checked_at: string | null;
  last_attempt_at: string | null;
  stale: boolean;
  check_error: string | null;
  ubuntu_priority: string | null;
  ubuntu_cvss_score: number | null;
  ubuntu_updated_at: string | null;
  ubuntu_url: string;
  data_as_of: string;
}

export interface KernelCveFeedStatus {
  enabled: boolean;
  /** Reading the operator's mirror (KERNEL_CVE_FEED_URL) rather than ubuntu.com. */
  mirror: boolean;
  advisories: KernelCveCheck[];
}
