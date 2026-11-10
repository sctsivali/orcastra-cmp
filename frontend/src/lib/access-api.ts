import { fetchAPI } from "@/lib/api";

/** One of the caller's own projects whose access is limited or ending because of its paid term. */
export interface TenantTermItem {
  cluster_id: string;
  project_name: string;
  phase: "ending" | "read_only";
  /** Read-only because OrcaHub narrowed the access without sending dates. */
  inferred: boolean;
  term_ended_at: string | null;
  access_ends_at: string | null;
}

export type TenantTermPhase = "active" | "ending" | "read_only" | "ended";

export interface TenantAccessSummary {
  total_clusters: number;
  total_rules: number;
  // Optional: a backend that predates the paid term answers without them.
  phase?: TenantTermPhase;
  items?: TenantTermItem[];
  ended_at?: string | null;
}

/** GET /me/access: whether this session may use the dashboard, and if not, why. */
export interface MyAccessVerdict {
  email: string | null;
  role: "admin" | "partner" | "tenant" | null;
  has_role: boolean;
  blocked_reason: "no_role_assigned" | "no_tenant_access" | null;
  tenant_access: TenantAccessSummary | null;
}

export const accessAPI = {
  myAccess: (signal?: AbortSignal) => fetchAPI<MyAccessVerdict>("/me/access", { signal }),
};
