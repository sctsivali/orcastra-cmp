/** GET /system/host-health: the dashboard's own dependencies (Administrators only). */
export type HostCheckId = "vault" | "logging" | "identity" | "database" | "cache" | "storage";
export type HostCheckStatus = "ok" | "degraded" | "down" | "unknown" | "not_configured";
export type HostCheckSeverity = "critical" | "warning" | "info";

export interface HostHealthFact {
  label: string;
  value: string | number | boolean | null;
}

export interface HostCheck {
  id: HostCheckId;
  label: string;
  status: HostCheckStatus;
  severity: HostCheckSeverity;
  summary: string;
  facts: HostHealthFact[];
  advisories: string[];
  remediation: string | null;
  actions: "unseal_vault"[];
  /** Epoch seconds when this check last changed status. */
  since: number | null;
  checked_at: number;
  latency_ms: number | null;
}

export interface HostHealthReport {
  checks: HostCheck[];
  overall: HostCheckStatus;
  attention: number;
  collected_at: number;
  source: "cached" | "live";
  store: "shared" | "process";
  interval_seconds: number;
}

/** GET /system/vault/seal-status and the body of POST /system/vault/unseal. */
export interface VaultSealStatus {
  state: "ok" | "sealed" | "uninitialized" | "unreachable" | "not_configured";
  seal_type: string | null;
  threshold: number | null;
  shares: number | null;
  progress: number | null;
  /** The current attempt's id; "" while no key has been entered. */
  nonce: string;
  version: string | null;
  unseal_available: boolean;
  unseal_unavailable_reason: string | null;
  insecure_transport: boolean;
  checked_at: number;
}

export type UnsealOutcome = "accepted" | "duplicate" | "unsealed" | "reset";

export interface VaultUnsealResult extends VaultSealStatus {
  outcome: UnsealOutcome;
}
