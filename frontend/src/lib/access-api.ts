import { fetchAPI } from "@/lib/api";

/** GET /me/access: whether this session may use the dashboard, and if not, why (issue #17). */
export interface MyAccessVerdict {
  email: string | null;
  role: "admin" | "partner" | "tenant" | null;
  has_role: boolean;
  blocked_reason: "no_role_assigned" | "no_tenant_access" | null;
  tenant_access: { total_clusters: number; total_rules: number } | null;
}

export const accessAPI = {
  myAccess: (signal?: AbortSignal) => fetchAPI<MyAccessVerdict>("/me/access", { signal }),
};
