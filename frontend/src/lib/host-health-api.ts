import { fetchAPI, fetchRead, type ReadOptions } from "@/lib/api";
import type { HostHealthReport, VaultSealStatus, VaultUnsealResult } from "@/types/host-health";

// A fresh report runs every check; Vault's own read can take 20 s on a slow registry LIST.
const FRESH_TIMEOUT_MS = 40_000;

export const hostHealthAPI = {
  report: (opts: ReadOptions = {}) =>
    fetchRead<HostHealthReport>("/system/host-health", opts, opts.fresh ? FRESH_TIMEOUT_MS : undefined),
  sealStatus: (opts: ReadOptions = {}) => fetchRead<VaultSealStatus>("/system/vault/seal-status", opts),
  /**
   * One key share, or a reset. `nonce` is the attempt the caller last saw ("" when none): the
   * backend refuses rather than add a share to an attempt someone else reset in between.
   */
  unseal: (body: { key: string; nonce: string } | { reset: true; nonce: string }) =>
    fetchAPI<VaultUnsealResult>("/system/vault/unseal", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
