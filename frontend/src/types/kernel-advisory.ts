// Mirrors backend/app/models/kernel_advisory_schemas.py.

export type KernelAdvisoryState = "patched" | "exposed" | "unverified";
export type KernelAdvisorySeverity = "critical" | "warning" | "info";

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
}

export interface KernelAdvisoryFinding {
  advisory_id: string;
  state: KernelAdvisoryState;
  severity: KernelAdvisorySeverity;
  reason: string;
  check_command: string | null;
  fixed_in: string | null;
  reference_url: string;
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
}
