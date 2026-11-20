/** Mini's own API: operators, the caller's certificate, the audit log, and the sign-in status. */

import { API_URL, authHeaders, fetchAPI } from "@/lib/api";

export type OperatorRole = "admin" | "partner" | "tenant";

export interface CertificateView {
  fingerprint: string;
  common_name: string;
  status: "active" | "revoked" | "superseded";
  not_after: string | null;
  created_at: string;
  last_used_at: string | null;
  renewed_from_fingerprint: string | null;
}

export interface Operator {
  uuid: string;
  email: string;
  display_name: string | null;
  role: OperatorRole;
  status: "active" | "revoked" | "deleted";
  owner_org_id: number | null;
  owner_org_name: string | null;
  issued_by: string | null;
  last_login_at: string | null;
  created_at: string;
  expires_at: string | null;
  expiring: boolean;
  certificates: CertificateView[];
  manageable: boolean;
}

export interface IssueRequest {
  email: string;
  display_name?: string;
  role: OperatorRole;
  organization_id?: number | null;
  ttl_days?: number;
}

/** A one-time .p12 bundle and the password that opens it. */
export interface IssuedBundle {
  blob: Blob;
  filename: string;
  password: string;
  operatorUuid: string;
}

export interface MyCertificate {
  email: string;
  display_name: string | null;
  role: OperatorRole;
  fingerprint: string | null;
  common_name: string | null;
  not_after: string | null;
  days_left: number | null;
  expiring: boolean;
  renew_available: boolean;
  renew_opens_at: string | null;
  other_active_certificates: number;
}

export interface CertStatus {
  certificate_presented: boolean;
  enrolled: boolean;
  active: boolean;
  fingerprint: string | null;
  common_name: string | null;
  expires_at: string | null;
  bootstrap_available: boolean;
}

export interface AuditRow {
  id: number;
  event_id: string;
  occurred_at: string;
  actor_email: string | null;
  actor_role: string | null;
  action: string;
  category: string | null;
  severity: string | null;
  result: string | null;
  target_type: string | null;
  target_id: string | null;
  target_host: string | null;
  target_project: string | null;
  source_ip: string | null;
  request_id: string | null;
  details: Record<string, unknown> | null;
}

export interface AuditPage {
  items: AuditRow[];
  total: number;
  page: number;
  page_size: number;
}

export interface AuditVerifyReport {
  valid: boolean;
  checked: number;
  broken_at_id: number | null;
  count: number;
  max_id: number;
  head_hash: string | null;
  pruned_through_id: number;
  note: string | null;
}

export interface AuditStatus {
  retention_days: number;
  rows: number;
  oldest: string | null;
  pruned_through_id: number;
  last_verified_max_id: number;
  writer: { running: boolean; dropped: number; failed: number; last_error: string | null };
}

export interface AuditQuery {
  page?: number;
  page_size?: number;
  action?: string;
  actor?: string;
  result?: string;
  start?: string;
  end?: string;
}

/** The message a refused request carries, with the next step the backend put in it. */
export async function errorText(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.message === "string") return detail.message;
  } catch {
    // not JSON
  }
  return fallback;
}

async function bundle(path: string, init: RequestInit, fallback: string): Promise<IssuedBundle> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init.body ? { "content-type": "application/json" } : {}) },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorText(response, fallback));
  const password = response.headers.get("x-p12-password") ?? "";
  const disposition = response.headers.get("content-disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "certificate.p12";
  if (!password) throw new Error("The certificate arrived without its password. Issue it again.");
  return { blob: await response.blob(), filename, password, operatorUuid: response.headers.get("x-operator-uuid") ?? "" };
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const miniApi = {
  listOperators: (status?: string) => fetchAPI<Operator[]>(`/operators${query({ status })}`),
  issueOperator: (request: IssueRequest) =>
    bundle("/operators", { method: "POST", body: JSON.stringify(request) }, "The certificate could not be issued."),
  updateOperator: (uuid: string, change: { role?: OperatorRole; status?: "active" | "revoked"; display_name?: string }) =>
    fetchAPI<Operator>(`/operators/${encodeURIComponent(uuid)}`, { method: "PATCH", body: JSON.stringify(change) }),
  reissueCertificate: (uuid: string) =>
    bundle(`/operators/${encodeURIComponent(uuid)}/certificates`, { method: "POST" }, "The certificate could not be issued."),
  revokeCertificate: (uuid: string, fingerprint: string) =>
    fetchAPI<Operator>(`/operators/${encodeURIComponent(uuid)}/certificates/${encodeURIComponent(fingerprint)}`, { method: "DELETE" }),
  deleteOperator: (uuid: string) => fetchAPI<Operator>(`/operators/${encodeURIComponent(uuid)}`, { method: "DELETE" }),
  restoreOperator: (uuid: string) => fetchAPI<Operator>(`/operators/${encodeURIComponent(uuid)}/restore`, { method: "POST" }),

  myCertificate: () => fetchAPI<MyCertificate>("/me/certificate"),
  renewMyCertificate: () => bundle("/me/certificate/renew", { method: "POST" }, "The certificate could not be renewed."),

  auditLog: (q: AuditQuery) => fetchAPI<AuditPage>(`/audit-log${query({ ...q })}`),
  auditStatus: () => fetchAPI<AuditStatus>("/audit-log/status"),
  verifyAudit: () => fetchAPI<AuditVerifyReport>("/audit-log/verify", {}, 300_000),
  auditExportUrl: (format: "csv" | "json", q: AuditQuery) => `${API_URL}/audit-log/export${query({ format, ...q })}`,
};

/** Save a downloaded bundle under its own name. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}
