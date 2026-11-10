import { fetchRead, type ReadOptions } from "@/lib/api";

/** GET /system/vault-status: what the Vault notice shows. */
export type VaultState = "ok" | "sealed" | "uninitialized" | "unreachable" | "unavailable" | "not_configured";

export interface VaultStatus {
  state: VaultState;
  checked_at: number;
  /** "last_known" while the cluster list is served from before the outage. */
  registry_source?: "vault" | "last_known";
  /** Administrators only. */
  detail?: {
    reason?: string | null;
    registry_error?: string | null;
    registry_checked_at?: number | null;
  };
}

export const vaultStatusAPI = {
  get: (opts: ReadOptions = {}) => fetchRead<VaultStatus>("/system/vault-status", opts),
};
